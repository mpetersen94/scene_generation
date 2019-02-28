from collections import namedtuple
import datetime
import io
import matplotlib
matplotlib.use('Agg')  # noqa: E402
import matplotlib.patches as patches
import matplotlib.pyplot as plt
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


class MultiObjectMultiClassModel():
    def __init__(self, dataset):
        assert(isinstance(dataset, dataset_utils.ScenesDatasetVectorized))
        self.context_size = 10
        self.class_general_encoded_size = 10
        self.num_classes = dataset.get_num_classes()
        self.max_num_objects = dataset.get_max_num_objects()
        self.num_params_by_class = dataset.get_num_params_by_class()

        # Class-specific encoders
        self.class_encoders = []
        for class_i in range(self.num_classes):
            input_size = self.num_params_by_class[class_i]
            output_size = self.context_size
            H = 10
            self.class_encoders.append(
                torch.nn.Sequential(
                    torch.nn.Linear(input_size, H),
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
        # Geometric distribution over # of objects.
        # TODO(gizatt) This is a bad fit for the data I just generated,
        # which is uniform...
        keep_going_params = pyro.param(
            "keep_going_weight",
            torch.ones(1),
            constraint=constraints.positive)
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
        new_class_params = pyro.param(
            "new_class_weights",
            torch.ones(self.num_classes),
            constraint=constraints.simplex)
        return pyro.sample("new_class_%d" % object_i,
                           dist.Categorical(new_class_params),
                           obs=observed_new_class)

    def _sample_class_specific_generators(
            self, object_i, minibatch_size, context, new_class,
            observed_params):
        # To operate in batch, this needs to sample from every
        # class-specific generator, but mask off the ones that weren't
        # actually selected.
        # Unfortunately, that's pretty ugly and super-wasteful, but I
        # suspect the batch speedup will still be worth it.

        sampled_params_components = []
        for class_i in range(self.num_classes):
            def sample_params():
                params_means = pyro.param(
                    "params_means_{}_{}".format(object_i, class_i),
                    torch.zeros(self.num_params_by_class[class_i]))
                params_vars = pyro.param(
                    "params_vars_{}_{}".format(object_i, class_i),
                    torch.ones(self.num_params_by_class[class_i]),
                    constraint=constraints.positive)
                return pyro.sample(
                    "params_{}_{}".format(object_i, class_i),
                    dist.Normal(params_means, params_vars).to_event(1),
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
        print "one_hot: ", one_hot.shape, one_hot
        print "encoded components: ", encoded_components[0].shape, encoded_components
        stacked_components = torch.stack(encoded_components, dim=1)
        print "Stacked components: ", stacked_components.shape, stacked_components
        collapsed_components = one_hot.matmul(stacked_components).view(
            new_class.shape[0], self.context_size)
        return collapsed_components

    def _update_context(self, context, encoded_params):
        return context + encoded_params

    def _sample_single_object(self, object_i, data, batch_size, context):
        # Sample the new object type
        observed_new_class = self._extract_new_class(data, object_i)
        new_class = self._sample_new_class(
            object_i, batch_size, context, observed_new_class)

        # Generate an object of that type.
        observed_params = self._extract_params(
            data, object_i)
        sampled_params = self._sample_class_specific_generators(
            object_i, batch_size, context, new_class, observed_params)
        print "Generated classes: ", new_class
        for class_i in range(self.num_classes):
            print "Class {}:".format(class_i)
            print sampled_params[class_i]
            print "vs observed: ",
            print observed_params[class_i]
        # Update the context by encoding the new params
        # into a fixed-size vector through a class-specific encoder.
        encoded_params = self._apply_class_specific_encoders(
            context, new_class, sampled_params)
        context = self._update_context(
            context, encoded_params)
        return context

    def model(self, data=None):
        for class_i in range(self.num_classes):
            pyro.module("class_encoder_module_{}".format(class_i),
                        self.class_encoders[class_i])
        if data is None:
            data_batch_size = 1
        else:
            data_batch_size = data.batch_size

        with pyro.plate('data', size=data_batch_size) as subsample_inds:
            if data is not None:
                data_sub = dataset_utils.SubsampleVectorizedEnvironments(
                    data, subsample_inds)
            minibatch_size = len(subsample_inds)
            context = self._create_empty_context(minibatch_size)

            # Because any given row in the batch might produce
            # all of the objects, we must iterate over all of the
            # generation steps and mask if we're on a stop
            # where the generator said to stop.
            for object_i in range(self.max_num_objects):
                observed_keep_going = self._extract_keep_going(data, object_i)
                keep_going = self._sample_keep_going(
                    object_i, minibatch_size, context, observed_keep_going)

                # Do a generation step
                context = poutine.mask(
                    lambda: self._sample_single_object(
                        object_i, data, minibatch_size, context),
                    keep_going)()


if __name__ == "__main__":
    pyro.enable_validation(True)

    file = "../data/planar_bin/planar_bin_static_scenes.yaml"
    scenes_dataset = dataset_utils.ScenesDatasetVectorized(file)
    data = scenes_dataset.get_full_dataset()

    model = MultiObjectMultiClassModel(scenes_dataset)

    log_dir = "runs/pmomc2/" + datetime.datetime.now().strftime(
        "%Y-%m-%d-%H-%m-%s")
    writer = SummaryWriter(log_dir)

    pyro.clear_param_store()
    trace = poutine.trace(model.model).get_trace()
    trace.compute_log_prob()
    print "MODEL WITH NO ARGS RUN SUCCESSFULLY"
    # print(trace.format_shapes())

    pyro.clear_param_store()
    trace = poutine.trace(model.model).get_trace(
        dataset_utils.SubsampleVectorizedEnvironments(
            data, [0, 1, 2]))
    trace.compute_log_prob()
    print "MODEL WITH ARGS RUN SUCCESSFULLY"
    #print(trace.format_shapes())

    pyro.clear_param_store()
    trace = poutine.trace(model.generation_guide).get_trace(data, subsample_size=5)
    trace.compute_log_prob()
    print "GUIDE WITH ARGS RUN SUCCESSFULLY"
    #print(trace.format_shapes())
