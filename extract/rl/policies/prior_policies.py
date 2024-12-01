import torch
import copy

from extract.modules.variational_inference import MultivariateGaussian, mc_kl_divergence
from extract.rl.components.agent import BaseAgent
from extract.rl.components.policy import Policy
from extract.rl.policies.mlp_policies import (
    SplitObsMLPPolicy,
    MLPPolicy,
    HybridConvMLPPolicy,
)
from extract.utils.general_utils import AttrDict, ParamDict
from extract.utils.pytorch_utils import no_batchnorm_update


class PriorInitializedPolicy(Policy):
    """Initializes policy network with learned prior net."""

    def __init__(self, config):
        self._hp = self._default_hparams().overwrite(config)
        self.update_model_params(self._hp.prior_model_params)
        super().__init__()

    def _default_hparams(self):
        default_dict = ParamDict(
            {
                "prior_model": None,  # prior model class
                "prior_model_params": None,  # parameters for the prior model
                "prior_model_checkpoint": None,  # checkpoint path of the prior model
                "prior_model_epoch": "latest",  # epoch that checkpoint should be loaded for (defaults to latest)
                "policy_model": None,  # policy model class in case we want to initialize different from prior
                "policy_model_params": None,  # parameters for the policy model
                "policy_model_checkpoint": None,  # checkpoint path of the policy model
                "policy_model_epoch": "latest",  # epoch that checkpoint should be loaded for (defaults to latest)
                "load_weights": True,  # optionally allows to *not* load the weights (ie train from scratch)
            }
        )
        return super()._default_hparams().overwrite(default_dict)

    def forward(self, obs, extra_info):
        with no_batchnorm_update(self):  # BN updates harm the initialized policy
            return super().forward(obs, extra_info)

    def _build_network(self):
        if self._hp.policy_model is not None:
            net = self._hp.policy_model(self._hp.policy_model_params, None)
        else:
            net = self._hp.prior_model(self._hp.prior_model_params, None)
        if self._hp.load_weights:
            if self._hp.policy_model is not None:
                BaseAgent.load_model_weights(
                    net, self._hp.policy_model_checkpoint, self._hp.prior_model_epoch
                )
            else:
                BaseAgent.load_model_weights(
                    net, self._hp.prior_model_checkpoint, self._hp.prior_model_epoch
                )
        return net

    def _compute_action_dist(self, obs, extra_info):
        return self.net.compute_learned_prior(obs, first_only=True)

    def sample_rand(self, obs, extra_info):
        if len(obs.shape) == 1:
            output_dict = self.forward(obs[None], extra_info)
            output_dict.action = output_dict.action[0]
            return output_dict
        return self.forward(
            obs, extra_info
        )  # for prior-initialized policy we run policy directly for rand sampling from prior

    @staticmethod
    def update_model_params(params):
        # TODO: the device could be set to cpu even if GPU available
        params.device = (
            torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
        )
        params.batch_size = 1  # run only single-element batches for forward pass


class PriorAugmentedPolicy(Policy):
    """Augments policy output with prior divergence."""

    def _default_hparams(self):
        default_dict = ParamDict(
            {
                "max_divergence_range": 100,  # range at which prior divergence gets clipped
            }
        )
        return super()._default_hparams().overwrite(default_dict)

    def forward(self, obs, extra_info):
        policy_output = super().forward(obs, extra_info)
        if not self._rollout_mode:
            (
                raw_prior_divergence,
                policy_output.prior_dist,
            ) = self._compute_prior_divergence(policy_output, obs)
            policy_output.prior_divergence = self.clamp_divergence(raw_prior_divergence)
        return policy_output

    def clamp_divergence(self, divergence):
        return torch.clamp(
            divergence, -self._hp.max_divergence_range, self._hp.max_divergence_range
        )

    def _compute_prior_divergence(self, policy_output, obs):
        raise NotImplementedError


class LearnedPriorAugmentedPolicy(PriorAugmentedPolicy):
    """Augments policy output with divergence to learned prior distribution."""

    def __init__(self, config):
        self._hp = self._default_hparams().overwrite(config)
        PriorInitializedPolicy.update_model_params(self._hp.prior_model_params)
        PriorAugmentedPolicy.__init__(self)  # super().__init__()
        if self._hp.prior_batch_size > 0:
            self._hp.prior_model_params.batch_size = self._hp.prior_batch_size
        self.prior_net = self._hp.prior_model(self._hp.prior_model_params, None)
        BaseAgent.load_model_weights(
            self.prior_net, self._hp.prior_model_checkpoint, self._hp.prior_model_epoch
        )

    def _default_hparams(self):
        default_dict = ParamDict(
            {
                "prior_model": None,  # prior model class
                "prior_model_params": None,  # parameters for the prior model
                "prior_model_checkpoint": None,  # checkpoint path of the prior model
                "prior_model_epoch": "latest",  # epoch that checkpoint should be loaded for (defaults to latest)
                "prior_batch_size": -1,  # optional: use separate batch size for prior network
                "reverse_KL": False,  # if True, computes KL[q||p] instead of KL[p||q] (can be more stable to opt)
                "analytic_KL": False,  # if True, computes KL divergence analytically, otherwise sampling based
                "num_mc_samples": 10,  # number of samples for monte-carlo KL estimate
            }
        )
        return super()._default_hparams().overwrite(default_dict)

    def _compute_prior_divergence(self, policy_output, obs):
        with no_batchnorm_update(self.prior_net):
            prior_dist = self.prior_net.compute_learned_prior(
                obs, first_only=True
            ).detach()
            if self._hp.analytic_KL:
                return self._analytic_divergence(policy_output, prior_dist), prior_dist
            return self._mc_divergence(policy_output, prior_dist), prior_dist

    def _analytic_divergence(self, policy_output, prior_dist):
        """Analytic KL divergence between two Gaussian distributions."""
        assert isinstance(prior_dist, MultivariateGaussian) and isinstance(
            policy_output.dist, MultivariateGaussian
        )
        if self._hp.reverse_KL:
            return prior_dist.kl_divergence(policy_output.dist).sum(dim=-1)
        else:
            return policy_output.dist.kl_divergence(prior_dist).sum(dim=-1)

    def _mc_divergence(self, policy_output, prior_dist):
        """Monte-Carlo KL divergence estimate."""
        if self._hp.reverse_KL:
            return mc_kl_divergence(
                prior_dist, policy_output.dist, n_samples=self._hp.num_mc_samples
            )
        else:
            return mc_kl_divergence(
                policy_output.dist, prior_dist, n_samples=self._hp.num_mc_samples
            )

    def sample_rand(self, obs, extra_info):
        with torch.no_grad():
            with no_batchnorm_update(self.prior_net):
                prior_dist = self.prior_net.compute_learned_prior(
                    obs, first_only=True
                ).detach()
        action = prior_dist.sample()
        action, log_prob = self._tanh_squash_output(action, 0)  # ignore log_prob output
        return AttrDict(action=action, log_prob=log_prob)


class LearnedPriorAugmentedPIPolicy(
    PriorInitializedPolicy, LearnedPriorAugmentedPolicy
):
    def __init__(self, config):
        LearnedPriorAugmentedPolicy.__init__(self, config)

    def forward(self, obs, extra_info):
        with no_batchnorm_update(self):
            return LearnedPriorAugmentedPolicy.forward(self, obs, extra_info)


class LanguageLearnedPriorAugmentedPIPolicy(LearnedPriorAugmentedPIPolicy):
    # TODO: this is a hacky way to get the language embedding into the policy while remaining backwards compatible
    def _prep_inputs(self, obs, extra_info):
        # this follows skill_prior_mdl.py's def _learned_prior_input in the state based model
        assert "lang" in extra_info
        inputs = AttrDict(
            states=obs,
            lang=extra_info.lang.squeeze(1),
        )
        return inputs

    def forward(self, obs, extra_info):
        return super().forward(self._prep_inputs(obs, extra_info), extra_info)

    # def _compute_action_dist(self, obs, extra_info):
    #    return super()._compute_action_dist(
    #        self._prep_inputs(obs, extra_info), extra_info
    #    )

    # def sample_rand(self, obs, extra_info):
    #    return super().sample_rand(self._prep_inputs(obs, extra_info), extra_info)


class ACPriorInitializedPolicy(PriorInitializedPolicy):
    """PriorInitializedPolicy for case with separate prior obs --> uses prior observation as input only."""

    def forward(self, obs, extra_info):
        return super().forward(self.net.unflatten_obs(obs).prior_obs, extra_info)


class ACLanguagePriorInitializedPolicy(PriorInitializedPolicy):
    """LanguageLearnedPriorAugmentedPIPolicy for case with separate prior obs --> uses prior observation as input only."""

    def _prep_inputs(self, obs, extra_info):
        # this follows skill_prior_mdl.py's def _learned_prior_input in the Image Based model
        assert "lang" in extra_info
        return AttrDict(
            images=obs,
            lang=extra_info.lang.squeeze(1),
            shape=obs.shape,  # for backwards compat
        )

    def forward(self, obs, extra_info):
        if obs.shape[0] == 1:
            return super().forward(
                self._prep_inputs(self.net.unflatten_obs(obs).prior_obs, extra_info),
                extra_info,
            )  # use policy_net or batch_size 1 inputs
        return super().forward(self._prep_inputs(self.net.unflatten_obs(obs).prior_obs, extra_info), extra_info)


class ACLearnedPriorAugmentedPIPolicy(LearnedPriorAugmentedPIPolicy):
    """LearnedPriorAugmentedPIPolicy for case with separate prior obs --> uses prior observation as input only."""

    def forward(self, obs, extra_info):
        if obs.shape[0] == 1:
            return super().forward(
                self.net.unflatten_obs(obs).prior_obs, extra_info
            )  # use policy_net or batch_size 1 inputs
        return super().forward(self.prior_net.unflatten_obs(obs).prior_obs, extra_info)


class ACLanguageLearnedPriorAugmentedPIPolicy(LanguageLearnedPriorAugmentedPIPolicy):
    """LanguageLearnedPriorAugmentedPIPolicy for case with separate prior obs --> uses prior observation as input only."""

    def __init__(self, config):
        super().__init__(config)  # this is fsr necessary for it not to throw an error

    def _prep_inputs(self, obs, extra_info):
        # this follows skill_prior_mdl.py's def _learned_prior_input in the Image Based model
        assert "lang" in extra_info
        return AttrDict(
            images=obs,
            lang=extra_info.lang.squeeze(1),
            shape=obs.shape,  # for backwards compat
        )

    def forward(self, obs, extra_info):
        if obs.shape[0] == 1:
            return super().forward(
                self.net.unflatten_obs(
                    self._prep_inputs(obs, extra_info).images
                ).prior_obs,
                extra_info,
            )  # use policy_net or batch_size 1 inputs
        return super().forward(self.prior_net.unflatten_obs(obs).prior_obs, extra_info)


class ACLearnedPriorAugmentedPolicy(LearnedPriorAugmentedPolicy):
    """LearnedPriorAugmentedPolicy for case with separate prior obs."""

    def __init__(self, config):
        super().__init__(config)  # this is fsr necessary for it not to throw an error

    def _compute_prior_divergence(self, policy_output, obs):
        return super()._compute_prior_divergence(
            policy_output, self.prior_net.unflatten_obs(obs).prior_obs
        )

    def sample_rand(self, obs, extra_info):
        return super().sample_rand(
            self.prior_net.unflatten_obs(obs).prior_obs, extra_info
        )


class ACLearnedPriorAugmentedMLPPolicy(
    ACLearnedPriorAugmentedPolicy, SplitObsMLPPolicy
):
    """LearnedPriorAugmentedPolicy for case with separate prior obs using MLP policy net."""

    def __init__(self, config):
        SplitObsMLPPolicy.__init__(self, config)
        ACLearnedPriorAugmentedPolicy.__init__(
            self, self._hp.overwrite(config)
        )  # this is fsr necessary for it not to throw an error


class ACLearnedPriorAugmentedHybridConvMLPPolicy(
    ACLearnedPriorAugmentedPolicy, HybridConvMLPPolicy
):
    """LearnedPriorAugmentedPolicy for case with separate prior obs using HybridConvMLP policy net."""

    def __init__(self, config):
        HybridConvMLPPolicy.__init__(self, config)
        ACLearnedPriorAugmentedPolicy.__init__(
            self, self._hp.overwrite(config)
        )  # this is fsr necessary for it not to throw an error
