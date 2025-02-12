##^# ops import and utils ######################################################
import os, pickle, time as time_module, pdb, math
from pprint import pprint
from collections import OrderedDict as odict
from operator import itemgetter, mul
from functools import reduce

from .jax_friendly_interface import init

jaxm = init()

import numpy as np
from torch.utils.tensorboard import SummaryWriter


##$#############################################################################
##^# general utils #############################################################
vec = lambda x: x.reshape(-1)
identity = lambda x: x
is_equal = (
    lambda a, b: (type(a) == type(b))
    and (a.shape == b.shape)
    and (jaxm.norm(a - b) / math.sqrt(a.numel()) < 1e-7)
)
prod = lambda zs: reduce(mul, zs) if zs != () else 1


def normalize(x, dim=-2, params=None, min_std=1e-3):
    if params is None:
        x_mu = jaxm.mean(x, dim, keepdims=True)
        x_std = jaxm.maximum(
            jaxm.std(x, dim, keepdims=True), jaxm.array(min_std)
        )
    else:
        x_mu, x_std = params
    return (x - x_mu) / x_std, (x_mu, x_std)


unnormalize = lambda x, params: x * params[1] + params[0]

t2j = lambda x: jaxm.array(x.detach())
j2n = lambda x: np.array(x)
n2j = lambda x: jaxm.array(x)


def scale_down(X, size=2, width=None, height=None):
    kernel = jaxm.ones((1, 1, size, size)) / (size ** 2)

    assert X.ndim == 2 or X.ndim == 3 or (X.ndim == 4 and X.shape[1] == 1)
    if X.ndim == 2:
        height = width if width is not None else round(math.sqrt(X.shape[-1]))
        width = X.shape[-1] // height
        Z = X.reshape((X.shape[0], 1, height, width))
    elif X.ndim == 3:
        height, width = X.shape[-2:]
        Z = X[:, None, :, :]
    elif X.ndim == 4:
        assert X.shape[1] == 1
        height, width = X.shape[-2:]
        Z = X  # do nothing

    Z = jaxm.lax.conv(Z, kernel, (size, size), "VALID")
    Z = Z.reshape((Z.shape[0], Z.shape[1], height // size, width // size))

    if X.ndim == 2:
        Z = Z.reshape((X.shape[0], -1))
    elif X.ndim == 3:
        Z = Z[:, 0, :, :]
    elif X.ndim == 4:
        Z = Z

    return Z


##$#############################################################################
##^# table printing utility class ##############################################
class TablePrinter:
    def __init__(self, names, fmts=None, prefix="", use_writer=False):
        self.names = names
        self.fmts = fmts if fmts is not None else ["%9.4e" for _ in names]
        self.widths = [
            max(self.calc_width(fmt), len(name)) + 2
            for (fmt, name) in zip(fmts, names)
        ]
        self.prefix = prefix
        self.writer = None
        if use_writer:
            try:
                self.writer = SummaryWriter(flush_secs=1)
                self.iteration = 0
            except NameError:
                print("SummaryWriter not available, ignoring")

    def calc_width(self, fmt):
        f = fmt[-1]
        width = None
        if f == "f" or f == "e" or f == "d" or f == "i":
            width = max(len(fmt % 1), len(fmt % (-1)))
        elif f == "s":
            width = len(fmt % "")
        else:
            raise ValueError("I can't recognized the [%s] print format" % fmt)
        return width

    def pad_field(self, s, width, lj=True):
        # lj -> left justify
        assert len(s) <= width
        rem = width - len(s)
        if lj:
            return (" " * (rem // 2)) + s + (" " * ((rem // 2) + (rem % 2)))
        else:
            return (" " * ((rem // 2) + (rem % 2))) + s + (" " * (rem // 2))

    def make_row_sep(self):
        return "+" + "".join([("-" * width) + "+" for width in self.widths])

    def make_header(self):
        s = self.prefix + self.make_row_sep() + "\n"
        s += self.prefix
        for (name, width) in zip(self.names, self.widths):
            s += "|" + self.pad_field("%s" % name, width, lj=True)
        s += "|\n"
        return s + self.prefix + self.make_row_sep()

    def make_footer(self):
        return self.prefix + self.make_row_sep()

    def make_values(self, vals):
        assert len(vals) == len(self.fmts)
        s = self.prefix + ""
        for (val, fmt, width) in zip(vals, self.fmts, self.widths):
            s += "|" + self.pad_field(fmt % val, width, lj=False)
        s += "|"

        if self.writer is not None:
            for (name, val) in zip(self.names, vals):
                self.writer.add_scalar(name, val, self.iteration)
            self.iteration += 1

        return s

    def print_header(self):
        print(self.make_header())

    def print_footer(self):
        print(self.make_footer())

    def print_values(self, vals):
        print(self.make_values(vals))


##$#############################################################################
##^# solution caching decorator ################################################
def to_tuple_(arg):
    if isinstance(arg, np.ndarray):
        return arg.tobytes()
    elif isinstance(arg, jaxm.DeviceArray):
        return to_tuple_(np.array(arg))
    else:
        return to_tuple_(np.array(arg))


def to_tuple(*args):
    return tuple(to_tuple_(arg) for arg in args)


def fn_with_sol_cache(fwd_fn, cache=None, jit=True):
    def inner_decorator(fn):
        nonlocal cache
        cache = cache if cache is None else cache
        fwd_fn_ = fwd_fn  # assume already jit-ed

        def fn_with_sol(*args, **kwargs):
            cache, sol_key = fn_with_sol.cache, to_tuple(*args)
            sol = fwd_fn_(*args) if not sol_key in cache else cache[sol_key]
            cache.setdefault(sol_key, sol)
            ret = fn_with_sol.fn(sol, *args, **kwargs)
            return ret

        fn_with_sol.cache = cache
        fn_with_sol.fn = jaxm.jit(fn) if jit else fn
        return fn_with_sol

    return inner_decorator


##$#############################################################################
