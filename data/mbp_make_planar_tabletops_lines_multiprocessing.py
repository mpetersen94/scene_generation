import argparse
import codecs
import datetime
import matplotlib.pyplot as plt
from multiprocessing import Pool, Queue, Manager
import numpy as np
import os
import random
import time
import yaml
import sys

import pydrake
from pydrake.common import FindResourceOrThrow
from pydrake.common.eigen_geometry import Quaternion, AngleAxis, Isometry3
from pydrake.geometry import (
    Box,
    HalfSpace,
    SceneGraph,
    Sphere
)
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
from pydrake.solvers.mathematicalprogram import (SolutionResult)
from pydrake.solvers.ipopt import (IpoptSolver)
from pydrake.systems.analysis import Simulator
from pydrake.systems.framework import DiagramBuilder
from pydrake.systems.meshcat_visualizer import MeshcatVisualizer
from pydrake.systems.rendering import PoseBundle

from underactuated.planar_multibody_visualizer import PlanarMultibodyVisualizer


def RegisterVisualAndCollisionGeometry(
        mbp, body, pose, shape, name, color, friction):
    mbp.RegisterVisualGeometry(body, pose, shape, name + "_vis", color)
    mbp.RegisterCollisionGeometry(body, pose, shape, name + "_col",
                                  friction)


class GeneratorWorker(object):
    """Multiprocess worker."""

    def __init__(self, output_queue=None):
        self.output_queue = output_queue

    def __call__(self, worker_index):
        np.random.seed(int(codecs.encode(os.urandom(4), 'hex'), 32) & (2**32 - 1))
        random.seed(os.urandom(4))

        print("Worker %d starting" % worker_index)
        builder = DiagramBuilder()
        mbp, scene_graph = AddMultibodyPlantSceneGraph(
            builder, MultibodyPlant(time_step=0.01))
        world_body = mbp.world_body()

        n_lines = np.random.randint(1, 1+1)  # Always one line
        n_bodies_per_line = np.random.randint(1, 5+1, size=n_lines)  # 1 - 5 objects
        n_bodies = int(np.sum(n_bodies_per_line))
        output_dict = {"n_objects": n_bodies}

        for k in range(n_bodies):
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
                axis=[0, 1, 0],
                damping=0.)
            mbp.AddJoint(body_joint_z)

            body_joint_theta = RevoluteJoint(
                name="body_{}_theta".format(k),
                frame_on_parent=body_pre_theta.body_frame(),
                frame_on_child=body.body_frame(),
                axis=[0, 0, 1],
                damping=0.)
            mbp.AddJoint(body_joint_theta)

            if np.random.random() > 0.5:
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
            else:
                length = np.random.uniform(0.1, 0.3)
                height = np.random.uniform(0.1, 0.3)
                body_shape = Box(length, height, 0.25)
                color = np.array([0.5, 0.25, np.random.uniform(0.5, 0.8), 1.0])
                output_dict["obj_%04d" % k] = {
                    "class": "2d_box",
                    "params": [height, length],
                    "params_names": ["height", "length"],
                    "color": color.tolist(),
                    "pose": [0, 0, 0]
                }

            RegisterVisualAndCollisionGeometry(
                mbp, body, Isometry3(), body_shape, "body_{}".format(k),
                color, CoulombFriction(0.9, 0.8))

        mbp.Finalize()

        diagram = builder.Build()

        diagram_context = diagram.CreateDefaultContext()
        mbp_context = diagram.GetMutableSubsystemContext(
            mbp, diagram_context)
        sg_context = diagram.GetMutableSubsystemContext(
            scene_graph, diagram_context)

        q0 = mbp.GetPositions(mbp_context).copy()

        k = 0
        line_start = np.random.randn(2)*0.25
        line_dir = np.random.uniform(-1, 1, size=2)
        line_dir /= np.linalg.norm(line_dir)
        if line_dir.dot(line_start) > 0:
            line_dir *= -1.0
        row_dir = np.array([line_dir[1], -line_dir[0]])

        line_spacing = np.random.uniform(0.1, 0.2)
        row_spacing = np.random.uniform(0.2, 0.4)

        print("Line dir: ", line_dir, " and row dir: ", row_dir, " and start loc: ", line_start)
        for line_i in range(n_lines):
            for line_body_i in range(n_bodies_per_line[line_i]):
                pos = line_start + line_dir*line_body_i*line_spacing + row_dir*line_i*row_spacing
                body_x_index = mbp.GetJointByName("body_{}_x".format(k)).position_start()
                q0[body_x_index] = pos[0]
                body_z_index = mbp.GetJointByName("body_{}_z".format(k)).position_start()
                q0[body_z_index] = pos[1]
                body_theta_index = mbp.GetJointByName("body_{}_theta".format(k)).position_start()
                q0[body_theta_index] = np.random.uniform(0, 2*np.pi)
                k += 1

        ik = InverseKinematics(mbp, mbp_context)
        q_dec = ik.q()
        prog = ik.prog()

        constraint = ik.AddMinimumDistanceConstraint(0.01)
        prog.AddQuadraticErrorCost(np.eye(q0.shape[0])*1.0, q0, q_dec)
        for i in range(n_bodies):
            body_x_index = mbp.GetJointByName("body_{}_x".format(i)).position_start()
            body_z_index = mbp.GetJointByName("body_{}_z".format(i)).position_start()
            prog.AddBoundingBoxConstraint(-1, 1, q_dec[body_x_index])
            prog.AddBoundingBoxConstraint(-1, 1, q_dec[body_z_index])

        mbp.SetPositions(mbp_context, q0)

        prog.SetInitialGuess(q_dec, q0)
        print("Solving for %d bodies" % n_bodies)
        print "Initial guess: ", q0
        result = prog.Solve()
        print result
        print prog.GetSolverId().name()
        qf = prog.GetSolution(q_dec)
        print "Final: ", qf
        if result == SolutionResult.kSolutionFound:
            # Update poses in output dict
            qf = mbp.GetPositions(mbp_context).copy().tolist()
            for k in range(n_bodies):
                x_index = mbp.GetJointByName("body_{}_x".format(k)).position_start()
                z_index = mbp.GetJointByName("body_{}_z".format(k)).position_start()
                t_index = mbp.GetJointByName("body_{}_theta".format(k)).position_start()

                pose = [qf[x_index], qf[z_index], qf[t_index]]
                output_dict["obj_%04d" % k]["pose"] = pose
            self.output_queue.put(output_dict)
        else:
            print("Bad projection, rejecting.")
        print("Worker %d done" % worker_index)


if __name__ == "__main__":
    start_time = time.time()

    p = Pool(20)
    m = Manager()
    n_examples = 2000
    output_queue = m.Queue()
    result = p.map_async(GeneratorWorker(output_queue=output_queue),
                         range(n_examples))
    while not result.ready():
        try:
            if not output_queue.empty():
                env = output_queue.get(timeout=0)

                with open("planar_tabletop_lines_scenes.yaml", "a") as file:
                    yaml.dump({"env_%d" % int(round(time.time() * 1000)):
                              env},
                              file)
        except Exception as e:
            print "Unhandled exception while saving data: ", e

    end_time = time.time()
    elapsed = end_time - start_time
    print("Total elapsed for %d examples: %f (%f per example)" %
          (n_examples, elapsed, elapsed / n_examples))