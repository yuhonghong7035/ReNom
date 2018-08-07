#!/usr/bin/env python
# encoding: utf-8
from __future__ import division, print_function
import numpy as np
from renom.core import get_gpu, Node, Variable
from renom.operation import sqrt, square
from renom.cuda.cuda import is_cuda_active
from abc import ABCMeta, abstractmethod
from future.utils import with_metaclass
from renom.cuda import cuda as cu


class Optimizer(with_metaclass(ABCMeta, object)):

    _updates_node = False
    # Called by update_node in core.py
    def __call__(self, *args, **kwargs):
        if is_cuda_active():
            return self._get_gpu(*args, **kwargs)
        else:
            return self._get_cpu(*args, **kwargs)

    @abstractmethod
    def _get_cpu(self, *args, **kwargs):
        pass

    @abstractmethod
    def _get_gpu(self, *args, **kwargs):
        pass

    @property
    def updates_node(self):
        return self._updates_node == True


class Sgd(Optimizer):
    '''Stochastic Gradient Descent.

    Args:
        lr (float): Learning rate.
        momentum (float): Momentum coefficient of optimization.

    Example:
        >>> import numpy as np
        >>> import renom as rm
        >>> x = rm.Variable(np.random.rand(2, 3))
        >>> x
        Variable([[ 0.93283856,  0.44494787,  0.47652033],
                  [ 0.04769089,  0.16719061,  0.52063918]], dtype=float32)
        >>> a = 2
        >>> opt = rm.Sgd(lr=0.1)    # Stochastic gradient decent algorithm
        >>> y = rm.sum(a*x)
        >>> dx = y.grad(detach_graph=False).get(x)
        >>> dx
        RMul([[ 2.,  2.,  2.],
              [ 2.,  2.,  2.]], dtype=float32)
        >>> y.grad(detach_graph=False).update(opt)
        >>> x
        Variable([[ 0.73283857,  0.24494787,  0.27652031],
                  [-0.1523091 , -0.03280939,  0.32063919]], dtype=float32)
    '''

    def __init__(self, lr=0.1, momentum=0.4):
        self._updates_node = True
        self._lr = lr
        self._momentum = momentum
        self._params = {}

    def _get_cpu(self, dy, node):

        node_id = id(node)
        pdy = self._params.get(node_id, 0)
        ret = self._lr * dy + self._momentum * pdy
        if self._momentum > 0:
            self._params[node_id] = ret
        if isinstance(ret, Node):
            ret.detach_graph()
        return ret

    def _get_gpu(self, dy, node):

        node_id = id(node)
        pdy = self._params.get(node_id, get_gpu(dy).zeros_like_me())
        ndy = get_gpu(dy).empty_like_me()
        cu.cu_optimizer_sgd(self._lr, self._momentum, get_gpu(dy), get_gpu(pdy), ndy, get_gpu(node))

        if self._momentum > 0:
            self._params[node_id] = ndy
        return ndy

    def reset(self):
        self._params = {}


class Adagrad(Optimizer):
    '''Adaptive gradient algorithm. [Adagrad]_

    Args:
        lr (float): Learning rate.
        epsilon (float): Small number in the equation for avoiding zero division.

    .. [Adagrad] Duchi, J., Hazan, E., & Singer, Y. Adaptive Subgradient Methods for
        Online Learning and Stochastic Optimization. Journal of Machine Learning Research, 12, 2121–2159.
    '''

    def __init__(self, lr=0.01, epsilon=1e-8):
        self._lr = lr
        self._epsilon = epsilon
        self._params = {}

    def _get_cpu(self, dy, node):
        node_id = id(node)
        pdy = self._params.get(node_id, 0)
        r = pdy + dy * dy
        ret = self._lr * dy / (np.sqrt(r) + self._epsilon)
        self._params[node_id] = r
        if isinstance(ret, Node):
            ret.detach_graph()
        return ret

    def _get_gpu(self, dy, node):
        node_id = id(node)
        pdy = self._params.get(node_id, get_gpu(dy).zeros_like_me())
        ndy = get_gpu(dy).empty_like_me()
        r = get_gpu(pdy).empty_like_me()
        cu.cu_optimizer_adagrad(self._lr, self._epsilon, get_gpu(dy), get_gpu(pdy), ndy, r)
        self._params[node_id] = r
        return ndy

    def reset(self):
        self._params = {}


class Adadelta(Optimizer):
    '''Adaptive gradient algorithm. [Adagrad]_

    Args:
        dr (float): Decay rate.
        epsilon (float): Small number in the equation for avoiding zero division.

    .. [Adagrad] Duchi, J., Hazan, E., & Singer, Y. Adaptive Subgradient Methods for
        Online Learning and Stochastic Optimization. Journal of Machine Learning Research, 12, 2121–2159.
    '''

    def __init__(self, dr=0.95, epsilon=1e-8):
        self._updates_node = True
        self._dr = dr
        self._epsilon = epsilon
        self._params = {}

    def _get_cpu(self, dy, node):
        node_id = id(node)
        pdy = self._params.get(node_id, None)
        if pdy is None:
            psg = 0
            psx = 0
        else:
            psg = pdy['psg']      # E_squared_grad[t-1]
            psx = pdy['psx']      # E_squared_x[t-1]
        dr = self._dr
        E_squared_grad = dr * psg + (1 - dr) * np.square(dy)
        dx = np.sqrt(psx + self._epsilon) / np.sqrt(E_squared_grad + self._epsilon) * dy
        E_squared_x = dr * psx + (1 - dr) * np.square(dx)

        ret = dx
        self._params[node_id] = {
            'psg': E_squared_grad,
            'psx': E_squared_x,
        }

        if isinstance(ret, Node):
            ret.detach_graph()
        return ret

    def _get_gpu(self, dy, node):
        node_id = id(node)
        pdy = self._params.get(node_id, None)
        if pdy is None:
            psg = get_gpu(dy).zeros_like_me()
            psx = get_gpu(dy).zeros_like_me()
        else:
            psg = pdy['psg']
            psx = pdy['psx']
        dr = self._dr
        eps = self._epsilon
        ndy = get_gpu(node)
        cu.cu_optimizer_adadelta(dr, eps, psg, psx, get_gpu(dy), ndy)
        ret = ndy
        self._params[node_id] = {
            'psg': psg,
            'psx': psx,
        }

        if isinstance(ret, Node):
            ret.detach_graph()
        return ret

    def reset(self):
        self._params = {}

class Rmsprop(Optimizer):
    '''Rmsprop described by following formula. [Rmsprop]_

    .. math::

        m_{t+1} &=& gm_{t} + (1-g)\\nabla E^2 \\\\
        r_{t} &=& \\frac{lr}{\sqrt{m_{t+1}}+\epsilon} \\\\
        w_{t+1} &=& w_{t} - r_{t}\\nabla E

    Args:
        lr (float): Learning rate.
        g (float):
        epsilon (float): Small number in the equation for avoiding zero division.

    .. [Rmsprop] Nitish Srivastava, Kevin Swersky, Geoffrey Hinton. Neural Networks for Machine Learning.
    '''

    def __init__(self, lr=0.001, g=0.9, epsilon=1e-8):
        self._lr = lr
        self._g = g
        self._epsilon = epsilon
        self._params = {}

    def _get_cpu(self, dy, node):
        node_id = id(node)
        pdy = self._params.get(node_id, 0)
        r = self._g * pdy + (1 - self._g) * (dy**2)
        ret = self._lr * dy / (sqrt(r) + self._epsilon)
        self._params[node_id] = r
        if isinstance(ret, Node):
            ret.detach_graph()
        return ret

    def _get_gpu(self, dy, node):
        node_id = id(node)
        pdy = self._params.get(node_id, get_gpu(dy).zeros_like_me())
        ndy = get_gpu(dy).empty_like_me()
        r = get_gpu(pdy).empty_like_me()
        cu.cu_optimizer_rmsprop(self._lr, self._epsilon, self._g, get_gpu(dy), get_gpu(pdy), ndy, r)
        self._params[node_id] = r
        return ndy

    def reset(self):
        self._params = {}


class Adam(Optimizer):
    '''Adaptive moment estimation described by following formula. [Adam]_

    .. math::

        m_{t+1} &=& bm_t + \\nabla E \\\\
        n_{t+1} &=& gn_t + \\nabla E^2 \\\\
        \\hat{m}_{t+1} &=& \\frac{m_{t+1}}{1-b^{t+1}} \\\\
        \\hat{n}_{t+1} &=& \\frac{n_{t+1}}{1-g^{t+1}} \\\\
        w_{t+1} &=& w_{t} - \\frac{\\alpha \hat{m}_{t+1}}{\sqrt{\hat{n}_{t+1}}+\epsilon}

    Args:
        lr (float): Learning rate.
        g (float): Coefficient
        b (float): Coefficient
        epsilon (float): Small number in the equation for avoiding zero division.


    .. [Adam] Diederik P. Kingma, Jimmy Ba. ADAM: A METHOD FOR STOCHASTIC OPTIMIZATION(2014)
        https://arxiv.org/pdf/1412.6980.pdf
    '''

    def __init__(self, lr=0.001, g=0.999, b=0.9, epsilon=1e-8):
        self._lr = lr
        self._g = g
        self._b = b
        self._epsilon = epsilon
        self._params = {}
        self._min = 2e-20

    CHECK_ZERO_VALUE = 100

    def _get_cpu(self, dy, node):
        node_id = id(node)
        pdy = self._params.get(node_id, None)
        nth = 0
        if pdy is None:
            b = self._b
            g = self._g
            u = (1 - self._b) * dy
            r = (1 - self._g) * (dy**2)
        else:
            u = pdy["u"]
            r = pdy["r"]
            b = pdy["beta"]
            g = pdy["gamma"]
            nth = pdy["nth"]

            if nth % self.CHECK_ZERO_VALUE == 0:
                if not is_cuda_active():
                    min_flug = np.where(np.abs(u) < self._min, True, False)
                    min_flug = np.where(np.abs(r) < self._min, True, False)
                    u.setflags(write=True)
                    r.setflags(write=True)
                    u[min_flug] = 0
                    r[min_flug] = 0
            u = self._b * u + (1 - self._b) * dy
            r = self._g * r + (1 - self._g) * (dy * dy)

        self._params[node_id] = {"beta": b * self._b,
                                 "gamma": g * self._g,
                                 "u": u,
                                 "r": r,
                                 "nth": nth + 1}

        ret = self._lr * u / (sqrt(r / (1 - g)) + self._epsilon) / (1 - b)
        if isinstance(ret, Node):
            ret.detach_graph()
        return ret

    def _get_gpu(self, dy, node):
        node_id = id(node)
        pdy = self._params.get(node_id, None)
        nth = 0
        if pdy is None:
            b = self._b
            g = self._g
            u = get_gpu(dy).zeros_like_me()
            r = get_gpu(dy).zeros_like_me()
        else:
            u = pdy["u"]
            r = pdy["r"]
            b = pdy["beta"]
            g = pdy["gamma"]
            nth = pdy["nth"]

        ndy = get_gpu(dy).empty_like_me()
        cu.cu_optimizer_adam(self._lr, self._epsilon, g, self._g, b, self._b, self._min, nth %
                             self.CHECK_ZERO_VALUE == 0, get_gpu(u), get_gpu(r), get_gpu(dy), ndy)

        self._params[node_id] = {"beta": b * self._b,
                                 "gamma": g * self._g,
                                 "u": u,
                                 "r": r,
                                 "nth": nth + 1}

        return ndy

    def reset(self):
        self._params = {}
