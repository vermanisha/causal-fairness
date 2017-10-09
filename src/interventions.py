import torch
from torch.autograd import Variable
import copy

from utils import combine_variables


class Interventions:
    """Manage and create training data sets for interventions."""

    # Methods for creating intervened samples
    known_functions = {
        'randn': (lambda self, mean, var:
                  torch.randn(self.n_samples, 1) * var + mean),
        'const': (lambda self, const:
                  torch.ones(self.n_samples, 1) * const),
        'rand': (lambda self, start, end:
                 torch.rand(self.n_samples, 1) * (start - end) + end),
        'range': (lambda self, a, b:
                  torch.linspace(a, b, steps=self.n_samples).unsqueeze_(1)),
        'bernoulli': (lambda self, p:
                      torch.bernoulli(torch.ones(self.n_samples, 1) * p)),
    }

    def __init__(self, sem, base_sample, intervention_spec, target='Y'):
        """
        Initialize with a base sample and intervention specification.

        Arguments:

            sem: A structural equation model `SEM` from the `sem` module

            base_sample: A base_sample (dict) as generated by `sem.sample`

            interventions_spec: This is our self made format to specify
            interventions. In a dict, for each proxy variable, we store another
            dict, which we call `functions`. In `functions`, keys are preset
            strings that correspond to the `known_functions` in the
            `Intervention` class. Current options: 'randn', 'rand', 'const',
            'range' Every value of `functions` must be a list of tuples (!),
            where the tuples hold one or multiple scalar arguments (depending
            on the key).

            target: The vertex that needs correction (default: 'Y')

        Example:

            A valid intervention_spec could look like:

                intervention_spec = {
                    'P': {'randn': [(0, 3), (0, 3)],
                          'const': [(1,), (0,)],
                          'range': [(-1, 1)]
                         },
                    'X': {'randn': [(0, 1), (0, 1), (0, 1)]
                         },
                    }
        """
        self.base_sample = base_sample
        self.n_samples = len(next(iter(base_sample.values())))
        self.interventions = intervention_spec
        self.proxies = list(intervention_spec.keys())
        self.sem = sem
        self.intervened_graph = self.sem.get_intervened_graph(self.proxies)
        self.target = target
        self._set_n_interventions()
        self.training_samples = []
        self._check_input()

    def _check_input(self):
        """Some basic checks of the input."""
        assert self.target in self.sem.leafs(), \
            "Can't correct for non-leaf {}".format(self.target)

        for proxy in self.proxies:
            assert self.target in self.sem.descendents(proxy), \
                ("Can't correct for non-descendent {} of {}."
                 .format(self.target, proxy))

    def _set_n_interventions(self):
        """Set the total number of interventions, i.e. training sets."""
        self.n_interventions = 1
        for proxy, funcs in self.interventions.items():
            n_proxy = 0
            for params in funcs.values():
                if not isinstance(params, list):
                    params = [params]
                n_proxy += len(params)
            self.n_interventions *= n_proxy

    def _create_intervened_samples(self):
        """For each intervention get a sample with the right proxy values."""
        self.training_samples = []
        print("Initialize training samples with intervened values...", end=' ')
        for proxy, functions in self.interventions.items():
            for func, parameters in functions.items():
                if not isinstance(parameters, list):
                    parameters = [parameters]
                for params in parameters:
                    sample = copy.deepcopy(self.base_sample)
                    sample[proxy] = self.known_functions[func](self, *params)
                    self.training_samples.append(sample)
        print("DONE")

    def _update(self):
        """Update the variables downstream of the proxies."""
        exclude = self.intervened_graph.roots() + [self.target]
        update = [v for v in self.intervened_graph.topological_sort()
                  if v not in exclude]

        print("Predict non-roots {} in all intervened samples."
              .format(update))
        for sample in self.training_samples:
            self.sem.predict_from_sample(sample, update=update, mutate=True)
        print("All intervened samples updated.")

    def _set_training_samples(self):
        """Generate the training samples for the given interventions."""
        self._create_intervened_samples()
        self._update()

    def _copy_and_freeze(self, model, biases):
        """Copy a learned model and partially freeze parameters."""
        # Copy the original model
        corrected = copy.deepcopy(model)

        # First freeze all parameters
        for param in corrected.parameters():
            param.requires_grad = False

        # Only give gradients to the part that is retrained for correction
        for i, v in enumerate(self.intervened_graph.parents(self.target)):
            if v in self.proxies:
                if biases:
                    for param in corrected.layers[0][i].parameters():
                        param.requires_grad = True
                else:
                    corrected.layers[0][i].weight.requires_grad = True
        return corrected

    def train_corrected(self, batchsize=32, epochs=50, biases=False, **kwargs):
        # Some basic input checks
        target = self.target
        proxies = self.proxies
        parents = self.intervened_graph.parents(target)
        print("Correct for the effect of {} on {}.".format(proxies, target))

        print("Generate intervened samples.")
        self._set_training_samples()
        print("All intervened samples ready for training.")

        # Sanity check
        assert len(self.training_samples) == self.n_interventions, \
            ("# interventions {} does not match # training samples {}"
             .format(self.n_interventions, self.training_samples))
        print("There are {} interventions.".format(len(self.training_samples)))

        print("Freeze everything except first weights from {} to {}..."
              .format(proxies, target), end=' ')
        corrected = self._copy_and_freeze(self.sem.learned[target], biases)
        print("DONE")

        print("Set up the optimizer...", end=' ')
        opt = torch.optim.Adam(filter(lambda p:
                                      p.requires_grad, corrected.parameters()),
                               **kwargs)
        print("DONE")

        print("Partially retrain the target model for correction...", end=' ')
        n_samples = self.n_samples
        for epoch in range(epochs):
            p = torch.randperm(n_samples).long()
            # Go through batches
            for i1 in range(0, n_samples, batchsize):
                i2 = min(i1 + batchsize, n_samples)
                # Reset gradients
                opt.zero_grad()
                # Forward pass
                Ys = Variable(torch.zeros(batchsize, self.n_interventions))
                for i, sample in enumerate(self.training_samples):
                    data = combine_variables(parents, sample)[p]
                    args = Variable(data[i1:i2, :])
                    Ys[:, i] = corrected(args).squeeze()
                # Compute loss
                loss = torch.sum(torch.var(Ys, dim=0))
                # Backward pass
                loss.backward()
                # Parameter update
                opt.step()
        print("DONE")
        print("Finished correction.")
        return corrected

    def summary(self):
        print("Sample size: {}, Number of interventions {}"
              .format(self.n_samples, self.n_interventions))
        print(self.interventions)
