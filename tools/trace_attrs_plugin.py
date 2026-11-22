"""pytest plugin: record which class attributes (UPPERCASE settings on Dialect, Parser,
Generator and Tokenizer classes) each test reads, keyed by the class it was read through.

A flag like `SUPPORTS_ORDER_BY_ALL = False` on one dialect then maps to the tests that
actually consult it on that dialect (or a subclass), not to every test using the dialect.

Load with -p tools.trace_attrs_plugin; writes $TRACE_ATTRS_OUT.<worker>.json as
{nodeid: ["sqlglot/path.py:Qual.ATTR", ...]}.
"""

import importlib
import inspect
import json
import os
import pkgutil
import sys
from enum import Enum

import pytest

_current = [None]
_seen = set()
_out = {}
_names = {}


def _class_key(cls):
    k = _names.get(cls)
    if k is None:
        mod = sys.modules.get(cls.__module__)
        f = getattr(mod, "__file__", "") or ""
        i = f.rfind("/sqlglot/")
        k = f"{f[i + 1:]}:{cls.__qualname__}" if i >= 0 and "/tests/" not in f else ""
        _names[cls] = k
    return k


class Rec:
    """Non-data descriptor: returns the original value, records (owner, name)."""

    __slots__ = ("name", "value")

    def __init__(self, name, value):
        self.name = name
        self.value = value

    def __get__(self, obj, owner=None):
        if _current[0] is not None:
            cls = type(obj) if obj is not None else owner
            _seen.add((cls, self.name))
        return self.value


def _is_setting(name, value):
    if not (name.isupper() or (name.startswith("_") and name[1:].isupper())):
        return False
    if name.startswith("__"):
        return False
    if inspect.isclass(value) or callable(value) or hasattr(value, "__get__"):
        return False
    return True


def _install():
    import sqlglot
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name != "sqlglot.__main__":
            try:
                importlib.import_module(info.name)
            except Exception:
                pass
    bases = (Dialect, Parser, Generator, Tokenizer)
    seen = set()
    todo = list(bases)
    while todo:
        c = todo.pop()
        if c in seen:
            continue
        seen.add(c)
        todo.extend(c.__subclasses__())
    n = 0
    for cls in seen:
        if issubclass(cls, Enum) or not _class_key(cls):
            continue
        for name, value in list(vars(cls).items()):
            if _is_setting(name, value):
                setattr(cls, name, Rec(name, value))
                n += 1
    print(f"trace_attrs: {n} attributes on {len(seen)} classes", file=sys.stderr)


_collect = set()


def pytest_configure(config):
    _install()
    # Reads while test modules import (module-level parsing etc.) are baked into those
    # modules before any test runs: record them separately, keyed by class.
    _current[0] = "COLLECT"


def pytest_collection_finish(session):
    for cls, name in _seen:
        k = _class_key(cls)
        if k:
            _collect.add(f"{k}.{name}")
    _seen.clear()
    _current[0] = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    try:  # the dispatch table is built once per generator class per process
        import sqlglot.generator as g

        g._DISPATCH_CACHE.clear()
    except Exception:
        pass
    _current[0] = item.nodeid
    _seen.clear()
    yield
    _current[0] = None
    keys = set()
    for cls, name in _seen:
        k = _class_key(cls)
        if k:
            keys.add(f"{k}.{name}")
    _out[item.nodeid] = sorted(keys)


def pytest_unconfigure(config):
    out = os.environ.get("TRACE_ATTRS_OUT")
    _out["COLLECT"] = sorted(_collect)
    if out:
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        with open(f"{out}.{worker}.json", "w") as fh:
            json.dump(_out, fh)
