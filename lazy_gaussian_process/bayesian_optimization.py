import warnings
import numpy as np

from target_space import TargetSpace
from event import Events, DEFAULT_EVENTS
from logger import _get_default_logger
from util import UtilityFunction, acq_max, ensure_rng

from sklearn.gaussian_process.kernels import Matern

#importing the Gaussian process regressor from sklearn library, it will be used when lazy_gpr = False
from sklearn.gaussian_process import GaussianProcessRegressor

#importing the lazy Gaussian process regressor, t will be used when lazy_gpr = True
from lazy_gaussian_process import GaussianProcessRegressor_lazy

import time

import pickle

class Queue:
    def __init__(self):
        self._queue = []

    @property
    def empty(self):
        return len(self) == 0

    def __len__(self):
        return len(self._queue)

    def __next__(self):
        if self.empty:
            raise StopIteration("Queue is empty, no more objects to retrieve.")
        obj = self._queue[0]
        self._queue = self._queue[1:]
        return obj

    def next(self):
        return self.__next__()

    def add(self, obj):
        """Add object to end of queue."""
        self._queue.append(obj)


class Observable(object):
    """
    Inspired/Taken from
        https://www.protechtraining.com/blog/post/879#simple-observer
    """
    def __init__(self, events):
        # maps event names to subscribers
        # str -> dict
        self._events = {event: dict() for event in events}

    def get_subscribers(self, event):
        return self._events[event]

    def subscribe(self, event, subscriber, callback=None):
        if callback == None:
            callback = getattr(subscriber, 'update')
        self.get_subscribers(event)[subscriber] = callback

    def unsubscribe(self, event, subscriber):
        del self.get_subscribers(event)[subscriber]

    def dispatch(self, event):
        for _, callback in self.get_subscribers(event).items():
            callback(event, self)

class BayesianOptimization(Observable):
    def __init__(self, f, pbounds, random_state=None, verbose=2, lazy_gpr=False, lag=10000):

        """
        this is a comment
        """
        self._random_state = ensure_rng(random_state)

        # Data structure containing the function to be optimized, the bounds of
        # its domain, and a record of the evaluations we have done so far
        self._space = TargetSpace(f, pbounds, random_state)

        # queue
        self._queue = Queue()

        if(lazy_gpr):
            # Internal lazy GP regressor
            self._gp = GaussianProcessRegressor_lazy(
                kernel=Matern(nu=2.5),
                alpha=1e-6,
                normalize_y=True,
                n_restarts_optimizer=1,
                random_state=self._random_state,
                lag=lag
                #optimizer=None
            )
        else:
            # Internal GP regressor
            self._gp = GaussianProcessRegressor(
                kernel=Matern(nu=2.5),
                alpha=1e-6,
                normalize_y=True,
                n_restarts_optimizer=1,
                random_state=self._random_state
                #optimizer=None
            )

        self._verbose = verbose
        super(BayesianOptimization, self).__init__(events=DEFAULT_EVENTS)

    @property
    def space(self):
        return self._space

    @property
    def max(self):
        return self._space.max()

    @property
    def res(self):
        return self._space.res()

    def get_lower_L(self):
        return self._gp.L

    def register(self, params, target):
        """Expect observation with known target"""
        self._space.register(params, target)
        self.dispatch(Events.OPTMIZATION_STEP)

    def probe(self, params, lazy=True):
        """Probe target of x"""
        x = 0
        if lazy:
            self._queue.add(params)
        else:
            x = self._space.probe(params)
            self.dispatch(Events.OPTMIZATION_STEP)
        return x

    def suggest(self, utility_function):
        """Most promissing point to probe next"""
        if len(self._space) == 0:
            return self._space.array_to_params(self._space.random_sample())

        # Sklearn's GP throws a large number of warnings at times, but
        # we don't really need to see them here.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._gp.fit(self._space.params, self._space.target)

        # Finding argmax of the acquisition function.
        suggestion = acq_max(
            ac=utility_function.utility,
            gp=self._gp,
            y_max=self._space.target.max(),
            bounds=self._space.bounds,
            random_state=self._random_state
        )

        return self._space.array_to_params(suggestion)

    def _prime_queue(self, init_points):
        """Make sure there's something in the queue at the very beginning."""
        if self._queue.empty and self._space.empty:
            init_points = max(init_points, 1)

        for _ in range(init_points):
            self._queue.add(self._space.random_sample())

    def _prime_subscriptions(self):
        if not any([len(subs) for subs in self._events.values()]):
            _logger = _get_default_logger(self._verbose)
            self.subscribe(Events.OPTMIZATION_START, _logger)
            self.subscribe(Events.OPTMIZATION_STEP, _logger)
            self.subscribe(Events.OPTMIZATION_END, _logger)

    def maximize(self,
                 init_points=5,
                 n_iter=25,
                 acq='ucb',
                 kappa=2.576,
                 xi=0.01,
                 samples=None,
                 eps = 0.2,
                 solution = 1,
                 **gp_params):
        """Mazimize your function"""
        self._prime_subscriptions()
        self.dispatch(Events.OPTMIZATION_START)
        self._prime_queue(init_points) #add random points to the queue
        self.set_gp_params(**gp_params)

        util = UtilityFunction(kind=acq, kappa=kappa, xi=xi)
        iteration = 0

        total_time = 0
        while not self._queue.empty or iteration < n_iter:
            try:
                x_probe = next(self._queue)
                # print(self._gp.kernel.theta)
            except StopIteration:
                tstart = time.time()
                x_probe = self.suggest(util)
                tend = time.time()
                total_time += (tend-tstart)
                iteration += 1
                # print(self._gp.kernel.theta)

            y = self.probe(x_probe, lazy=False)

            if(abs(y - solution) < eps):
               print ("Breaking the loop now ...")
               break

        # if samples != None :
        #     with open("samples.pickle", "rb") as f:
        #     epochs, wd, lr, m, acc = pickle.load(f)
        #     for _ in range(len(epochs)):
        #         self._space.register_seeds(x, params)
        #         self.dispatch(Events.OPTMIZATION_STEP)

        self.dispatch(Events.OPTMIZATION_END)

    def set_bounds(self, new_bounds):
        """
        A method that allows changing the lower and upper searching bounds

        Parameters
        ----------
        new_bounds : dict
            A dictionary with the parameter name and its new bounds
        """
        self._space.set_bounds(new_bounds)

    def set_gp_params(self, **params):
        self._gp.set_params(**params)
