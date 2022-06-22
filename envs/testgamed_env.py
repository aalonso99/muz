from envs.env_utils import ASpace

import numpy as np


class TestEnvD:
    def __init__(self):
        self.counter = 0
        self.action_space = ASpace(2)
        self.over = False

    def step(self, action):
        self.counter += 1

        img = np.full([4], self.counter % 7, dtype=np.float32)
        img[0] = self.counter
        reward = (self.counter - 1) % 7
        if self.counter > 50:
            self.over = True

        return img, reward, self.over, False

    def reset(self):
        self.counter = 0
        self.over = 0
        return np.full([4], self.counter % 7, dtype=np.float32)

    def close(self):
        pass


def make_env(config):
    return TestEnvD()
