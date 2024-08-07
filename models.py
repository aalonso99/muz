import math

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
import random

import cv2

from dnd_kdtree import DND

from itertools import product


def conv3x3(in_channels, out_channels, stride=1):
    return torch.nn.Conv2d(
        in_channels, out_channels, kernel_size=3, stride=stride, padding=1, bias=False
    )


class ResBlockSmall(torch.nn.Module):
    def __init__(self, num_channels, stride=1, dropout=0.2):
        super().__init__()
        self.conv1 = conv3x3(num_channels, num_channels, stride)
        nn.init.kaiming_normal_(self.conv1.weight)
        self.bn1 = torch.nn.BatchNorm2d(num_channels)
        self.conv2 = conv3x3(num_channels, num_channels)
        nn.init.kaiming_normal_(self.conv2.weight)
        self.bn2 = torch.nn.BatchNorm2d(num_channels)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)

    def forward(self, x):
        out = self.dropout1(x)
        out = self.conv1(out)
        out = self.bn1(out)
        out = F.relu(out)
        out = self.dropout2(out)
        out = self.conv2(out)
        out = self.bn2(out)
        out += x
        out = F.relu(out)
        return out


class CartRepr(nn.Module):
    def __init__(self, obs_size, latent_size):
        super().__init__()
        self.obs_size = obs_size
        self.fc1 = nn.Linear(obs_size, latent_size)
        self.fc2 = nn.Linear(latent_size, latent_size)

    def forward(self, state):
        assert state.dim() == 2
        assert state.shape[1] == self.obs_size
        state = state.to(dtype=torch.float32)
        out = self.fc1(state)
        out = torch.relu(out)
        out = self.fc2(out)
        return out


class CartDyna(nn.Module):
    def __init__(self, action_size, latent_size, support_width):
        self.latent_size = latent_size
        self.action_size = action_size
        self.full_width = (2 * support_width) + 1
        super().__init__()
        self.fc1 = nn.Linear(latent_size + action_size, latent_size)
        self.fc2 = nn.Linear(latent_size, latent_size + self.full_width)

    def forward(self, latent, action):
        assert latent.dim() == 2 and action.dim() == 2
        assert (
            latent.shape[1] == self.latent_size and action.shape[1] == self.action_size
        )

        out = torch.cat([action, latent], dim=1)
        out = self.fc1(out)
        out = torch.relu(out)
        out = self.fc2(out)
        new_latent = out[:, : self.latent_size]
        reward_logits = out[:, self.latent_size :]
        return new_latent, reward_logits


class CartDynaLSTM(nn.Module):
    def __init__(self, action_size, latent_size, support_width, lstm_hidden_size):
        self.latent_size = latent_size
        self.action_size = action_size
        self.full_width = (2 * support_width) + 1
        super().__init__()

        self.lstm = nn.LSTM(input_size=self.latent_size, hidden_size=lstm_hidden_size)
        self.fc1 = nn.Linear(latent_size + action_size, latent_size)
        self.fc2 = nn.Linear(latent_size, latent_size)
        self.fc3 = nn.Linear(lstm_hidden_size, self.full_width)

    def forward(self, latent, action, lstm_hiddens):
        assert latent.dim() == 2 and action.dim() == 2
        assert latent.shape[1] == self.latent_size, \
               "latent.shape[1] = {}, but latent_size = {}".format(latent.shape[1], self.latent_size)
        assert action.shape[1] == self.action_size, \
               "action.shape[1] = {}, but action_size = {}".format(action.shape[1], self.action_size)
        

        out = torch.cat([action, latent], dim=1)
        out = self.fc1(out)
        out = torch.relu(out)
        new_latent = self.fc2(out)
        lstm_input = new_latent.unsqueeze(0)
        val_prefix_logits, new_hiddens = self.lstm(lstm_input, lstm_hiddens)
        val_prefix_logits = val_prefix_logits.squeeze(0)
        val_prefix_logits = self.fc3(val_prefix_logits)
        return new_latent, val_prefix_logits, new_hiddens


class CartPred(nn.Module):
    def __init__(self, action_size, latent_size, support_width):
        super().__init__()
        self.action_size = action_size
        self.latent_size = latent_size
        self.full_width = (support_width * 2) + 1
        self.fc1 = nn.Linear(latent_size, latent_size)
        self.fc_policy = nn.Linear(latent_size, action_size)
        self.fc_value = nn.Linear(latent_size, self.full_width)

    def forward(self, latent):
        assert latent.dim() == 2
        assert latent.shape[1] == self.latent_size
        out = self.fc1(latent)
        out = torch.relu(out)
        policy_logits = self.fc_policy(out)
        value_logits = self.fc_value(out)
        return policy_logits, value_logits


class MuZeroCartNet(nn.Module):
    def __init__(self, action_size: int, obs_size, config: dict):
        super().__init__()
        self.config = config
        self.action_size = action_size
        self.action_dim = 1
        self.obs_size = obs_size
        self.latent_size = config["latent_size"]
        self.support_width = config["support_width"]
        
        self.possible_actions = [ 0, 1 ]

        self.pred_net = CartPred(self.action_size, self.latent_size, self.support_width)

        if self.config["value_prefix"]:
            self.lstm_hidden_size = self.config["lstm_hidden_size"]
            self.dyna_net = CartDynaLSTM(
                self.action_size,
                self.latent_size,
                self.support_width,
                self.lstm_hidden_size,
            )
        else:
            self.dyna_net = CartDyna(
                self.action_size, self.latent_size, self.support_width
            )
        self.repr_net = CartRepr(self.obs_size, self.latent_size)

        self.policy_loss = nn.CrossEntropyLoss(reduction="none")
        self.reward_loss = nn.CrossEntropyLoss(reduction="none")
        self.value_loss = nn.CrossEntropyLoss(reduction="none")
        self.cos_sim = nn.CosineSimilarity(dim=1)

    def consistency_loss(self, x1, x2):
        assert x1.shape == x2.shape
        return -self.cos_sim(x1, x2)

    def init_optim(self, lr):
        params = (
            list(self.pred_net.parameters())
            + list(self.dyna_net.parameters())
            + list(self.repr_net.parameters())
        )
        self.optimizer = torch.optim.SGD(
            params,
            lr=lr,
            weight_decay=self.config["weight_decay"],
            momentum=self.config["momentum"],
        )

    def predict(self, latent):
        policy, value = self.pred_net(latent)
        return policy, value

    def dynamics(self, latent, action, hiddens=None):
        if self.config["value_prefix"]:
            latent, val_prefix, new_hiddens = self.dyna_net(latent, action, hiddens)
            return latent, val_prefix, new_hiddens
        else:
            latent, reward = self.dyna_net(latent, action)
            return latent, reward

    def represent(self, observation):
        latent = self.repr_net(observation)
        return latent
        
        
class CartNECPred(nn.Module):
    def __init__(self, action_size, latent_size,
    			 k=50,
                 max_size = 1000,
                 kdtree_rebuild = 50,
                 leaf_size=30,
                 delta=0.001):
        super().__init__()
        self.action_size = action_size
        self.latent_size = latent_size
        self.fc1 = nn.Linear(latent_size, latent_size)
        self.fc_policy = nn.Linear(latent_size, action_size) # Policy head
        self.fc_value_embedding = nn.Linear(latent_size, latent_size) # First layer of value head
        self.dnd = DND(latent_size, k, max_size, kdtree_rebuild, leaf_size)
        self.delta = delta
        
    def compute_value(self, latent, neighbors_repr, neighbors_value):
        dists = torch.squeeze(torch.cdist(torch.unsqueeze(latent,1), neighbors_repr))

        k = 1.0/(dists+self.delta)
        w = k/k.sum()
        
        value = torch.sum(neighbors_value * w, dim=1)
        
        return value

    def forward(self, latent):
        assert latent.dim() == 2
        assert latent.shape[1] == self.latent_size
        out = self.fc1(latent)
        out = torch.relu(out)
        policy_logits = self.fc_policy(out)
        
        if not self.dnd.available:
            
            # print("DND not available")
            return policy_logits, torch.zeros(latent.shape[0],1)
            # return policy_logits, torch.normal(0.0,1.0,size=(latent.shape[0],1))
        
        else:
        
            # Computation of the value using neural episodic control https://arxiv.org/pdf/1703.01988.pdf
            out = self.fc_value_embedding(out)

            _, knn_indices = self.dnd.query_knn(out.detach().numpy())
            neighbors_repr = []
            neighbors_value = []
            
            for neighbor_indices in knn_indices:
            
                reprs_row = []
                values_row = []
                for i in neighbor_indices:
                    # Since the kdtree is not rebuilt every time there are changes in the memory_table, it 
                    # can point us to neighbors that don't exist any more. In that case, we simply ignore
                    # them (one alternative is to look for it first in the memory_table keys, another is to
                    # rebuild the kdtree every time an element is removed)
                    try:
                        reprs_row.append(self.dnd.memory_table[i][0])
                    except:
                        print("Not found index " + str(i))
                        print("Max index in memory: " + str(max(self.dnd.memory_table.keys())))
                        print("Min index in memory: " + str(min(self.dnd.memory_table.keys())))
                        continue
                    
                    try:
                        values_row.append(self.dnd.memory_table[i][1])
                    except:
                        reprs_row.pop()
                        
                neighbors_repr.append(reprs_row)
                neighbors_value.append(values_row)
                
            neighbors_repr = torch.tensor(np.array(neighbors_repr), requires_grad=False)
            neighbors_value = torch.tensor(np.array(neighbors_value), requires_grad=False)

            value_logits = self.compute_value(out, neighbors_repr, neighbors_value)
        
        return policy_logits, value_logits
        
        
class MuZeroNECCartNet(nn.Module):
	# If a weights_path is passed, the weights of a pretrained MuZeroCartNet are 
	# loaded from that path into the compatible layers of this network
    def __init__(self, action_size: int, obs_size, config: dict, 
    			 k=5,
                 max_size: int = 1500,
                 kdtree_rebuild: int = 20,
                 leaf_size: int = 10,
                 delta: float = 0.001,
                 weights_path: str = None):
                 
        super().__init__()
        self.config = config
        self.action_size = action_size
        self.action_dim = 1
        self.obs_size = obs_size
        self.latent_size = config["latent_size"]
        self.support_width = config["support_width"]
        
        self.possible_actions = [ 0, 1 ]

        self.pred_net = CartNECPred(self.action_size, self.latent_size,
        						    k = k,
                 				    max_size = max_size,
                 				    kdtree_rebuild = kdtree_rebuild,
                 				    leaf_size = leaf_size,
                 				    delta = delta)

        if self.config["value_prefix"]:
            self.lstm_hidden_size = self.config["lstm_hidden_size"]
            self.dyna_net = CartDynaLSTM(
                self.action_size,
                self.latent_size,
                self.support_width,
                self.lstm_hidden_size,
            )
        else:
            self.dyna_net = CartDyna(
                self.action_size, self.latent_size, self.support_width
            )
        self.repr_net = CartRepr(self.obs_size, self.latent_size)
        
        if weights_path != None:
            # Load pretrained weights (only layers present in this model too)
            pretrained_state_dict = torch.load(weights_path)
            pretrained_state_dict = {k: v for k, v in pretrained_state_dict.items() if k in self.state_dict()}
            self.load_state_dict(pretrained_state_dict, strict=False)

            # Freeze pretrained layers
            self.freeze_weights(self.dyna_net)
            self.freeze_weights(self.repr_net)
            self.freeze_weights(self.pred_net)
            self.unfreeze_weights(self.pred_net.fc_policy)
            self.unfreeze_weights(self.pred_net.fc_value_embedding)

        self.policy_loss = nn.CrossEntropyLoss(reduction="none")
        self.reward_loss = nn.CrossEntropyLoss(reduction="none")
        self.value_loss = nn.CrossEntropyLoss(reduction="none")
        self.cos_sim = nn.CosineSimilarity(dim=1)
        
    def freeze_weights(self, model):
        for param in model.parameters():
            param.requires_grad = False
            
    def unfreeze_weights(self, model):
        for param in model.parameters():
            param.requires_grad = True
            
    def add_to_dnd(self, latent, value, observation=None, memory_object=None):
        dnd_latent = self.pred_net.fc1(latent)
        dnd_latent = torch.relu(dnd_latent)
        dnd_latent = self.pred_net.fc_value_embedding(dnd_latent)
        self.pred_net.dnd.add(dnd_latent.detach().numpy(), value.detach().numpy(), 
                              observations=observation, memory_object=memory_object)
        
    def save_dnd(self, path):
        self.pred_net.dnd.save(path)

    def load_dnd(self, path):
        self.pred_net.dnd.load(path)
        
    def consistency_loss(self, x1, x2):
        assert x1.shape == x2.shape
        return -self.cos_sim(x1, x2)

    def init_optim(self, lr):
        params = (
            list(self.pred_net.parameters())
            + list(self.dyna_net.parameters())
            + list(self.repr_net.parameters())
        )
        self.optimizer = torch.optim.SGD(
            params,
            lr=lr,
            weight_decay=self.config["weight_decay"],
            momentum=self.config["momentum"],
        )

    def predict(self, latent):
        policy, value = self.pred_net(latent)
        return policy, value

    def dynamics(self, latent, action, hiddens=None):
        if self.config["value_prefix"]:
            latent, val_prefix, new_hiddens = self.dyna_net(latent, action, hiddens)
            return latent, val_prefix, new_hiddens
        else:
            latent, reward = self.dyna_net(latent, action)
            return latent, reward

    def represent(self, observation):
        latent = self.repr_net(observation)
        return latent
               
        
class BipedalRepr(nn.Module):
    def __init__(self, obs_size, latent_size):
        super().__init__()
        self.obs_size = obs_size
        self.fc1 = nn.Linear(obs_size, latent_size)
        self.fc2 = nn.Linear(latent_size, latent_size)

    def forward(self, state):
        #	print("State:" + str(state))
        assert state.dim() == 2
        assert state.shape[1] == self.obs_size
        state = state.to(dtype=torch.float32)
        out = self.fc1(state)
        out = torch.relu(out)
        out = self.fc2(out)
        return out


# class BipedalDyna(nn.Module):
#     def __init__(self, action_size, latent_size, support_width):
#         self.latent_size = latent_size
#         self.action_size = action_size
#         self.full_width = (2 * support_width) + 1
#         super().__init__()
#         self.fc1 = nn.Linear(latent_size + action_size, latent_size)
#         self.fc2 = nn.Linear(latent_size, latent_size + self.full_width)

#     def forward(self, latent, action):
#         assert latent.dim() == 2 and action.dim() == 2
#         assert (
#             latent.shape[1] == self.latent_size and action.shape[1] == self.action_size
#         )

#         out = torch.cat([action, latent], dim=1)
#         out = self.fc1(out)
#         out = torch.relu(out)
#         out = self.fc2(out)
#         new_latent = out[:, : self.latent_size]
#         reward_logits = out[:, self.latent_size :]
#         return new_latent, reward_logits
        
        
class BipedalDynaLSTM(nn.Module):
    def __init__(self, action_size, action_dim, latent_size, support_width, lstm_hidden_size):
        self.latent_size = latent_size
        self.action_size = action_size
        self.action_dim = action_dim
        self.full_width = (2 * support_width) + 1
        super().__init__()

        self.lstm = nn.LSTM(input_size=self.latent_size, hidden_size=lstm_hidden_size)
        #self.fc0 = nn.Linear(action_dim, latent_size) # Transforms action policy logits into latent space
        #self.fc1 = nn.Linear(2*latent_size, latent_size) # Processes action+state in latent space
        self.fc1 = nn.Linear(action_dim+latent_size, latent_size) # Processes action+state in latent space
        self.fc2 = nn.Linear(latent_size, latent_size)
        self.fc3 = nn.Linear(lstm_hidden_size, self.full_width)

    def forward(self, latent, action, lstm_hiddens):
        assert latent.dim() == 2 
        assert action.dim() == 2
        assert latent.shape[1] == self.latent_size 
        assert action.shape[1] == self.action_dim 

        #x = self.fc0(action.flatten())
        #x = torch.relu(x)
        #x = torch.cat([x, latent], dim=1)
        x = torch.cat([action, latent], dim=1)
        x = self.fc1(x)
        x = torch.relu(x)
        new_latent = self.fc2(x)
        lstm_input = new_latent.unsqueeze(0)
        val_prefix_logits, new_hiddens = self.lstm(lstm_input, lstm_hiddens)
        val_prefix_logits = val_prefix_logits.squeeze(0)
        val_prefix_logits = self.fc3(val_prefix_logits)
        return new_latent, val_prefix_logits, new_hiddens
          

class BipedalPred(nn.Module):
    def __init__(self, action_size, action_dim, latent_size, support_width):
        super().__init__()
        self.action_size = action_size
        self.action_dim = action_dim
        self.latent_size = latent_size
        self.full_width = (support_width * 2) + 1
        self.fc1 = nn.Linear(latent_size, latent_size)
        # self.fc2 = nn.Linear(latent_size, action_size + self.full_width)
        self.fcs_policy = [ nn.Linear(latent_size, action_size) 
                            for _ in range(self.action_dim) ] # BipedalWalker has 4 dimensions in the action space
        self.fc_value = nn.Linear(latent_size, self.full_width)

    def forward(self, latent):
        assert latent.dim() == 2
        assert latent.shape[1] == self.latent_size
        x = self.fc1(latent)
        x = torch.relu(x)
        # x = self.fc2(x)
        # policy_logits = x[:, : self.action_size]
        for fc in self.fcs_policy:
            fc.to(device=x.device)
        policy_logits = torch.stack([ fc(x) for fc in self.fcs_policy ], dim=1)
        # value_logits = x[:, self.action_size :]
        value_logits = self.fc_value(x)
        return policy_logits, value_logits
        

class MuZeroBipedalNet(nn.Module):
    def __init__(self, action_size: int, action_dim: int, obs_size, config: dict):
        super().__init__()
        self.config = config
        self.action_size = action_size
        self.action_dim = action_dim
        self.obs_size = obs_size
        self.latent_size = config["latent_size"]
        self.support_width = config["support_width"]

        dim_action_values = config["dim_action_values"]
        self.possible_actions = [ [dim_action_values[action_n_comp] for action_n_comp in action_n]
                                for action_n in product(range(self.action_size), repeat=self.action_dim) ]
        self.possible_actions_str = [ repr(action) for action in self.possible_actions ]
        self.possible_action_indices = [ action_idx for action_idx 
                                         in product(range(self.action_size), 
                                                    repeat=self.action_dim) ]

        self.pred_net = BipedalPred(self.action_size, self.action_dim, self.latent_size, self.support_width)

        if self.config["value_prefix"]:
            self.lstm_hidden_size = self.config["lstm_hidden_size"]
            self.dyna_net = BipedalDynaLSTM(
                self.action_size,
                self.action_dim,
                self.latent_size,
                self.support_width,
                self.lstm_hidden_size,
            )
        else:
            self.dyna_net = BipedalDyna(
                self.action_size, self.latent_size, self.support_width
            )
        self.repr_net = BipedalRepr(self.obs_size, self.latent_size)

        self.policy_loss = nn.CrossEntropyLoss(reduction="mean")
        self.reward_loss = nn.CrossEntropyLoss(reduction="none")
        self.value_loss = nn.CrossEntropyLoss(reduction="none")
        self.cos_sim = nn.CosineSimilarity(dim=1)

    def consistency_loss(self, x1, x2):
        assert x1.shape == x2.shape
        return -self.cos_sim(x1, x2)

    def init_optim(self, lr):
        params = (
            list(self.pred_net.parameters())
            + list(self.dyna_net.parameters())
            + list(self.repr_net.parameters())
        )
        self.optimizer = torch.optim.SGD(
            params,
            lr=lr,
            weight_decay=self.config["weight_decay"],
            momentum=self.config["momentum"],
        )

    def predict(self, latent):
        policy, value = self.pred_net(latent)
        return policy, value

    def dynamics(self, latent, action, hiddens=None):
        if self.config["value_prefix"]:
            latent, val_prefix, new_hiddens = self.dyna_net(latent, action, hiddens)
            return latent, val_prefix, new_hiddens
        else:
            latent, reward = self.dyna_net(latent, action)
            return latent, reward

    def represent(self, observation):
        latent = self.repr_net(observation)
        return latent
        

class MuZeroAtariNet(nn.Module):
    def __init__(self, action_size, obs_size, config):
        super().__init__()
        self.config = config

        self.x_pad, self.y_pad = 0, 0
        print(obs_size)
        assert len(obs_size) == 3

        self.y_size, self.x_size, self.n_channels = obs_size

        if self.x_size % 16 != 0:
            self.x_pad = 16 - (obs_size[1] % 16)

        if self.y_size % 16 != 0:
            self.y_pad = 16 - (obs_size[0] % 16)

        self.x_size_final = math.ceil(obs_size[1] / 16)
        self.y_size_final = math.ceil(obs_size[0] / 16)

        self.action_size = action_size
        self.obs_size = obs_size
        self.support_width = config["support_width"]
        self.channel_list = config["channel_list"]
        self.latent_depth = self.channel_list[-1]
        self.latent_area = self.x_size_final * self.y_size_final

        if config["value_prefix"]:
            self.dyna_net = AtariDynamicsLSTMNet(
                latent_depth=self.latent_depth,
                support_width=self.support_width,
                latent_area=self.latent_area,
                action_space_size=self.action_size,
                lstm_hidden_size=self.config["lstm_hidden_size"],
                val_prefix_size=self.config["val_prefix_size"],
                reward_channels=self.config["reward_channels"],
            )
        else:
            self.dyna_net = AtariDynamicsNet(
                self.latent_depth,
                self.support_width,
                self.latent_area,
                self.action_size,
            )
        self.pred_net = AtariPredictionNet(
            self.latent_depth, self.support_width, self.latent_area, self.action_size
        )
        self.repr_net = AtariRepresentationNet(
            self.x_pad,
            self.y_pad,
            self.latent_depth,
            self.channel_list,
            self.config["last_n_frames"],
            self.config["dropout"],
        )

        self.policy_loss = nn.CrossEntropyLoss(reduction="none")
        self.reward_loss = nn.CrossEntropyLoss(reduction="none")
        self.value_loss = nn.CrossEntropyLoss(reduction="none")
        self.cos_sim = nn.CosineSimilarity(dim=1)

    def consistency_loss(self, x1, x2):
        assert x1.shape == x2.shape
        batch_l = x1.shape[0]
        return -self.cos_sim(x1.reshape(batch_l, -1), x2.reshape(batch_l, -1))

    def init_optim(self, lr):
        params = (
            list(self.pred_net.parameters())
            + list(self.dyna_net.parameters())
            + list(self.repr_net.parameters())
        )
        self.optimizer = torch.optim.SGD(
            params, lr=lr, weight_decay=self.config["weight_decay"]
        )

    def predict(self, latent):
        policy, value = self.pred_net(latent)
        return policy, value

    def dynamics(self, latent, action, lstm_hiddens=None):
        if self.config["value_prefix"]:
            latent, reward, hiddens = self.dyna_net(latent, action, lstm_hiddens)
            return latent, reward, hiddens
        else:
            latent, reward = self.dyna_net(latent, action)
            return latent, reward

    def represent(self, observation):
        latent = self.repr_net(observation)
        return latent


# class ResBlock(nn.Module):
#     def __init__(
#         self, in_channels, out_channels=None, downsample=None, momentum=0.1, stride=1
#     ):
#         super().__init__()
#         if not out_channels:
#             out_channels = in_channels

#         self.conv1 = nn.Conv2d(
#             in_channels, out_channels, stride=stride, padding=1, kernel_size=3
#         )[
#         self.batch_norm1 = nn.BatchNorm2d(num_features=out_channels, momentum=momentum)

#         self.conv2 = nn.Conv2d(
#             out_channels, out_channels, stride=stride, padding=1, kernel_size=3
#         )
#         self.batch_norm2 = nn.BatchNorm2d(num_features=out_channels, momentum=momentum)
#         self.downsample = downsample

#     def forward(self, x):
#         identity = x
#         out = torch.relu(self.batch_norm1(self.conv1(x)))
#         out = self.batch_norm2(self.conv2(out))]

#         if self.downsample is not None:
#             identity = self.downsample(iout)

#         out = out + identity
#         out = torch.relu(out)
#         return out


# class AtariDereprNet(nn.Module):
#     def __init__(self, x_pad, y_pad, latent_depth, n_channels):
#         super().__init__()

#         self.pad = (0, x_pad, 0, y_pad)

#         self.conv1 = nn.Conv2d(3, n_channels, stride=2, kernel_size=3, padding=1)
#         self.batch_norm1 = nn.BatchNorm2d(num_features=n_channels, momentum=0.1)

#         self.res1 = ResBlockSmall(n_channels)

#         self.conv2 = nn.Conv2d(
#             n_channels, n_channels, stride=2, kernel_size=3, padding=1
#         )

#         self.res2 = ResBlockSmall(n_channels)
#         self.av_pool1 = nn.AvgPool2d(kernel_size=3, stride=2, padding=1)
#         self.res3 = ResBlockSmall(n_channels)
#         self.av_pool2 = nn.AvgPool2d(kernel_size=3, stride=2, padding=1)
#         self.res4 = ResBlockSmall(n_channels)

#         self.conv3 = nn.Conv2d(
#             n_channels, latent_depth, stride=1, kernel_size=3, padding=1
#         )

#     def forward(self, x):  # inputs are 96x96??
#         x = x.to(dtype=torch.float32)
#         out = F.pad(x, self.pad, "constant", 0)
#         out = torch.relu(self.batch_norm1(self.conv1(out)))  # outputs 48x48
#         out = self.res1(out)

#         out = self.conv2(out)  # outputs 24x24

#         out = self.res2(out)
#         out = self.av_pool1(out)  # outputs 12x12
#         out = self.res3(out)
#         out = self.av_pool2(out)  # outputs 6x6
#         out = self.res4(out)
#         out = self.conv3(out)
#         return out


class AtariRepresentationNet(nn.Module):
    def __init__(
        self, x_pad, y_pad, latent_depth, channel_list, last_n_frames, dropout
    ):
        super().__init__()

        self.pad = (0, x_pad, 0, y_pad)

        self.conv1 = nn.Conv2d(
            last_n_frames * 4, channel_list[0], stride=2, kernel_size=3, padding=1
        )
        self.batch_norm1 = nn.BatchNorm2d(num_features=channel_list[0], momentum=0.1)

        self.res1 = ResBlockSmall(channel_list[0], dropout=dropout)
        self.res2 = ResBlockSmall(channel_list[0], dropout=dropout)

        self.conv2 = nn.Conv2d(
            channel_list[0], channel_list[1], stride=2, kernel_size=3, padding=1
        )
        self.batch_norm2 = nn.BatchNorm2d(num_features=channel_list[1], momentum=0.1)

        self.res3 = ResBlockSmall(channel_list[1])

        self.av_pool1 = nn.AvgPool2d(kernel_size=3, stride=2, padding=1)
        self.batch_norm3 = nn.BatchNorm2d(num_features=channel_list[1], momentum=0.1)

        self.res4 = ResBlockSmall(channel_list[1])

        self.av_pool2 = nn.AvgPool2d(kernel_size=3, stride=2, padding=1)
        self.batch_norm4 = nn.BatchNorm2d(num_features=channel_list[1], momentum=0.1)

        self.res5 = ResBlockSmall(channel_list[1])

        # self.res6 = ResBlockSmall(channel_list[1])
        # self.res7 = ResBlockSmall(channel_list[1])
        # self.res8 = ResBlockSmall(channel_list[1])

    # def forward(self, x):  # inputs are 96x96??
    #     x = x.to(dtype=torch.float32)
    #     out = F.pad(x, self.pad, "constant", 0)

    #     out = torch.relu(self.batch_norm1(self.conv1(out)))  # outputs 48x48
    #     assert out.shape[-2:] == torch.Size([48, 48])

    #     out = self.res1(out)
    #     out = self.res2(out)

    #     # out = torch.relu(self.batch_norm2(self.conv2(out)))  # outputs 24x24
    #     out = torch.relu(self.batch_norm2(out))  # outputs 24x24

    #     assert out.shape[-2:] == torch.Size([24, 24])

    #     out = self.res3(out)
    #     out = self.res4(out)
    #     out = self.res5(out)

    #     out = self.av_pool1(out)  # outputs 12x12

    #     assert out.shape[-2:] == torch.Size([12, 12])

    #     out = self.res6(out)
    #     out = self.res7(out)
    #     out = self.res8(out)

    #     out = self.av_pool2(out)  # outputs 6x6

    #     assert out.shape[-2:] == torch.Size([6, 6])
    #     return out

    def forward(self, x):  # inputs are 96x96??
        x = x.to(dtype=torch.float32)
        out = F.pad(x, self.pad, "constant", 0)

        out = torch.relu(self.batch_norm1(self.conv1(out)))  # outputs 48x48
        assert out.shape[-2:] == torch.Size([48, 48])

        out = self.res1(out)
        out = self.res2(out)  
        out = torch.relu(self.batch_norm2(self.conv2(out)))  # outputs 24x24

        assert out.shape[-2:] == torch.Size([24, 24])

        out = self.res3(out)
        out = torch.relu(self.batch_norm3(self.av_pool1(out)))  # outputs 12x12

        assert out.shape[-2:] == torch.Size([12, 12])

        out = self.res4(out)
        out = torch.relu(self.batch_norm4(self.av_pool2(out)))  # outputs 6x6

        assert out.shape[-2:] == torch.Size([6, 6])

        out = self.res5(out)

        return out


class AtariDynamicsNet(nn.Module):
    def __init__(
        self,
        latent_depth,
        support_width,
        latent_area,
        action_space_size,
        reward_head_width=50,
    ):
        super().__init__()
        self.latent_depth = latent_depth
        self.full_support_width = (support_width * 2) + 1
        self.latent_area = latent_area

        self.conv1 = nn.Conv2d(
            latent_depth + action_space_size, latent_depth, kernel_size=3, padding=1
        )
        self.res1 = ResBlockSmall(latent_depth)

        self.fc1 = nn.Linear(latent_area * latent_depth, reward_head_width)
        self.fc2 = nn.Linear(reward_head_width, self.full_support_width)

    def forward(self, latent, actions_one_hot):
        # Receives 2D actions of batch_size x action_space_size
        action_images = torch.ones(
            latent.shape[0], latent.shape[2], latent.shape[3], device=latent.device
        )

        action_images_spread = torch.einsum(
            "bhw,ba->bahw", action_images, actions_one_hot
        )  # Spread the one-hot action over the first dim to make a channel for each possible action

        res_input = torch.cat((latent, action_images_spread), dim=1)

        batch_size = latent.shape[0]
        out = self.conv1(res_input)
        new_latent = self.res1(out)

        out = new_latent.reshape(batch_size, -1)
        reward_logits = self.fc2(torch.relu(self.fc1(out)))

        return new_latent, reward_logits


class AtariDynamicsLSTMNet(nn.Module):
    def __init__(
        self,
        latent_depth,
        support_width,
        latent_area,
        action_space_size,
        lstm_hidden_size,
        val_prefix_size,
        reward_channels,
    ):
        super().__init__()
        self.latent_depth = latent_depth
        self.full_support_width = (support_width * 2) + 1
        self.latent_area = latent_area

        self.conv1 = nn.Conv2d(
            latent_depth + action_space_size, latent_depth, kernel_size=3, padding=1
        )
        self.res1 = ResBlockSmall(latent_depth)

        self.conv1x1 = nn.Conv2d(latent_depth, reward_channels, 1)

        self.bn1 = nn.BatchNorm2d(reward_channels, momentum=0.9)
        self.bn2 = nn.BatchNorm1d(lstm_hidden_size, momentum=0.9)
        self.bn3 = nn.BatchNorm1d(val_prefix_size, momentum=0.9)

        self.lstm_hidden_size = lstm_hidden_size
        # lstm input size = latent.x * latent.y * reward_channels
        self.lstm_input_size = (96 // 16) * (96 // 16) * reward_channels
        self.lstm = nn.LSTM(
            input_size=self.lstm_input_size, hidden_size=self.lstm_hidden_size
        )

        self.fc1 = nn.Linear(lstm_hidden_size, val_prefix_size)
        self.fc2 = nn.Linear(val_prefix_size, self.full_support_width)

    def forward(self, latent, actions_one_hot, reward_hiddens):
        # Receives 2D actions of batch_size x action_space_size
        action_images = torch.ones(
            latent.shape[0], latent.shape[2], latent.shape[3], device=latent.device
        )

        action_images_spread = torch.einsum(
            "bhw,ba->bahw", action_images, actions_one_hot
        )  # Spread the one-hot action over the first dim to make a channel for each possible action

        res_input = torch.cat((latent, action_images_spread), dim=1)

        batch_size = latent.shape[0]
        out = self.conv1(res_input)
        new_latent = self.res1(out)

        value_prefix = self.conv1x1(new_latent)
        value_prefix = torch.relu(self.bn1(value_prefix))
        value_prefix = value_prefix.view(value_prefix.shape[0], -1)
        value_prefix = value_prefix.unsqueeze(0)
        value_prefix, value_hiddens = self.lstm(value_prefix, reward_hiddens)
        value_prefix = value_prefix.squeeze(0)
        value_prefix = torch.relu(self.bn2(value_prefix))
        value_prefix = torch.relu(self.bn3(self.fc1(value_prefix)))
        value_prefix_support = self.fc2(value_prefix)

        return new_latent, value_prefix_support, value_hiddens


class AtariPredictionNet(nn.Module):
    def __init__(
        self,
        latent_depth,
        support_width,
        latent_area,
        action_size,
        prediction_head_width=50,
    ):
        super().__init__()

        self.latent_depth = latent_depth
        self.full_support_width = (support_width * 2) + 1
        self.latent_area = latent_area

        self.fc1 = nn.Linear(latent_area * latent_depth, prediction_head_width)
        self.fc_policy = nn.Linear(prediction_head_width, action_size)
        self.fc_value = nn.Linear(prediction_head_width, self.full_support_width)

    def forward(self, x):
        batch_size = x.shape[0]
        out = x.reshape(batch_size, -1)

        out = torch.relu(self.fc1(out))
        policy_logits = self.fc_policy(out)
        value_logits = self.fc_value(out)
        return policy_logits, value_logits


class TestNet(nn.Module):
    def __init__(self, action_size, obs_size, config):
        super().__init__()
        self.config = config

        print(obs_size)
        assert len(obs_size) == 3

        self.action_size = action_size
        self.obs_size = obs_size
        self.support_width = config["support_width"]
        self.channel_list = config["channel_list"]
        self.latent_depth = self.channel_list[-1]
        self.latent_area = self.x_size_final * self.y_size_final

        self.dyna_net = TestDynamicsNet(
            self.latent_depth, self.support_width, self.action_size
        )
        self.pred_net = TestPredictionNet(
            self.latent_depth, self.support_width, self.action_size
        )
        self.repr_net = TestRepresentationNet(
            self.latent_depth,
            self.config["last_n_frames"],
        )

        self.policy_loss = nn.CrossEntropyLoss(reduction="none")
        self.reward_loss = nn.CrossEntropyLoss(reduction="none")
        self.value_loss = nn.CrossEntropyLoss(reduction="none")
        self.cos_sim = nn.CosineSimilarity(dim=1)

    def consistency_loss(self, x1, x2):
        assert x1.shape == x2.shape
        batch_l = x1.shape[0]
        return -self.cos_sim(x1.reshape(batch_l, -1), x2.reshape(batch_l, -1))

    def init_optim(self, lr):
        params = (
            list(self.pred_net.parameters())
            + list(self.dyna_net.parameters())
            + list(self.repr_net.parameters())
        )
        self.optimizer = torch.optim.SGD(
            params, lr=lr, weight_decay=self.config["weight_decay"]
        )

    def predict(self, latent):
        policy, value = self.pred_net(latent)
        return policy, value

    def dynamics(self, latent, action):
        latent, reward = self.dyna_net(latent, action)
        return latent, reward

    def represent(self, observation):
        latent = self.repr_net(observation)
        return latent


class TestRepresentationNet(nn.Module):
    def __init__(self, latent_depth, n_frames):
        super().__init__()
        self.fc1 = nn.Linear(n_frames * 4 * 96 * 96, latent_depth)

    def forward(self, x):
        out = x.flatten()
        assert len(out) == 16 * 96 * 96
        out = F.relu(self.fc1(out))
        return out


class TestDynamicsNet(nn.Module):
    def __init__(self, latent_depth, support_width, action_size):
        super().__init__()
        self.fc_dyna = nn.Linear(latent_depth, latent_depth)
        self.fc_reward = nn.Linear(latent_depth, support_width)

    def forward(self, latent, action):
        concat = torch.concat((latent, action), dim=1)
        latent = F.relu(self.fc_dyna(concat))
        reward_logits = self.fc_reward(concat)
        return latent, reward_logits


class TestPredictionNet(nn.Module):
    def __init__(self, latent_depth, support_width, action_size):
        super().__init__()
        self.fc_value = nn.Linear(self.latent_depth, support_width)
        self.fc_policy = nn.Linear(self.latent_depth, action_size)

    def forward(self, x):
        policy_logits = self.fc_policy(x)
        value_logits = self.fc_value(x)
        return policy_logits, value_logits


def support_to_scalar(support, epsilon=0.00001):
    squeeze = False
    if support.ndim == 1:
        squeeze = True
        support.unsqueeze_(0)

    if not all(abs(torch.sum(support, dim=1)) - 1 < 0.01):
        print(support)

    half_width = int((support.shape[1] - 1) / 2)
    vals = torch.tensor(
        range(-half_width, half_width + 1), dtype=support.dtype, device=support.device
    )

    # Dot product of the two
    out_val = torch.einsum("i,bi -> b", [vals, support])

    sign_out = torch.where(out_val >= 0, 1, -1)

    num = torch.sqrt(1 + 4 * epsilon * (torch.abs(out_val) + 1 + epsilon)) - 1
    res = (num / (2 * epsilon)) ** 2

    output = sign_out * (res - 1)

    if squeeze:
        output.squeeze_(0)

    return output


def scalar_to_support(scalar: torch.Tensor, epsilon=0.00001, half_width: int = 10):
    # Scaling the value function and converting to discrete support as found in
    # Appendix F if MuZero
    squeeze = False
    if scalar.ndim == 0:
        scalar.unsqueeze_(0)
        squeeze = True

    sign_x = torch.where(scalar >= 0, 1, -1)
    h_x = sign_x * (torch.sqrt(torch.abs(scalar) + 1) - 1 + epsilon * scalar)

    h_x.clamp_(-half_width, half_width)

    upper_ndxs = (torch.ceil(h_x) + half_width).to(dtype=torch.int64)
    lower_ndxs = (torch.floor(h_x) + half_width).to(dtype=torch.int64)
    ratio = h_x % 1
    support = torch.zeros(*scalar.shape, 2 * half_width + 1, device=scalar.device)

    support.scatter_(1, upper_ndxs.unsqueeze(1), ratio.unsqueeze(1))
    # do lower ndxs second as if lower==upper, ratio = 0, 1 - ratio = 1
    support.scatter_(1, lower_ndxs.unsqueeze(1), (1 - ratio).unsqueeze(1))

    assert all(abs(torch.sum(support, dim=1) - 1) < 0.0001)

    if squeeze:
        support.squeeze_(0)

    return support
