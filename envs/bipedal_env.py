import gymnasium as gym
import cv2


class WrappedEnv(gym.Wrapper):
    def __init__(self, env, config):
        super().__init__(env)
        self.env = env
        self.full_image_size = config["obs_size"]

    def reset(self):
        obs = self.env.reset()
        return obs

    def step(self, action):
        result = self.env.step(action)
        #print(result)
        next_state, reward, done, _, info = result
        return next_state, reward, done, info


def make_env(config):
    env = WrappedEnv(gym.make(config["env_name"]), config)
    return env
