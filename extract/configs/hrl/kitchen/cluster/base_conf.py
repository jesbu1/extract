import os
import copy

from extract.utils.general_utils import AttrDict
from extract.rl.components.agent import MixedIntervalHierarchicalAgent

# from extract.rl.policies.mlp_policies import SplitObsMLPPolicy
from extract.rl.policies.hybrid_policies import ACHybridLearnedPriorAugmentedPIPolicy
from extract.rl.components.critic import ConvCritic
from extract.rl.envs.kitchen import KitchenEnv
from extract.rl.components.sampler import MultiImageAugmentedHierarchicalSampler
from extract.rl.components.replay_buffer import IntImageUniformReplayBuffer
from extract.rl.agents.hybrid_skill_space_agent import (
    HybridSkillSpaceActionPriorSACAgent,
)
from extract.rl.agents.variable_length_skill_space_agent import (
    VariableLengthACSkillSpaceAgent,
)
from extract.models.cluster_skill_prior_mdl import ImageClusterSkillPriorMdl
from extract.configs.default_data_configs.kitchen import data_spec

current_dir = os.path.dirname(os.path.realpath(__file__))

notes = "hierarchical RL on the kitchen env"

configuration = {
    "seed": 42,
    "agent": MixedIntervalHierarchicalAgent,
    "environment": KitchenEnv,
    "sampler": MultiImageAugmentedHierarchicalSampler,
    "data_dir": ".",
    "num_epochs": 60,
    "max_rollout_len": 280,
    "n_steps_per_epoch": 50000,
    "n_warmup_steps": 1e3,  # 2e3,
}
configuration = AttrDict(configuration)


# Replay Buffer
replay_params = AttrDict(
    capacity=50000,
)

# Observation Normalization
obs_norm_params = AttrDict()

sampler_config = AttrDict(
    n_frames=4,
)

base_agent_params = AttrDict(
    batch_size=256,
    replay=IntImageUniformReplayBuffer,  # save memory by saving as np.uint8
    replay_params=replay_params,
    # obs_normalizer=Normalizer,
    # obs_normalizer_params=obs_norm_params,
    clip_q_target=True,
)


###### Low-Level ######
# LL Policy
ll_model_params = AttrDict(
    state_dim=data_spec.state_dim,
    action_dim=data_spec.n_actions,
    kl_div_weight=5e-4,
    nz_enc=128,
    nz_mid=128,
    nz_vae=5,
    n_input_frames=4,
    # n_lstm_layers=1,
    prior_input_res=data_spec.res,
    num_prior_net_layers=4,
    encoder_max_range=2.0,
    n_skills=None,  # FILL IN
    max_rollout_steps=30,
    skill_progress_termination_threshold=1.0,
)

# LL Agent
ll_agent_config = copy.deepcopy(base_agent_params)
ll_agent_config.update(
    AttrDict(
        model=ImageClusterSkillPriorMdl,
        model_params=ll_model_params,
        model_checkpoint=os.path.join(
            os.environ["EXP_DIR"],
            "skill_prior_learning/kitchen_image/hierarchical_cluster/",
        ),
    )
)


###### High-Level ########
# HL Policy
hl_policy_params = AttrDict(
    action_dim=None,  # ll_model_params.nz_vae
    # + ll_model_params.n_skills,  # z-dimension of the skill VAE + # skills the decoder can condition on
    input_dim=data_spec.state_dim,
    max_action_range=2.0,  # prior is Gaussian with unit variance
    nz_mid=256,
    n_layers=5,
    # unused_obs_size=ll_model_params.prior_input_res**2
    # * 3
    # * ll_model_params.n_input_frames,
)

# HL Critic
hl_critic_params = AttrDict(
    action_dim=ll_model_params.nz_vae,
    input_dim=hl_policy_params.input_dim,
    input_res=ll_model_params.prior_input_res,
    input_nc=3 * ll_model_params.n_input_frames,
    output_dim=ll_model_params.n_skills,
    n_layers=5,  # number of policy network layers
    nz_mid=256,
    action_input=True,
    # unused_obs_size=hl_policy_params.unused_obs_size,
)

# HL Agent
hl_agent_config = copy.deepcopy(base_agent_params)
hl_agent_config.update(
    AttrDict(
        policy=ACHybridLearnedPriorAugmentedPIPolicy,
        policy_params=hl_policy_params,
        critic=ConvCritic,
        critic_params=hl_critic_params,
        fixed_alpha=None,
        fixed_alpha_d=None,
        discount_factor=0.99,
    )
)


##### Joint Agent #######
agent_config = AttrDict(
    hl_agent=HybridSkillSpaceActionPriorSACAgent,
    hl_agent_params=hl_agent_config,
    ll_agent=VariableLengthACSkillSpaceAgent,
    ll_agent_params=ll_agent_config,
    log_video_caption=True,
)

# Dataset - Random data
data_config = AttrDict()
data_config.dataset_spec = data_spec

# Environment
env_config = AttrDict(
    reward_norm=1.0,
    screen_height=ll_model_params.prior_input_res,
    screen_width=ll_model_params.prior_input_res,
)

# reduce replay capacity because we are training image-based, do not dump (too large)
from extract.rl.components.replay_buffer import SplitObsUniformReplayBuffer

agent_config.ll_agent_params.replay = SplitObsUniformReplayBuffer
# agent_config.ll_agent_params.replay_params.unused_obs_size = (
#    ll_model_params.prior_input_res**2 * 3 * 2
#    + hl_agent_config.policy_params.action_dim
# )  # ignore HL action
# agent_config.ll_agent_params.replay_params.dump_replay = False
# agent_config.hl_agent_params.replay_params.dump_replay = False