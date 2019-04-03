from collections import namedtuple
from copy import deepcopy
import datetime
import io
import matplotlib
import matplotlib.patches as patches
import matplotlib.pyplot as plt
# Must be before torch.
import pydrake
import numpy as np
import scipy as sp
import scipy.stats
import time

from tensorboardX import SummaryWriter

import pyro
import pyro.distributions as dist
import pyro.infer
import pyro.optim

from pyro import poutine
from pyro.infer import (
    config_enumerate,
    Trace_ELBO, TraceGraph_ELBO,
    SVI
)
from pyro.contrib.autoguide import (
    AutoDelta, AutoDiagonalNormal,
    AutoMultivariateNormal, AutoGuideList
)
import torch
import torch.distributions.constraints as constraints

import scene_generation.data.dataset_utils as dataset_utils
import scene_generation.differentiable_nlp as diff_nlp


class ProjectToFeasibilityDist(dist.TorchDistribution):
    has_rsample = True
    arg_constraints = {"pre_projection_params": torch.distributions.constraints.real}

    def __init__(self, pre_projection_params, class_i,
                 object_i, context, new_class, generated_data,
                 base_environment_type):
        batch_shape = pre_projection_params.shape[:-1]
        event_shape = pre_projection_params.shape[-1:]
        dtype = pre_projection_params.dtype
        variance = torch.tensor(0.05, dtype=dtype)
        # TODO(gizatt) Handle when len(batch_shape) > 1?
        if len(batch_shape) > 1:
            raise NotImplementedError("Don't know how to handle"
                                      "multidimensional batch.")

        ones = np.ones([batch_shape[0], 1])

        tentative_generated_data = dataset_utils.VectorizedEnvironments(
            batch_size=generated_data.batch_size,
            keep_going=np.hstack([generated_data.keep_going.detach().numpy(), ones*0]),
            classes=np.hstack([generated_data.classes.detach().numpy(), ones*class_i]).astype(np.int),
            params_by_class=[p.detach().numpy() for p in generated_data.params_by_class],
            dataset=generated_data.dataset)
        # Cram the generated parameters onto the appropriate params_by_class
        # element. Don't bother updating the other class params, as they won't
        # be read.
        # params_by_class[class_i].shape() =
        #   [minibatch_size, #_object_so_far, #_params_for_this_class]
        tentative_generated_data.params_by_class[class_i] = np.concatenate(
            [tentative_generated_data.params_by_class[class_i],
             pre_projection_params.detach().numpy().reshape(batch_shape[0], 1, -1)], axis=1)

        all_params = []
        all_params_derivatives = []
        for k in range(batch_shape[0]):
            # Short circuit if class_i and new_class don't match,
            # or if keep_going has previously been zero.
            # This will likely happen in batching quite frequently,
            # and saves lots of unnecessary projections. What is produced
            # doesn't matter, as evaluated probabilities will be masked out.
            new_params = pre_projection_params[k, :].clone()
            new_params_derivs = torch.eye(event_shape[0], dtype=dtype)
            if class_i == new_class[k] and (
                    object_i == 0 or
                    np.all(generated_data.keep_going[k, :].detach().numpy() != 0.)):
                env = tentative_generated_data.subsample([k]).convert_to_yaml()[0]
                builder, mbp, scene_graph, q0 = dataset_utils.BuildMbpAndSgFromYamlEnvironment(
                    env, base_environment_type)
                diagram = builder.Build()

                diagram_context = diagram.CreateDefaultContext()
                mbp_context = diagram.GetMutableSubsystemContext(
                    mbp, diagram_context)

                # Pre-compute the "active" decision variable indices
                if base_environment_type in ["planar_bin", "planar_tabletop"]:
                    x_index = mbp.GetJointByName(
                        "body_{}_x".format(object_i)).position_start()
                    z_index = mbp.GetJointByName(
                        "body_{}_z".format(object_i)).position_start()
                    t_index = mbp.GetJointByName(
                        "body_{}_theta".format(object_i)).position_start()
                    inds = [x_index, z_index, t_index]
                else:
                    raise NotImplementedError("Unsupported base environment type.")

                # Do projection
                q_min = q0.copy()
                q_max = q0.copy()
                q_min[inds] = -np.infty
                q_max[inds] = np.infty

                results = diff_nlp.ProjectMBPToFeasibility(
                    q0, mbp, mbp_context,
                    [diff_nlp.SetArguments(diff_nlp.AddMinimumDistanceConstraint, minimum_distance=0.01),
                     diff_nlp.SetArguments(diff_nlp.AddJointPositionBounds, q_min=q_min, q_max=q_max)],
                    compute_gradients_at_solution=True, verbose=0)

                new_params[:len(inds)] = torch.tensor(results.qf[inds])
                for i, ind in enumerate(inds):
                    new_params_derivs[i, :len(inds)] = torch.tensor(results.dqf_dq0[ind, inds])

            all_params.append(new_params)
            all_params_derivatives.append(new_params_derivs)

        all_params_tensor = torch.stack(all_params)
        all_params_derivatives_tensor = torch.stack(all_params_derivatives)

        self._rsample = diff_nlp.PassthroughWithGradient.apply(
            pre_projection_params, all_params_tensor,
            all_params_derivatives_tensor)
        self._distrib = dist.Normal(
            self._rsample,
            variance.expand(event_shape)).to_event(1)

        super(ProjectToFeasibilityDist, self).__init__(
            batch_shape, event_shape, validate_args=False)

    def expand(self, batch_shape, _instance=None):
        new = self._get_checked_instance(
            ProjectToFeasibilityDist, _instance)
        batch_shape = torch.Size(batch_shape)
        new._rsample = self._rsample.expand(batch_shape + self.event_shape)
        new._distrib = self._distrib.expand(batch_shape)
        super(ProjectToFeasibilityDist, new).__init__(
            batch_shape, self.event_shape, validate_args=False)
        return new

    @torch.distributions.constraints.dependent_property
    def support(self):
        return torch.distributions.constraints.real

    def log_prob(self, value):
        assert value.shape[-1] == self.event_shape[0]
        if value.dim() > 1:
            assert value.shape[0] == self.batch_shape[0]

        # Difference of each new value from the projected point
        diff_values = value - self._rsample
        # Project that into the infeasible cone -- we're moving out
        # towards local infeasible space if any of these inner products
        # is positive.
        # I'll use a "large" threshold of violation for now...
        # TODO(gizatt) What's a good val for eps?
        return self._distrib.log_prob(value)

    def rsample(self, sample_shape=torch.Size()):
        return self._rsample.expand(sample_shape + self.batch_shape + self.event_shape)


class MultiObjectMultiClassModelWithContext():
    def __init__(self, dataset, use_projection=False):
        assert(isinstance(dataset, dataset_utils.ScenesDatasetVectorized))
        self.use_projection = use_projection
        self.dataset = dataset
        self.base_environment_type = dataset.base_environment_type
        self.context_size = 20
        self.class_general_encoded_size = 20
        self.max_num_objects = dataset.get_max_num_objects()
        self.num_classes = dataset.get_num_classes()
        self.num_params_by_class = dataset.get_num_params_by_class()

        # Class-specific encoders and generators
        self.class_means_generators = []
        self.class_vars_generators = []
        self.class_encoders = []
        for class_i in range(self.num_classes):
            # Generator
            input_size = self.context_size
            output_size = self.num_params_by_class[class_i]
            generator_H = 20
            self.class_means_generators.append(
                torch.nn.Sequential(
                    torch.nn.Linear(input_size, generator_H),
                    torch.nn.ReLU(),
                    torch.nn.Linear(generator_H, generator_H),
                    torch.nn.ReLU(),
                    torch.nn.Linear(generator_H, output_size),
                )
            )
            self.class_vars_generators.append(
                torch.nn.Sequential(
                    torch.nn.Linear(input_size, generator_H),
                    torch.nn.ReLU(),
                    torch.nn.Linear(generator_H, generator_H),
                    torch.nn.ReLU(),
                    torch.nn.Linear(generator_H, output_size),
                    torch.nn.Softplus()
                )
            )
            # Encoder
            input_size = self.num_params_by_class[class_i]
            output_size = self.context_size
            encoder_H = 20
            self.class_encoders.append(
                torch.nn.Sequential(
                    torch.nn.Linear(input_size, encoder_H),
                    torch.nn.ReLU(),
                    torch.nn.Linear(encoder_H, encoder_H),
                    torch.nn.ReLU(),
                    torch.nn.Linear(encoder_H, output_size),
                )
            )

        self.context_updater = torch.nn.GRU(
            input_size=self.context_size,
            hidden_size=20)

        # Keep going predictor:
        # regresses bernoulli keep_going weight
        # from current context
        keep_going_H = 20
        self.keep_going_controller = torch.nn.Sequential(
            torch.nn.Linear(self.context_size, keep_going_H),
            torch.nn.ReLU(),
            torch.nn.Linear(keep_going_H, keep_going_H),
            torch.nn.ReLU(),
            torch.nn.Linear(keep_going_H, 1),
            torch.nn.Sigmoid()
        )
        # Class predictor:
        # regresses categorical weights
        # from current context
        class_H = 20
        self.class_controller = torch.nn.Sequential(
            torch.nn.Linear(self.context_size, class_H),
            torch.nn.ReLU(),
            torch.nn.Linear(keep_going_H, class_H),
            torch.nn.ReLU(),
            torch.nn.Linear(keep_going_H, self.num_classes),
            torch.nn.Softmax()
        )

        # Param inversion for guide:
        # Predicts pre-projection params from post-projection params.
        # TODO(gizatt) This shouldn't be necessary... and this task
        # is impossible, it's non-invertible.
        if self.use_projection:
            self.class_guides = []
            for class_i in range(self.num_classes):
                # Generator
                input_size = self.num_params_by_class[class_i]
                output_size = self.num_params_by_class[class_i]
                H = 50
                self.class_guides.append(
                    torch.nn.Sequential(
                        torch.nn.Linear(input_size, H),
                        torch.nn.ReLU(),
                        torch.nn.Linear(H, H),
                        torch.nn.ReLU(),
                        torch.nn.Linear(H, H),
                        torch.nn.ReLU(),
                        torch.nn.Linear(H, output_size),
                    )
                )

    def _create_empty_context(self, minibatch_size):
        return torch.zeros(minibatch_size, self.context_size)

    def _extract_keep_going(self, data, object_i):
        if data is None:
            return None
        return data.keep_going[..., object_i]

    def _extract_new_class(self, data, object_i):
        if data is None:
            return None
        return data.classes[..., object_i]

    def _extract_params(self, data, object_i):
        if data is None:
            return [None]*self.num_classes
        params = [p[..., object_i, :] for p in data.params_by_class]
        return params

    def _sample_keep_going(self, object_i, minibatch_size, context,
                           observed_keep_going):
        # This sampling strategy supports a geometric distribution
        # over # of objects.
        #keep_going_params = pyro.param(
        #    "keep_going_weights", torch.ones(1)*0.5,
        #    constraint=constraints.interval(0, 1))
        #keep_going_params = pyro.param(
        #    "keep_going_weights".format(object_i),
        #    torch.ones(self.max_num_objects)*0.9,
        #    constraint=constraints.interval(0, 1))[object_i]
        keep_going_params = self.keep_going_controller(context).view(minibatch_size)
        #keep_going_params = pyro.param(
        #    "keep_going_weights", torch.ones(1)*0.5,
        #    constraint=constraints.interval(0, 1))
        return pyro.sample("keep_going_%d" % object_i,
                           dist.Bernoulli(keep_going_params),
                           obs=observed_keep_going) == 1.

    def _sample_new_class(self, object_i, minibatch_size, context,
                          observed_new_class):
        # TODO(gizatt) An alternative generator could carry around
        # the predicted class weights on their own, and use them
        # to collect the results of the encoders into an
        # updated context to avoid the cast-to-int that has
        # to happen here. The actual thing "generated" would no
        # longer be clear, but that's irrelevant in the context
        # of training this thing, isn't it?
        #new_class_params = pyro.param(
        #    "new_class_weights",
        #    torch.ones(self.num_classes)/self.num_classes,
        #    constraint=constraints.simplex)
        new_class_params = self.class_controller(context)[0]
        return pyro.sample("new_class_%d" % object_i,
                           dist.Categorical(new_class_params),
                           obs=observed_new_class)

    def _sample_class_specific_generators(
            self, object_i, minibatch_size, context, new_class,
            observed_params, generated_data):
        # To operate in batch, this needs to sample from every
        # class-specific generator, but mask off the ones that weren't
        # actually selected.
        # Unfortunately, that's pretty ugly and super-wasteful, but I
        # suspect the batch speedup will still be worth it.

        sampled_params_components = []
        for class_i in range(self.num_classes):
            if self.use_projection:
                def sample_params():
                    params_means = self.class_means_generators[class_i](context).view(minibatch_size, self.num_params_by_class[class_i])
                    params_vars = self.class_vars_generators[class_i](context).view(minibatch_size, self.num_params_by_class[class_i])

                    pre_projection_params = pyro.sample(
                        "pre_projection_params_{}_{}".format(object_i, class_i),
                        dist.Normal(params_means, params_vars + 1E-6).to_event(1))

                    projection_dist = ProjectToFeasibilityDist(
                        pre_projection_params, class_i,
                        object_i, context, new_class, generated_data,
                        self.base_environment_type)

                    return pyro.sample("params_{}_{}".format(object_i, class_i),
                                       projection_dist, obs=observed_params[class_i])
            else:
                def sample_params():
                    params_means = self.class_means_generators[class_i](context).view(minibatch_size, self.num_params_by_class[class_i])
                    params_vars = self.class_vars_generators[class_i](context).view(minibatch_size, self.num_params_by_class[class_i])
                    return pyro.sample(
                        "params_{}_{}".format(object_i, class_i),
                        dist.Normal(params_means, params_vars + 1E-6).to_event(1),
                        obs=observed_params[class_i])

            # Sample everything in batch -- meaning we'll sample
            # every class even though we know what class we wanted to sample.
            # Mask so that only the appropriate ones show up in the objective.
            sampled_params_components.append(
                poutine.mask(sample_params, new_class == class_i)())

        return sampled_params_components

    def _apply_class_specific_encoders(
            self, context, new_class, params):
        # No masking is needed because the encoders are deterministic.
        # Some clever splitting off to each encoder is still needed,
        # though...*

        encoded_components = [
            self.class_encoders[class_i](params[class_i])
            for class_i in range(self.num_classes)
        ]

        one_hot = torch.zeros(new_class.shape + (self.num_classes,))
        one_hot.scatter_(1, new_class.unsqueeze(1), 1)
        one_hot = one_hot.view(-1, 1, self.num_classes)
        stacked_components = torch.stack(encoded_components, dim=1)
        collapsed_components = one_hot.matmul(stacked_components).view(
            new_class.shape[0], self.context_size)
        return collapsed_components

    def _update_context(self, encoded_params, context):
        return self.context_updater(
            encoded_params.view(1, -1, self.class_general_encoded_size),
            context.view(1, -1, self.context_size))[-1]

    def _sample_single_object(self, object_i, data, batch_size,
                              context, generated_data):
        # Sample the new object type
        observed_new_class = self._extract_new_class(data, object_i)
        new_class = self._sample_new_class(
            object_i, batch_size, context, observed_new_class)

        # Generate an object of that type.
        observed_params = self._extract_params(
            data, object_i)
        sampled_params = self._sample_class_specific_generators(
            object_i, batch_size, context, new_class,
            observed_params, generated_data)
        # Update the context by encoding the new params
        # into a fixed-size vector through a class-specific encoder.
        encoded_params = self._apply_class_specific_encoders(
            context, new_class, sampled_params)
        context = self._update_context(
            encoded_params, context)

        # Keep going, after this?
        observed_keep_going = self._extract_keep_going(data, object_i)
        keep_going = self._sample_keep_going(
            object_i, batch_size, context, observed_keep_going)

        return keep_going, new_class, sampled_params, encoded_params, context

    def model(self, data=None, subsample_size=None):
        pyro.module("context_updater_module", self.context_updater, update_module_params=True)
        pyro.module("keep_going_controller_module", self.keep_going_controller, update_module_params=True)
        pyro.module("class_controller_module", self.class_controller, update_module_params=True)
        for class_i in range(self.num_classes):
            pyro.module("class_means_generator_module_{}".format(class_i),
                        self.class_means_generators[class_i], update_module_params=True)
            pyro.module("class_vars_generator_module_{}".format(class_i),
                        self.class_vars_generators[class_i], update_module_params=True)
            pyro.module("class_encoder_module_{}".format(class_i),
                        self.class_encoders[class_i], update_module_params=True)
        if data is None:
            data_batch_size = 1
        else:
            data_batch_size = data.batch_size

        generated_keep_going = []
        generated_classes = []
        generated_params_by_class = [[] for i in range(self.num_classes)]
        generated_encodings = []
        generated_contexts = []

        with pyro.plate('data', size=data_batch_size) as subsample_inds:
            if data is not None:
                data_sub = data.subsample(subsample_inds)
            else:
                data_sub = None
            minibatch_size = len(subsample_inds)
            context = self._create_empty_context(minibatch_size)

            # Because any given row in the batch might produce
            # all of the objects, we must iterate over all of the
            # generation steps and mask if we're on a stop
            # where the generator said to stop.
            not_terminated = torch.ones(minibatch_size) == 1.
            generated_data = dataset_utils.VectorizedEnvironments(
                batch_size=minibatch_size,
                keep_going=torch.empty(minibatch_size, 0),
                classes=torch.empty(minibatch_size, 0),
                params_by_class=[torch.empty(minibatch_size, 0, p) for p in self.num_params_by_class],
                dataset=self.dataset)
            for object_i in range(self.max_num_objects):
                # Do a generation step
                keep_going, new_class, sampled_params, encoded_params, context = \
                    poutine.mask(
                        lambda: self._sample_single_object(
                            object_i, data_sub, minibatch_size,
                            context, generated_data),
                        not_terminated)()

                not_terminated = not_terminated * keep_going
                generated_keep_going.append(keep_going)
                generated_classes.append(new_class)
                for k in range(self.num_classes):
                    generated_params_by_class[k].append(sampled_params[k])
                generated_encodings.append(encoded_params)
                generated_contexts.append(context)

                # Reassemble the output VectorizedEnvironments
                generated_data = dataset_utils.VectorizedEnvironments(
                    batch_size=minibatch_size,
                    keep_going=torch.stack(generated_keep_going, -1),
                    classes=torch.stack(generated_classes, -1),
                    params_by_class=[
                        torch.stack(p, -2) for p in generated_params_by_class],
                    dataset=self.dataset)

        return (generated_data,
                torch.stack(generated_encodings, -1),
                torch.stack(generated_contexts, -1))

    def guide(self, data, subsample_size=None):
        if not data:
            raise InvalidArgumentError("Guide must be handed data.")
        data_batch_size = data.batch_size
        if subsample_size:
            minibatch_size = subsample_size
        else:
            minibatch_size = data_batch_size
        if self.use_projection:
            AMORTIZED = True
            if AMORTIZED:
                for class_i in range(self.num_classes):
                    pyro.module("class_guide_module_{}".format(class_i),
                                self.class_guides[class_i], update_module_params=True)

            with pyro.plate('data', size=data_batch_size, subsample_size=subsample_size) as subsample_inds:
                data_sub = data.subsample(subsample_inds)
                for object_i in range(self.max_num_objects):
                    for class_i in range(self.num_classes):
                        if AMORTIZED:
                            estimate = self.class_guides[class_i](data_sub.params_by_class[class_i][:, object_i, :])
                            pyro.sample(
                                "pre_projection_params_{}_{}".format(object_i, class_i),
                                dist.Delta(estimate).to_event(1))

                        else:
                            estimate = pyro.param("delta_pre_projection_params_{}_{}".format(object_i, class_i),
                                                  torch.randn(data_batch_size, self.num_params_by_class[class_i]),
                                                  event_dim=1)
                            pyro.sample(
                                "pre_projection_params_{}_{}".format(object_i, class_i),
                                dist.Delta(estimate).to_event(1))



if __name__ == "__main__":
    pyro.enable_validation(True)

    file = "../data/planar_bin/planar_bin_static_scenes.yaml"
    scenes_dataset = dataset_utils.ScenesDatasetVectorized(
        file, base_environment_type="planar_bin")
    data = scenes_dataset.get_full_dataset()

    model = MultiObjectMultiClassModelWithContext(scenes_dataset, use_projection=True)

    log_dir = "runs/pmomc2/" + datetime.datetime.now().strftime(
        "%Y-%m-%d-%H-%m-%s")
    writer = SummaryWriter(log_dir)

    start = time.time()
    pyro.clear_param_store()
    generated_data, generated_encodings, generated_contexts = model.model()

    # Convert that data back to a YAML environment, which is easier to
    # handle.
    scene_yaml = scenes_dataset.convert_vectorized_environment_to_yaml(
        generated_data)
    dataset_utils.DrawYamlEnvironment(scene_yaml[0], "planar_bin")
    end = time.time()
    print "Time to generate and draw one scene: %fs" % (end - start)

    start = time.time()
    pyro.clear_param_store()
    trace = poutine.trace(model.model).get_trace()
    trace.compute_log_prob()
    end = time.time()
    #print(trace.format_shapes())
    print "Time to run and do log probs with no args: %fs" % (end - start)
#
    start = time.time()
    pyro.clear_param_store()
    trace = poutine.trace(model.model).get_trace(
        data.subsample([0, 1, 2]))
    trace.compute_log_prob()
    end = time.time()
    print "Time to run and do log probs with 3 datapoints: %fs" % (end - start)
    #print(trace.format_shapes())
#

    start = time.time()
    pyro.clear_param_store()
    trace = poutine.trace(model.model).get_trace(data.subsample(range(100)))
    trace.compute_log_prob()
    end = time.time()
    print "Time to run and do log probs with %d datapoints: %fs" % (
        100, end - start)

    #pyro.clear_param_store()
    #trace = poutine.trace(model.generation_guide).get_trace(data, subsample_size=5)
    #trace.compute_log_prob()
    #print "GUIDE WITH ARGS RUN SUCCESSFULLY"
    ##print(trace.format_shapes())
#