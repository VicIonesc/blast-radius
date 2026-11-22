"""pytest plugin: run ProcessPoolExecutor work inline, in the test's own process.

test_optimizer and test_executor hand their fixture cases to spawned worker processes,
which coverage contexts and our traces cannot see. During map collection we swap those
pools for a synchronous executor so every line they run is credited to the right test.
"""

import pickle
import sys
from concurrent.futures import Future


def _cross(obj):
    """What a spawned worker would see: the object after a pickle round trip
    (sqlglot expressions pickle through sqlglot.serde)."""
    return pickle.loads(pickle.dumps(obj))


class InlineExecutor:
    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*_cross(tuple(initargs)))

    def submit(self, fn, *args, **kwargs):
        fut = Future()
        try:
            args, kwargs = _cross((args, kwargs))
            fut.set_result(_cross(fn(*args, **kwargs)))
        except BaseException as e:  # noqa: BLE001 - mirror executor semantics
            fut.set_exception(e)
        return fut

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [_cross(fn(*_cross(a))) for a in zip(*iterables)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch():
    for name, mod in list(sys.modules.items()):
        if name.startswith("tests") and getattr(mod, "ProcessPoolExecutor", None) is not None:
            mod.ProcessPoolExecutor = InlineExecutor


def pytest_collection_finish(session):
    _patch()
