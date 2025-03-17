from cleanil.utils import (
    load_yaml, 
    set_seed, 
    get_device, 
    get_logger, 
    LoggerConfig,
)
from cleanil.data import (
    load_d4rl_expert_trajs, 
    load_d4rl_replay_buffer, 
    combine_data,
    normalize, 
    train_test_split,
)
from cleanil.envs.utils import make_environment, EnvConfig
from cleanil.il import recoil
from cleanil.rl.actor import make_tanh_normal_actor
from cleanil.rl.critic import DoubleQNetwork
from torchrl.data import (
    LazyTensorStorage, 
    ReplayBuffer, 
    SamplerWithoutReplacement,
)
from torchrl.envs.transforms import ObservationNorm
from cleanil.envs.termination import get_termination_fn

def main():
    config = load_yaml()
    env_config = EnvConfig(**config["env"])
    algo_config = recoil.RECOILConfig(**config["algo"])

    set_seed(config["seed"])
    device = get_device(config["device"])
    logger = get_logger(config, LoggerConfig(**config["logger"]))
    
    print("device", device)

    # load expert data
    expert_data = load_d4rl_expert_trajs(
        algo_config.dataset,
        algo_config.num_expert_trajs,
        skip_terminated=True,
    )
    expert_data = expert_data.to(device)
    expert_data, eval_data = train_test_split(expert_data, algo_config.train_ratio)

    # load offline data
    buffer = load_d4rl_replay_buffer(
        algo_config.transition_dataset, 
        algo_config.batch_size,
    )
    buffer.set_sampler(SamplerWithoutReplacement())
    data = buffer.sample(batch_size=algo_config.transition_data_size)
    data = data.to(device)

    print("transition data size", len(data))

    # combine data, maybe upsample expert data for policy learning
    data = combine_data(expert_data, data, algo_config.upsample)

    # update termination
    termination_fn = get_termination_fn(env_config.name)
    data["done"] = termination_fn(data["observation"]).float()
    data["next"]["done"] = termination_fn(data["next"]["observation"]).float()

    # normalize data
    obs_mean = data["observation"].mean(0)
    obs_std = data["observation"].std(0)
    expert_data["observation"] = normalize(expert_data["observation"], obs_mean, obs_std**2)
    expert_data["next"]["observation"] = normalize(expert_data["next"]["observation"], obs_mean, obs_std**2)
    eval_data["observation"] = normalize(eval_data["observation"], obs_mean, obs_std**2)
    eval_data["next"]["observation"] = normalize(eval_data["next"]["observation"], obs_mean, obs_std**2)
    data["observation"] = normalize(data["observation"], obs_mean, obs_std**2)
    data["next"]["observation"] = normalize(data["next"]["observation"], obs_mean, obs_std**2)
    
    # setup environments
    _, eval_env = make_environment(env_config, device)
    transform = ObservationNorm(
        obs_mean, obs_std, 
        in_keys=["observation"], out_keys=["observation"],
        standard_normal=True,
    )
    eval_env.append_transform(transform)

    # setup agent
    observation_spec = eval_env.observation_spec["observation"]
    action_spec = eval_env.action_spec
    obs_dim = observation_spec.shape[-1]
    act_dim = action_spec.shape[-1]
    actor = make_tanh_normal_actor(
        obs_dim, 
        act_dim, 
        algo_config.hidden_dims, 
        algo_config.activation,
        action_spec.space.low,
        action_spec.space.high,
    )
    critic = DoubleQNetwork(
        obs_dim + 1, # add terminal flag
        act_dim,
        algo_config.hidden_dims, 
        algo_config.activation,
    )
    actor.to(device)
    critic.to(device)

    # make buffers
    expert_buffer = ReplayBuffer(
        storage=LazyTensorStorage(
            len(expert_data), 
            device=device,
        )
    )
    expert_buffer.extend(expert_data)

    eval_buffer = ReplayBuffer(
        storage=LazyTensorStorage(
            len(eval_data), 
            device=device,
        )
    )
    eval_buffer.extend(eval_data)

    transition_buffer = ReplayBuffer(
        storage=LazyTensorStorage(
            algo_config.buffer_size, 
            device=device,
        )
    )
    transition_buffer.extend(data)

    # setup trainer
    trainer = recoil.Trainer(
        algo_config,
        actor,
        critic,
        expert_buffer,
        transition_buffer,
        eval_buffer,
        obs_mean,
        obs_std,
        eval_env,
        logger,
        device,
    )
    trainer.train()

if __name__ == "__main__":
    main()