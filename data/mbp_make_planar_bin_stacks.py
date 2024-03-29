import argparse
import datetime
import matplotlib.pyplot as plt
import numpy as np
import os
import random
import time
import yaml
import sys

import pydrake
from pydrake.autodiffutils import AutoDiffXd
from pydrake.common import FindResourceOrThrow
from pydrake.common.eigen_geometry import Quaternion, AngleAxis, Isometry3
from pydrake.geometry import (
    Box,
    HalfSpace,
    SceneGraph,
    SceneGraph_,
    Sphere
)
from pydrake.math import RigidTransform
from pydrake.multibody.tree import (
    PrismaticJoint,
    SpatialInertia,
    RevoluteJoint,
    UniformGravityFieldElement,
    UnitInertia
)
from pydrake.multibody.plant import (
    AddMultibodyPlantSceneGraph,
    CoulombFriction,
    MultibodyPlant
)

from pydrake.forwarddiff import gradient
from pydrake.multibody.parsing import Parser
from pydrake.multibody.inverse_kinematics import InverseKinematics
from pydrake.solvers.ipopt import (IpoptSolver)
from pydrake.solvers.mathematicalprogram import MathematicalProgram, Solve
from pydrake.multibody.optimization import StaticEquilibriumProblem
from pydrake.systems.analysis import Simulator
from pydrake.systems.framework import AbstractValue, BasicVector, DiagramBuilder, LeafSystem
from pydrake.systems.meshcat_visualizer import MeshcatVisualizer
from pydrake.systems.rendering import PoseBundle

from underactuated.planar_scenegraph_visualizer import PlanarSceneGraphVisualizer

def RegisterVisualAndCollisionGeometry(
        mbp, body, pose, shape, name, color, friction):
    mbp.RegisterVisualGeometry(body, pose, shape, name + "_vis", color)
    mbp.RegisterCollisionGeometry(body, pose, shape, name + "_col",
                                  friction)


def generate_mbp_sg_diagram(seed):
    np.random.seed(seed)

    builder = DiagramBuilder()
    mbp, scene_graph = AddMultibodyPlantSceneGraph(
        builder, MultibodyPlant(time_step=0.005))

    # Add ground
    world_body = mbp.world_body()
    ground_shape = Box(2., 2., 1.)
    wall_shape = Box(0.1, 2., 1.1)
    ground_body = mbp.AddRigidBody("ground", SpatialInertia(
        mass=10.0, p_PScm_E=np.array([0., 0., 0.]),
        G_SP_E=UnitInertia(1.0, 1.0, 1.0)))
    mbp.WeldFrames(world_body.body_frame(), ground_body.body_frame(),
                   RigidTransform())
    RegisterVisualAndCollisionGeometry(
        mbp, ground_body,
        RigidTransform(p=[0, 0, -0.5]),
        ground_shape, "ground", np.array([0.5, 0.5, 0.5, 1.]),
        CoulombFriction(0.9, 0.8))
    # Short table walls
    RegisterVisualAndCollisionGeometry(
        mbp, ground_body,
        RigidTransform(p=[-1, 0, 0]),
        wall_shape, "wall_nx",
        np.array([0.5, 0.5, 0.5, 1.]), CoulombFriction(0.9, 0.8))
    RegisterVisualAndCollisionGeometry(
        mbp, ground_body,
        RigidTransform(p=[1, 0, 0]),
        wall_shape, "wall_px",
        np.array([0.5, 0.5, 0.5, 1.]), CoulombFriction(0.9, 0.8))

    n_stacks = max(min(np.random.geometric(0.5), 3), 1)
    n_bodies_per_stack = np.array([max(min(np.random.geometric(0.3), 5), 2) for k in range(n_stacks)])
    n_bodies = np.sum(n_bodies_per_stack)
    output_dict = {"n_objects": int(n_bodies)}

    k = 0
    for stack_i in range(n_stacks):
        for obj_i in range(n_bodies_per_stack[stack_i]):
            no_mass_no_inertia = SpatialInertia(
                mass=1.0, p_PScm_E=np.array([0., 0., 0.]),
                G_SP_E=UnitInertia(0., 0., 0.))
            body_pre_z = mbp.AddRigidBody("body_{}_pre_z".format(k),
                                          no_mass_no_inertia)
            body_pre_theta = mbp.AddRigidBody("body_{}_pre_theta".format(k),
                                              no_mass_no_inertia)
            body = mbp.AddRigidBody("body_{}".format(k), SpatialInertia(
                mass=1.0, p_PScm_E=np.array([0., 0., 0.]),
                G_SP_E=UnitInertia(0.1, 0.1, 0.1)))

            body_joint_x = PrismaticJoint(
                name="body_{}_x".format(k),
                frame_on_parent=world_body.body_frame(),
                frame_on_child=body_pre_z.body_frame(),
                axis=[1, 0, 0],
                damping=0.)
            mbp.AddJoint(body_joint_x)

            body_joint_z = PrismaticJoint(
                name="body_{}_z".format(k),
                frame_on_parent=body_pre_z.body_frame(),
                frame_on_child=body_pre_theta.body_frame(),
                axis=[0, 0, 1],
                damping=0.)
            mbp.AddJoint(body_joint_z)

            body_joint_theta = RevoluteJoint(
                name="body_{}_theta".format(k),
                frame_on_parent=body_pre_theta.body_frame(),
                frame_on_child=body.body_frame(),
                axis=[0, 1, 0],
                damping=0.)
            mbp.AddJoint(body_joint_theta)

            #if (obj_i == n_bodies_per_stack[stack_i] - 1) and np.random.random() > 0.5:
            radius = np.random.uniform(0.05, 0.15)
            color = np.array([0.25, 0.5, np.random.uniform(0.5, 0.8), 1.0])
            body_shape = Sphere(radius)
            output_dict["obj_%04d" % k] = {
                "class": "2d_sphere",
                "params": [radius],
                "params_names": ["radius"],
                "color": color.tolist(),
                "pose": [0, 0, 0]
            }
            #else:
            #    length = np.random.uniform(0.1, 0.3)
            #    height = np.random.uniform(0.1, 0.3)
            #    body_shape = Box(length, 0.25, height)
            #    color = np.array([0.5, 0.25, np.random.uniform(0.5, 0.8), 1.0])
            #    output_dict["obj_%04d" % k] = {
            #        "class": "2d_box",
            #        "params": [height, length],
            #        "params_names": ["height", "length"],
            #        "color": color.tolist(),
            #        "pose": [0, 0, 0]
            #    }

            RegisterVisualAndCollisionGeometry(
                mbp, body, RigidTransform(), body_shape, "body_{}".format(k),
                color, CoulombFriction(0.9, 0.8))
            k += 1

    mbp.Finalize()
    return builder, mbp, scene_graph, n_bodies_per_stack

def generate_example():
    seed = int(time.time()*1000*1000) % 2**32
    builder, mbp, scene_graph, n_bodies_per_stack = generate_mbp_sg_diagram(seed)
    n_bodies = sum(n_bodies_per_stack)
    n_stacks = len(n_bodies_per_stack)

    visualizer = builder.AddSystem(MeshcatVisualizer(
        scene_graph,
        zmq_url="tcp://127.0.0.1:6000"))
    visualizer.load()
    builder.Connect(scene_graph.get_pose_bundle_output_port(),
                    visualizer.get_input_port(0))

    #plt.gca().clear()
    #visualizer = builder.AddSystem(PlanarSceneGraphVisualizer(scene_graph, ylim=[-0.5, 1.0], ax=plt.gca()))
    #builder.Connect(scene_graph.get_pose_bundle_output_port(),
    #                visualizer.get_input_port(0))
    diagram = builder.Build()

    diagram_context = diagram.CreateDefaultContext()
    mbp_context = diagram.GetMutableSubsystemContext(
        mbp, diagram_context)
    sg_context = diagram.GetMutableSubsystemContext(
        scene_graph, diagram_context)

    q0 = mbp.GetPositions(mbp_context).copy()
    body_i = 0
    for stack_i in range(n_stacks):
        stack_base_location = np.random.uniform(-0.85, 0.85)
        for obj_i in range(n_bodies_per_stack[stack_i]):
            body_x_index = mbp.GetJointByName("body_{}_x".format(obj_i)).position_start()
            q0[body_x_index] = stack_base_location
            body_z_index = mbp.GetJointByName("body_{}_z".format(obj_i)).position_start()
            q0[body_z_index] = 0.3 * obj_i + 0.3
            body_i += 1

    # Create the static equilibrium projection problem
    # Re-generate MBP and SG, this time for conversion to AD
    builder_ad, mbp_ad, _, _ = generate_mbp_sg_diagram(seed)
    diagram_ad = builder_ad.Build().ToAutoDiffXd()
    mbp_ad = diagram_ad.GetSubsystemByName(mbp_ad.get_name())
    diagram_ad_context = diagram_ad.CreateDefaultContext()
    mbp_ad_context = diagram_ad.GetMutableSubsystemContext(mbp_ad, diagram_ad_context)
    se_problem = StaticEquilibriumProblem(mbp_ad, mbp_ad_context, set())
    prog = se_problem.get_mutable_prog()
    q_dec = se_problem.q_vars()

    #constraint = ik.AddMinimumDistanceConstraint(0.01)
    prog.AddQuadraticErrorCost(np.eye(q0.shape[0])*1.0, q0, q_dec)
    for obj_i in range(n_bodies):
        body_x_index = mbp.GetJointByName("body_{}_x".format(obj_i)).position_start()
        body_z_index = mbp.GetJointByName("body_{}_z".format(obj_i)).position_start()
        prog.AddBoundingBoxConstraint(-1, 1, q_dec[body_x_index])
        prog.AddBoundingBoxConstraint(0, 2, q_dec[body_z_index])

    mbp.SetPositions(mbp_context, q0)

    prog.SetInitialGuess(q_dec, q0)

    def vis_callback(x):
        print("Callback with ", x)
        vis_diagram_context = diagram.CreateDefaultContext()
        mbp.SetPositions(diagram.GetMutableSubsystemContext(mbp, vis_diagram_context), x)
        pose_bundle = scene_graph.get_pose_bundle_output_port().Eval(diagram.GetMutableSubsystemContext(scene_graph, vis_diagram_context))
        context = visualizer.CreateDefaultContext()
        context.FixInputPort(0, AbstractValue.Make(pose_bundle))
        visualizer.Publish(context)
    prog.AddVisualizationCallback(vis_callback, q_dec)
            
    print("Solving")
    print("Initial guess: ", q0)
    t_start = time.time()
    result = Solve(prog)
    print("Solved in %f seconds" % (time.time() - t_start))
    print(result)
    print(result.get_solver_id().name())
    q0_proj = result.GetSolution(q_dec)
    print("Final: ", q0_proj)

    mbp.SetPositions(mbp_context, q0_proj)

    # mbp_context.FixInputPort(
    #    mbp.get_actuation_input_port().get_index(), np.zeros(
    #        mbp.get_actuation_input_port().size()))

    #simulator = Simulator(diagram, diagram_context)
    #simulator.set_target_realtime_rate(1.0)
    #simulator.set_publish_every_time_step(False)
    #simulator.StepTo(5.0)
#
    ## Update poses in output dict
    #qf = mbp.GetPositions(mbp_context).copy().tolist()
    #for k in range(n_bodies):
    #    x_index = mbp.GetJointByName("body_{}_x".format(k)).position_start()
    #    z_index = mbp.GetJointByName("body_{}_z".format(k)).position_start()
    #    t_index = mbp.GetJointByName("body_{}_theta".format(k)).position_start()
#
    #    pose = [qf[x_index], qf[z_index], qf[t_index]]
    #    output_dict["obj_%04d" % k]["pose"] = pose
    #return output_dict


if __name__ == "__main__":
    #np.random.seed(42)
    #random.seed(42)

    # Somewhere in the n=1000 range, I hit a
    # "Unhandled exception: Too many open files" error somewhere
    # between Meshcat setup and the print "Solving" line.
    import matplotlib.pyplot as plt
    plt.plot(10, 10)
    for example_num in range(1):
        #try:
            env = generate_example()
            # Check if it's reasonable
            for k in range(env["n_objects"]):
                obj_yaml = env["obj_%04d" % k]
                # Check if x or z is outside of bounds
                pose = np.array(obj_yaml["pose"])
                if pose[0] > 2.0 or pose[0] < -2.0 or pose[1] > 2.0 or pose[1] < 0.0:
                    raise ValueError("Skipping scene due to bad projection.")
            #with open("planar_bin_static_scenes_stacks.yaml", "a") as file:
            #    yaml.dump({"env_%d" % int(round(time.time() * 1000)):
            #              env},
            #              file)
       # except Exception as e:
       #     print "Unhandled exception: ", e
