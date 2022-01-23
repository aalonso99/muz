import math
import random

import torch
from torch import nn


class MCTS:
    def __init__(
        self, action_size, obs_size, mu_net,
    ):
        self.action_size = action_size
        self.obs_size = obs_size
        self.max_reward = 1
        self.min_reward = 0
        self.mu_net = mu_net

    def search(self, n_simulations, current_frame):
        # with torch.no_grad():
        #     model.eval()

        frame_t = torch.tensor(current_frame).unsqueeze(0)
        init_latent = self.mu_net.represent(frame_t)[0]
        init_policy, init_val = self.mu_net.predict(init_latent.unsqueeze(0))
        root_node = TreeNode(
            latent=init_latent,
            action_size=self.action_size,
            val_pred=init_val[0],
            pol_pred=init_policy[0],
        )

        for i in range(n_simulations):
            current_node = root_node
            new_node = False

            while not new_node:
                value_pred, policy_pred, latent = (
                    current_node.val_pred,
                    current_node.pol_pred,
                    current_node.latent,
                )
                action = current_node.pick_action()
                if current_node.children[action] is None:
                    action_t = nn.functional.one_hot(
                        torch.tensor(action), num_classes=self.action_size
                    )
                    new_latent, reward = [
                        x[0] for x in
                        self.mu_net.dynamics(action_t.unsqueeze(0), latent.unsqueeze(0))
                    ]
                    
                    new_policy, new_val = self.mu_net.predict(new_latent.unsqueeze(0))
                    current_node.insert(
                        action_n=action,
                        latent=new_latent,
                        val_pred=new_val[0],
                        pol_pred=new_policy[0],
                    )
                    new_node = True
                else:
                    current_node = current_node.children[action]
        return root_node
    
    def train(self, batch):
        loss = 0
        for image, action, targets in batch:
            target_value, target_reward, target_policy = targets
            hidden_state = self.mu_net.represent(torch.tensor(image).unsqueeze(0))

            one_hot_action = nn.functional.one_hot(
                torch.tensor([action]).to(dtype=torch.int64), 
                num_classes=self.action_size
            )
            _, pred_reward = self.mu_net.dynamics(hidden_state, one_hot_action)
            pred_policy, pred_value = [x[0] for x in self.mu_net.predict(hidden_state)]
            
            policy_loss = torch.einsum('i,j->', pred_policy, torch.tensor(target_policy))
            value_loss = torch.abs(pred_value - target_value) ** 2
            reward_loss = torch.abs(pred_reward - target_reward) ** 2
            
            loss += policy_loss + value_loss + reward_loss

        self.mu_net.optimizer.zero_grad()
        loss.backward()
        self.mu_net.optimizer.step()

        return loss


class TreeNode:
    def __init__(self, latent, action_size, val_pred=None, pol_pred=None, parent=None):
        self.action_size = action_size
        self.children = [None] * action_size
        self.latent = latent
        self.val_pred = val_pred
        self.pol_pred = pol_pred
        self.parent = parent
        self.average_val = 0
        self.num_visits = 0

    def insert(self, action_n, latent, val_pred, pol_pred):
        if self.children[action_n] is None:
            new_child = TreeNode(
                latent=latent,
                val_pred=val_pred,
                pol_pred=pol_pred,
                action_size=self.action_size,
                parent=self,
            )

            self.children[action_n] = new_child
            self.increment()
            self.update_val(val_pred)

        else:
            raise ValueError("This node has already been traversed")

    def increment(self):
        self.num_visits += 1
        if self.parent:
            self.parent.increment()

    def update_val(self, curr_val, discount=1):
        nmtr = self.average_val * self.num_visits + (curr_val + self.val_pred)
        dnmtr = self.num_visits + 1
        self.average_val = nmtr / dnmtr

        if self.parent is not None:
            self.parent.update_val(
                curr_val * discount
            )  # send this down to the parent so that it also updates

    def action_score(self, action_n, total_visit_count):
        c1 = 1.25
        c2 = 19652

        child = self.children[action_n]

        n = child.num_visits if child else 0
        q = child.average_val if child else 0

        p = self.pol_pred[action_n]

        vis_frac = math.sqrt(total_visit_count) / (1 + n)
        balance_term = c1 + math.log((total_visit_count + c2 + 1) / c2)

        score = q + (p * vis_frac * balance_term)
        return score

    def pick_action(self):
        total_visit_count = sum([a.num_visits if a else 0 for a in self.children])

        scores = [
            self.action_score(a, total_visit_count) for a in range(self.action_size)
        ]

        return scores.index(max(scores))


if __name__ == "__main__":
    pass
