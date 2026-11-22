"""pytest plugin: record which sqlglot classes each test instantiates (with their MRO).

Wraps __init__ of the expression, parser, generator and tokenizer base classes, so a
class-level edit (arg_types, _sql_names, TRANSFORMS, FUNCTIONS, KEYWORDS, ...) can be
mapped to the tests that actually build an instance of that class or a subclass.

Load with: -p tools.trace_classes_plugin ; writes $TRACE_CLASSES_OUT.<worker>.json
as {nodeid: ["sqlglot/path.py:QualName", ...]}.
"""

import json
import os
import sys

import pytest

_current = [None]
_seen = {}
_types = set()
_names = {}


def _name(cls):
    n = _names.get(cls)
    if n is None:
        mod = sys.modules.get(cls.__module__)
        f = getattr(mod, "__file__", "") or ""
        i = f.rfind("/sqlglot/")
        n = f"{f[i + 1:]}:{cls.__qualname__}" if i >= 0 and "/tests/" not in f else ""
        _names[cls] = n
    return n


def _wrap(base):
    orig = base.__init__

    def __init__(self, *a, **k):
        if _current[0] is not None:
            _types.add(type(self))
        return orig(self, *a, **k)

    base.__init__ = __init__


def pytest_configure(config):
    from sqlglot.expressions.core import Expr
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    for b in (Expr, Parser, Generator, Tokenizer):
        _wrap(b)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _current[0] = item.nodeid
    _types.clear()
    yield
    names = set()
    for cls in _types:
        for c in cls.__mro__:
            n = _name(c)
            if n:
                names.add(n)
    _seen[item.nodeid] = sorted(names)
    _current[0] = None


def pytest_unconfigure(config):
    out = os.environ.get("TRACE_CLASSES_OUT")
    if out:
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        with open(f"{out}.{worker}.json", "w") as fh:
            json.dump(_seen, fh)
