from __future__ import print_function
from copy import deepcopy
import datetime
import math
import matplotlib.pyplot as plt
import numpy as np
import os
import sys
import time
import yaml

import pydrake  # MUST BE BEFORE TORCH OR PYRO
import pyro
import pyro.distributions as dist
from pyro.optim import Adam
from pyro.infer import SVI, Trace_ELBO
import torch.multiprocessing as mp
from tensorboardX import SummaryWriter
from torchviz import make_dot

import scene_generation.data.dataset_utils as dataset_utils
from scene_generation.models.probabilistic_scene_grammar_nodes import *
from scene_generation.models.probabilistic_scene_grammar_model import *


def print_param_store(grads=False):
    for param_name in pyro.get_param_store().keys():
        val = pyro.param(param_name)#.tolist()
        grad = pyro.param(param_name).grad
        #if isinstance(val, float):
        #    val = [val]
        if grads:
            print(param_name, ": ", val.data, ", unconstrained grad: ", pyro.get_param_store()._params[param_name].grad)
        else:
            print(param_name, ": ", val.data)

def rotate_yaml_env(env, r):
    rotation_origin = np.array([0.5, 0.5])
    rotmat = np.array([[np.cos(r), -np.sin(r)],
                       [np.sin(r), np.cos(r)]])
    for obj_k in range(env["n_objects"]):
        obj_yaml = env["obj_%04d" % obj_k]
        init_pose = np.array(obj_yaml["pose"])
        init_pose[2] += r
        init_pose[2] = np.mod(init_pose[2], np.pi*2.)
        init_pose[:2] = rotmat.dot(init_pose[:2] - rotation_origin) + rotation_origin
        obj_yaml["pose"] = init_pose.tolist()

def score_sample_sync(env, guide_gvs, outer_iterations=2, num_attempts=3):
    baseline = 0
    observed_tree, joint_score = guess_parse_tree_from_yaml(
        env, guide_gvs=guide_gvs, outer_iterations=outer_iterations, num_attempts=num_attempts, verbose=False)
    # Joint score is P(T, V_obs)
    # Latent score is P(T | V_obs)
    latents_score, _ = observed_tree.get_total_log_prob(include_observed=False)
    f = joint_score - latents_score
    total_score = -joint_score # (latents_score * (f.detach() - baseline) + f)
    print("Obs tree with joint score %f, latents score %f, total score %f" % (joint_score, latents_score, total_score))
    active_param_names = set().union(
        *[node.get_param_names() for node in observed_tree.nodes],
        *[[n + "_est" for n in node.get_global_variable_names()] for node in observed_tree.nodes])
    return total_score, active_param_names

def core_sample_async(env, guide_gvs, eval_backward, output_queue, done):
    total_score, active_param_names = score_sample_sync(env, guide_gvs, outer_iterations=2, num_attempts=2)
    if eval_backward:
        total_score.backward(retain_graph=True)
    output_queue.put(total_score.detach())
    done.wait()
    
def score_subset_of_dataset_sync(dataset, n, guide_gvs):
    # Computes an SVI ELBO estimate of n samples from the dataset,
    # with a Delta-distribution mean-field variational distribution over
    # the global latent variables, and an implicit sampled distribution
    # over the local latent variables.
    losses = []
    active_param_names = set()
    baseline = 0
    for p_k in range(n):
        # Domain randomization
        env = random.choice(dataset)
        rotate_yaml_env(env, np.random.uniform(0, 2*np.pi))
        total_score, active_param_names_local = score_sample_sync(env, guide_gvs)
        losses.append(total_score)
        active_param_names = set().union(
            active_param_names,
            active_param_names_local)
    loss = torch.stack(losses).mean()
    return loss, active_param_names

def calc_score_and_backprob_async(dataset, n, guide_gvs, optimizer=None):
    # Select out minibatch
    envs = []
    for p_k in range(n):
        # Domain randomization
        env = random.choice(dataset)
        rotate_yaml_env(env, np.random.uniform(0, 2*np.pi))
        envs.append(env)

    do_backprop = optimizer is not None
    all_params_to_optimize = set(pyro.get_param_store()._params[name] for name in pyro.get_param_store().keys())
    
    if do_backprop:
        for p in all_params_to_optimize:
            p.grad.data.zero_()
    processes = []
    losses = []
    output_queue = mp.SimpleQueue()
    done = mp.Event()
    for env in envs:
        p = mp.Process(target=core_sample_async, args=(env, guide_gvs, do_backprop, output_queue, done))
        p.start()
        processes.append(p)
    for k in range(n):
        losses.append(output_queue.get())
    done.set()
    for p in processes:
        p.join()
    loss = torch.stack(losses).mean()
    print("Loss: ", loss)
    if do_backprop:
        # Apply averaging to gradients
        for p in all_params_to_optimize:
            p.grad.data /= float(n)
        optimizer(all_params_to_optimize)
    return loss

if __name__ == "__main__":
    seed = 48
    use_writer = False
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.set_default_tensor_type(torch.DoubleTensor)
    pyro.enable_validation(True)
    pyro.clear_param_store()
    

    hyper_parse_tree = generate_hyperexpanded_parse_tree()
    #plt.figure().set_size_inches(20, 20)
    #draw_parse_tree(hyper_parse_tree, label_name=True, label_score=False)
    #plt.show()

    train_dataset = dataset_utils.ScenesDataset("../data/table_setting/table_setting_environments_nominal_train")
    test_dataset = dataset_utils.ScenesDataset("../data/table_setting/table_setting_environments_nominal_test")
    print("%d training examples" % len(train_dataset))
    print("%d test examples" % len(test_dataset))


    log_dir = "/home/gizatt/projects/scene_generation/models/runs/psg/table_setting/" + datetime.datetime.now().strftime(
        "%Y-%m-%d-%H-%m-%s")

    if use_writer:
        writer = SummaryWriter(log_dir)
        def write_np_array(writer, name, x, i):
            for yi, y in enumerate(x):
                writer.add_scalar(name + "/%d" % yi, y, i)

        
    param_val_history = []
    score_history = []
    score_test_history = []

    # Initialize the guide GVS as mean field
    guide_gvs = hyper_parse_tree.get_global_variable_store()
    # Note -- if any terminal nodes have global variables associated with
    # them, they won't be in the guide.
    for var_name in guide_gvs.keys():
        guide_gvs[var_name][0] = pyro.param(var_name + "_est",
                                            guide_gvs[var_name][0],
                                            constraint=guide_gvs[var_name][1].support)
    # do gradient steps
    print_param_store()
    best_loss_yet = np.infty


    # setup the optimizer
    adam_params = {"lr": 0.025, "betas": (0.8, 0.95)}
    all_params_to_optimize = set(pyro.get_param_store()._params[name] for name in pyro.get_param_store().keys())
    # Ensure everything in pyro param store has zero grads
    for p in all_params_to_optimize:
        assert(p.requires_grad == True)
        p.grad = torch.zeros(p.shape)
        p.share_memory_()
        p.grad.share_memory_()

    optimizer = Adam(adam_params)
    baseline = 0.


    snapshots = {}

    for step in range(500):
        loss = calc_score_and_backprob_async(train_dataset, n=2, guide_gvs=guide_gvs, optimizer=optimizer)
        #loss = svi.step(observed_tree)
        score_history.append(loss)
        
        if (step % 5 == 0):
            # Evaluate on a few test data points
            loss_test = calc_score_and_backprob_async(test_dataset, n=1, guide_gvs=guide_gvs)
            score_test_history.append(loss_test)
            print("Loss_test: ", loss_test)

            if loss_test < best_loss_yet:
                best_loss_yet = loss
                pyro.get_param_store().save("best_on_test_save.pyro")
                
            # Also generate a few example environments
            # Generate a ground truth test environment
            plt.figure().set_size_inches(20, 20)
            for k in range(4):
                plt.subplot(2, 2, k+1)
                parse_tree = generate_unconditioned_parse_tree(initial_gvs=guide_gvs)
                yaml_env = convert_tree_to_yaml_env(parse_tree)
                try:
                    DrawYamlEnvironmentPlanar(yaml_env, base_environment_type="table_setting", ax=plt.gca())
                except:
                    print("Unhandled exception in drawing yaml env")
                draw_parse_tree(parse_tree, label_name=True, label_score=True)
            if use_writer:
                writer.add_scalar('loss_test', loss_test.item(), step)
                writer.add_figure("generated_envs", plt.gcf(), step, close=True)

        all_param_state = {name: pyro.param(name).detach().cpu().numpy().copy() for name in pyro.get_param_store().keys()}
        if use_writer:
            writer.add_scalar('loss', loss.item(), step)
            for param_name in all_param_state.keys():
                write_np_array(writer, param_name, all_param_state[param_name], step)
        param_val_history.append(all_param_state)
        #print("active param names: ", active_param_names)
        print("Place setting plate mean est: ", pyro.param("place_setting_plate_mean_est"))
        print("Place setting plate var est: ", pyro.param("place_setting_plate_var_est"))
    print("Final loss: ", loss)
    print_param_store()