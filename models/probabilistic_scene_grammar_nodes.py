from __future__ import print_function
from copy import deepcopy
from functools import partial
import time
import random
import sys
import yaml

import matplotlib.pyplot as plt
import networkx as nx
import numpy as np

import pydrake
import torch
import torch.distributions.constraints as constraints
import pyro
import pyro.distributions as dist
from pyro import poutine

from scene_generation.data.dataset_utils import (
    DrawYamlEnvironmentPlanar, ProjectEnvironmentToFeasibility)


def chain_pose_transforms(p_w1, p_12):
    ''' p_w1: xytheta Pose 1 in world frame
        p_12: xytheta Pose 2 in Pose 1's frame
        Returns: xytheta Pose 2 in world frame. '''
    out = torch.empty(3)
    r = p_w1[2]
    out[0] = p_w1[0] + p_12[0]*torch.cos(r) - p_12[1]*torch.sin(r)
    out[1] = p_w1[1] + p_12[0]*torch.sin(r) + p_12[1]*torch.cos(r)
    out[2] = p_w1[2] + p_12[2]
    return out

def invert_pose(pose):
    # TF^-1 = [R^t  -R.' T]
    out = torch.empty(3)
    r = pose[2]
    out[0] = -(pose[0]*torch.cos(-r) - pose[1]*torch.sin(-r))
    out[1] = -(pose[0]*torch.sin(-r) + pose[1]*torch.cos(-r))
    out[2] = -r
    return out

class ProductionRule(object):
    ''' Abstract interface for a production rule.
    Callable to perform the production, but also
    queryable for what nodes this connects and able to
    provide scoring for whether a candidate production
    is a good idea at all. '''
    def __init__(self, products, name):
        self.products = products
        self.name = name
    def __call__(self, parent):
        raise NotImplementedError()
    def score_products(self, parent, products):
        raise NotImplementedError()

class RootNode(object):
    pass

class Node(object):
    def __init__(self, name):
        self.name = name

class OrNode(Node):
    def __init__(self, name, production_rules, production_weights):
        Node.__init__(self, name=name)
        if len(production_weights) != len(production_rules):
            raise ValueError("# of production rules and weights must match.")
        if len(production_weights) == 0:
            raise ValueError("Must have nonzero number of production rules.")
        self.production_rules = production_rules
        self.production_dist = dist.Categorical(production_weights)
        
    def _recover_active_rule(self, production_rules):
        if production_rules[0] not in self.production_rules:
            print("Warning: rule not in OrNode production rules.")
        return torch.tensor(self.production_rules.index(production_rules[0]))

    def sample_production_rules(self, parent, obs_production_rules=None):
        if obs_production_rules is not None:
            active_rule = self._recover_active_rule(obs_production_rules)
        else:
            active_rule = None
        active_rule = pyro.sample(self.name + "_or_sample", self.production_dist, obs=active_rule)
        return [self.production_rules[active_rule]]

    def score_production_rules(self, parent, production_rules):
        if len(production_rules) != 1:
            return torch.tensor(-np.inf)
        active_rule = self._recover_active_rule(production_rules)
        return self.production_dist.log_prob(active_rule).sum()


class AndNode(Node):
    def __init__(self, name, production_rules):
        Node.__init__(self, name=name)
        if len(production_rules) == 0:
            raise ValueError("Must have nonzero # of production rules.")
        self.production_rules = production_rules
        
    def sample_production_rules(self, parent, obs_production_rules=None):
        if obs_production_rules is not None:
            assert(obs_production_rules == self.production_rules)
        return self.production_rules

    def score_production_rules(self, parent, production_rules):
        if production_rules != self.production_rules:
            return torch.tensor(-np.inf)
        else:
            return torch.tensor(-np.inf)


class CovaryingSetNode(Node):
    def __init__(self, name, production_rules,
                 production_weights_hints = {},
                 remaining_weight = 1.):
        ''' Make a categorical distribution over
           every possible combination of production rules
           that could be active, with a separate weight
           for each combination. (2^n weights!)

           Hints can be supplied in the form of a dictionary
           of (int tuple) : (float weight) pairs, and a float
           indicating the weight to distribute to the remaining
           pairs. These floats all indicate relative occurance
           weights. '''
        Node.__init__(self, name=name)
        # Build the initial weights, taking the suggestion
        # weights into account.
        assert(remaining_weight >= 0.)
        num_combinations = 2**len(production_rules) + 1
        init_weights = torch.ones(num_combinations) * remaining_weight
        for hint in production_weights_hints.keys():
            val = production_weights_hints[hint]
            assert(val >= 0.)
            combination_index = 0
            for index in hint:
                assert(isinstance(index, int) and index >= 0 and
                       index < len(production_rules))
                combination_index += 2**index
            init_weights[combination_index] = val
        init_weights /= torch.sum(init_weights)

        self.exhaustive_set_weights = pyro.param(
            self.name + "_exhaustive_set_weights",
            init_weights,
            constraint=constraints.simplex)

        self.production_dist = dist.Categorical(logits=torch.log(self.exhaustive_set_weights))
        self.production_rules = production_rules

    def _recover_selected_rules(self, production_rules):
        selected_rules = 0
        for rule in production_rules:
            if rule not in self.production_rules:
                print("Warning: rule not in CovaryingSetNode production rules: ", rule)
                return torch.tensor(-np.inf)
            k = self.production_rules.index(rule)
            selected_rules += 2**k
        assert(selected_rules >= 0 and selected_rules <= len(self.exhaustive_set_weights))
        return selected_rules

    def sample_production_rules(self, parent, obs_production_rules=None):
        if obs_production_rules is not None:
            selected_rules = self._recover_selected_rules(obs_production_rules)
        else:
            selected_rules = None
        selected_rules = pyro.sample(
            self.name + "_exhaustive_set_sample",
            self.production_dist,
            obs=selected_rules)
        # Select out the appropriate rules
        output = []
        for k, rule in enumerate(self.production_rules):
            if (selected_rules >> k) & 1:
                output.append(rule)
        return output

    def score_production_rules(self, parent, production_rules):
        selected_rules = self._recover_selected_rules(production_rules)
        return self.production_dist.log_prob(torch.tensor(selected_rules)).sum()


class IndependentSetNode(Node):
    def __init__(self, name, production_rules,
                 production_probs):
        ''' Make a categorical distribution over production rules
            that could be active, where each rule occurs
            independently of the others. Each production weight
            is a probability of that rule being active. '''
        Node.__init__(self, name=name)
        if len(production_probs) != len(production_rules):
            raise ValueError("Must have same number of production probs "
                             "as rules.")
        self.production_dist = dist.Bernoulli(production_probs)
        self.production_rules = production_rules

    def _recover_selected_rules(self, production_rules):
        selected_rules = torch.zeros(len(self.production_rules))
        for rule in production_rules:
            if rule not in self.production_rules:
                print("Warning: rule not in IndependentSetNode production rules: ", rule)
                return torch.tensor(-np.inf)
            selected_rules[self.production_rules.index(rule)] = 1
        return selected_rules

    def sample_production_rules(self, parent, obs_production_rules=None):
        if obs_production_rules is not None:
            selected_rules = self._recover_selected_rules(obs_production_rules)
        else:
            selected_rules = None
        selected_rules = pyro.sample(
            self.name + "_independent_set_sample",
            self.production_dist, obs=selected_rules)
        # Select out the appropriate rules
        output = []
        for k, rule in enumerate(self.production_rules):
            if selected_rules[k] == 1:
                output.append(rule)
        return output

    def score_production_rules(self, parent, production_rules):
        selected_rules = self._recover_selected_rules(production_rules)
        return self.production_dist.log_prob(selected_rules).sum()


class PlaceSetting(CovaryingSetNode):

    class ObjectProductionRule(ProductionRule):
        def __init__(self, name, object_name, object_type, mean_init, var_init):
            ProductionRule.__init__(self,
                name=name,
                products=[object_type])
            self.object_name = object_name
            self.object_type = object_type
            mean = pyro.param("place_setting_%s_mean" % object_name,
                              torch.tensor(mean_init))
            var = pyro.param("place_setting_%s_var" % object_name,
                              torch.tensor(var_init),
                              constraint=constraints.positive)
            self.offset_dist = dist.Normal(
                mean, var)

        def _recover_rel_pose_from_abs_pose(self, parent, abs_pose):
            return chain_pose_transforms(invert_pose(parent.pose), abs_pose)

        def __call__(self, parent, obs_products=None):
            # Observation should be absolute position of the product
            if obs_products is not None:
                assert(len(obs_products) == 1 and isinstance(obs_products[0], self.object_type))
                obs_rel_pose = self._recover_rel_pose_from_abs_pose(parent, obs_products[0].pose)
            else:
                obs_rel_pose = None
            rel_pose = pyro.sample("%s_pose" % (self.name),
                                   self.offset_dist, obs=obs_rel_pose)
            abs_pose = chain_pose_transforms(parent.pose, rel_pose)
            return [self.object_type(name="%s_%s" % (self.name, self.object_name), pose=abs_pose)]

        def score_products(self, parent, products):
            if len(products) != 1 or not isinstance(products[0], self.object_type):
                return torch.tensor(-np.inf)
            # Get relative offset of the PlaceSetting
            rel_pose = self._recover_rel_pose_from_abs_pose(parent, products[0].pose)
            return self.offset_dist.log_prob(rel_pose).sum()

    def __init__(self, name, pose):
        self.pose = pose
        # Represent each object's relative position to the
        # place setting origin with a diagonal Normal distribution.
        # So some objects will show up multiple
        # times here (left/right variants) where we know ahead of time
        # that they'll have multiple modes.
        # TODO(gizatt) GMMs? Guide will be even harder to write.
        self.object_types_by_name = {
            "plate": Plate,
            "cup": Cup,
            "left_fork": Fork,
            #"left_knife": Knife,
            #"left_spoon": Spoon,
            #"right_fork": Fork,
            #"right_knife": Knife,
            #"right_spoon": Spoon,
        }
        param_guesses_by_name = {
            "plate": ([0., 0.16, 0.], [0.01, 0.01, 3.]),
            "cup": ([0., 0.16 + 0.15, 0.], [0.05, 0.01, 3.]),
            #"right_fork": ([0.15, 0.16, 0.], [0.01, 0.01, 0.01]),
            "left_fork": ([-0.15, 0.16, 0.], [0.01, 0.01, 0.01]),
            #"left_spoon": ([-0.15, 0.16, 0.], [0.01, 0.01, 0.01]),
            #"right_spoon": ([0.15, 0.16, 0.], [0.01, 0.01, 0.01]),
            #"left_knife": ([-0.15, 0.16, 0.], [0.01, 0.01, 0.01]),
            #"right_knife": ([0.15, 0.16, 0.], [0.01, 0.01, 0.01]),
        }
        self.distributions_by_name = {}
        production_rules = []
        name_to_ind = {}
        for k, object_name in enumerate(self.object_types_by_name.keys()):
            mean_init, var_init = param_guesses_by_name[object_name]
            production_rules.append(
                self.ObjectProductionRule(
                    name="%s_prod_%03d" % (name, k),
                    object_name=object_name,
                    object_type=self.object_types_by_name[object_name],
                    mean_init=mean_init, var_init=var_init))
            # Build name mapping for convenienc of building the hint dictionary
            name_to_ind[object_name] = k

        # Weight the "correct" rules very heavily
        production_weights_hints = {
            #(name_to_ind["plate"], name_to_ind["cup"], name_to_ind["left_fork"], name_to_ind["right_knife"], name_to_ind["right_spoon"]): 2.,
            #(name_to_ind["plate"], name_to_ind["cup"], name_to_ind["left_fork"], name_to_ind["right_knife"]): 2.,
            #(name_to_ind["plate"], name_to_ind["cup"], name_to_ind["left_fork"]): 2.,
            #(name_to_ind["plate"], name_to_ind["cup"], name_to_ind["right_fork"]): 2.,
            #(name_to_ind["plate"], name_to_ind["cup"], name_to_ind["right_fork"], name_to_ind["right_knife"]): 2.,
            #(name_to_ind["plate"], name_to_ind["right_fork"]): 2.,
            (name_to_ind["plate"], name_to_ind["left_fork"]): 1.,
            #(name_to_ind["cup"],): 0.5,
            (name_to_ind["plate"], name_to_ind["cup"]): 1.,
            (name_to_ind["plate"],): 1.,
        }
        CovaryingSetNode.__init__(self, name=name, production_rules=production_rules,
                                   production_weights_hints=production_weights_hints,
                                   remaining_weight=0.)


class Table(IndependentSetNode, RootNode):

    class PlaceSettingProductionRule(ProductionRule):
        def __init__(self, name, pose):
            ProductionRule.__init__(self, 
                name=name,
                products=[PlaceSetting])
            # Relative offset from root pose is drawn from a diagonal
            # Normal. It's rotated into the root pose frame at sample time.
            mean = pyro.param("table_place_setting_mean",
                              torch.tensor([0.0, 0., np.pi/2.]))
            var = pyro.param("table_place_setting_var",
                              torch.tensor([0.01, 0.01, 0.1]),
                              constraint=constraints.positive)
            self.offset_dist = dist.Normal(mean, var)
            self.pose = pose

        def _recover_rel_offset_from_abs_offset(self, parent, abs_offset):
            pose_in_world = chain_pose_transforms(parent.pose, self.pose)
            return chain_pose_transforms(invert_pose(pose_in_world), abs_offset)

        def __call__(self, parent, obs_products=None):
            if obs_products is not None:
                assert len(obs_products) == 1 and isinstance(obs_products[0], PlaceSetting)
                obs_rel_offset = self._recover_rel_offset_from_abs_offset(parent, obs_products[0].pose) 
            else:
                obs_rel_offset = None
            rel_offset = pyro.sample("%s_place_setting_offset" % self.name,
                                     self.offset_dist)
            # Rotate offset
            pose_in_world = chain_pose_transforms(parent.pose, self.pose)
            abs_offset = chain_pose_transforms(pose_in_world, rel_offset)
            return [PlaceSetting(name=self.name + "_place_setting", pose=abs_offset)]

        def score_products(self, parent, products):
            if len(products) != 1 or not isinstance(products[0], PlaceSetting):
                return torch.tensor(-np.inf)
            # Get relative offset of the PlaceSetting
            rel_offset = self._recover_rel_offset_from_abs_offset(parent, products[0].pose)
            return self.offset_dist.log_prob(rel_offset).sum()

    def __init__(self, name="table", num_place_setting_locations=4):
        self.pose = torch.tensor([0.5, 0.5, 0.])
        self.table_radius = pyro.param("%s_radius" % name, torch.tensor(0.45), constraint=constraints.positive)
        # Set-valued: a plate may appear at each location.
        production_rules = []
        for k in range(num_place_setting_locations):
            # TODO(gizatt) Root pose for each cluster could be a parameter.
            # This turns this into a GMM, sort of?
            r = torch.tensor((k / float(num_place_setting_locations))*np.pi*2.)
            pose = torch.empty(3)
            pose[0] = self.table_radius * torch.cos(r)
            pose[1] = self.table_radius * torch.sin(r)
            pose[2] = r
            production_rules.append(self.PlaceSettingProductionRule(
                name="%s_prod_%03d" % (name, k), pose=pose))
        IndependentSetNode.__init__(self, name, production_rules,
                                    torch.ones(num_place_setting_locations)*0.5)

class TerminalNode(Node):
    def __init__(self, name):
        Node.__init__(self, name=name)

class Plate(TerminalNode):
    def __init__(self, pose, params=[0.2], name="plate"):
        TerminalNode.__init__(self, name=name)
        self.pose = pose
        self.params = params

    def generate_yaml(self):
        return {
            "class": "plate",
            "color": None,
            "img_path": "table_setting_assets/plate_red.png",
            "params": self.params,
            "params_names": ["radius"],
            "pose": self.pose.tolist()
        }

class Cup(TerminalNode):
    def __init__(self, pose, params=[0.05], name="cup"):
        TerminalNode.__init__(self, name=name)
        self.pose = pose
        self.params = params

    def generate_yaml(self):
        return {
            "class": "cup",
            "color": None,
            "img_path": "table_setting_assets/cup_water.png",
            "params": self.params,
            "params_names": ["radius"],
            "pose": self.pose.tolist()
        }


class Fork(TerminalNode):
    def __init__(self, pose, params=[0.02, 0.14], name="fork"):
        TerminalNode.__init__(self, name)
        self.pose = pose
        self.params = params

    def generate_yaml(self):
        return {
            "class": "fork",
            "color": None,
            "img_path": "table_setting_assets/fork.png",
            "params": self.params,
            "params_names": ["width", "height"],
            "pose": self.pose.tolist()
        }

class Knife(TerminalNode):
    def __init__(self, pose, params=[0.015, 0.15], name="knife"):
        TerminalNode.__init__(self, name)
        self.pose = pose
        self.params = params
    
    def generate_yaml(self):
        return {
            "class": "knife",
            "color": None,
            "img_path": "table_setting_assets/knife.png",
            "params": self.params,
            "params_names": ["width", "height"],
            "pose": self.pose.tolist()
        }

class Spoon(TerminalNode):
    def __init__(self, pose, params=[0.02, 0.12], name="spoon"):
        TerminalNode.__init__(self, name)
        self.pose = pose
        self.params = params
    
    def generate_yaml(self):
        return {
            "class": "spoon",
            "color": None,
            "img_path": "table_setting_assets/spoon.png",
            "params": self.params,
            "params_names": ["width", "height"],
            "pose": self.pose.tolist()
        }
