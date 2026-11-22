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
        if _current[0] is not None and not (_in_init[0] and self.name in _INIT_NAMES):
            cls = type(obj) if obj is not None else owner
            _seen.add((cls, self.name))
        return self.value


# Tokenizer settings are copied into a TokenizerCore by Tokenizer._init_core, so every
# test that builds a tokenizer "reads" all of them. What matters is which core fields the
# scan actually consults (nested_comments only on a block comment, numeric_escapes only on
# an escape...). Reads inside _init_core of the copied names are not recorded; a read of a
# core field during Tokenizer.tokenize is recorded as a read of the settings it came from,
# on the tokenizer's class (self.X) or its dialect's class (self.dialect.X).
_in_init = [0]
_INIT_NAMES = set()
_ctx = []
_unctx = set()


class CoreRec:
    __slots__ = ("field", "md", "names")

    def __init__(self, field, md, names):
        self.field = field
        self.md = md
        self.names = names

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        value = self.md.__get__(obj, owner)
        if isinstance(value, _REC_TYPES):
            return value  # recorded when it is looked into, not when it is fetched
        if _current[0] is not None:
            if _ctx:
                tok, dia = _ctx[-1]
                for src, a in self.names:
                    _seen.add((tok if src == "self" else dia, a))
            else:
                _unctx.add(self.field)
        return value

    def __set__(self, obj, value):
        self.md.__set__(obj, value)


# Container settings (NUMERIC_ESCAPES, QUOTES...) are often fetched into a local once per
# string and only consulted on an escape or a match: give each core its own recording
# copy, so a read counts when the container is actually looked into.
def _rec(keys):
    if _current[0] is not None:
        _seen.update(keys)


class RecDict(dict):
    __slots__ = ("_k",)

    def get(self, *a):
        _rec(self._k)
        return dict.get(self, *a)

    def __getitem__(self, k):
        _rec(self._k)
        return dict.__getitem__(self, k)

    def __contains__(self, k):
        _rec(self._k)
        return dict.__contains__(self, k)

    def __iter__(self):
        _rec(self._k)
        return dict.__iter__(self)

    def __len__(self):
        _rec(self._k)
        return dict.__len__(self)

    def keys(self):
        _rec(self._k)
        return dict.keys(self)

    def values(self):
        _rec(self._k)
        return dict.values(self)

    def items(self):
        _rec(self._k)
        return dict.items(self)


def _rec_set(base):
    class R(base):
        __slots__ = ("_k",)

        def __contains__(self, k):
            _rec(self._k)
            return base.__contains__(self, k)

        def __iter__(self):
            _rec(self._k)
            return base.__iter__(self)

        def __len__(self):
            _rec(self._k)
            return base.__len__(self)

    R.__name__ = f"Rec{base.__name__}"
    return R


RecSet, RecFrozenSet = _rec_set(set), _rec_set(frozenset)
_REC_TYPES = (RecDict, RecSet, RecFrozenSet)
_REC_OF = {dict: RecDict, set: RecSet, frozenset: RecFrozenSet}


def _wrap_containers(core, tok, dia):
    for field, d in _CORE_FIELDS.items():
        v = d.md.__get__(core, type(core))
        cls = _REC_OF.get(type(v))
        if cls is None:
            continue
        w = cls(v)
        w._k = tuple((tok if src == "self" else dia, a) for src, a in d.names)
        d.md.__set__(core, w)


_CORE_FIELDS = {}


def core_field_map():
    """{core field: [("self"|"dialect", SETTING)]} from the TokenizerCore(...) call."""
    import ast
    import textwrap

    from sqlglot.tokens import Tokenizer

    fn = ast.parse(textwrap.dedent(inspect.getsource(Tokenizer._init_core))).body[0]
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "TokenizerCore"]
    out = {}
    if len(calls) != 1 or calls[0].args:
        return out
    for kw in calls[0].keywords:
        names = []
        for n in ast.walk(kw.value):
            if not isinstance(n, ast.Attribute):
                continue
            v = n.value
            if isinstance(v, ast.Name) and v.id == "self" and n.attr != "dialect":
                names.append(("self", n.attr))
            elif (isinstance(v, ast.Attribute) and v.attr == "dialect"
                  and isinstance(v.value, ast.Name) and v.value.id == "self"):
                names.append(("dialect", n.attr))
        out[kw.arg] = names
    return out


def install_core_tracing():
    from sqlglot import tokenizer_core, tokens

    Tok, Core = tokens.Tokenizer, tokenizer_core.TokenizerCore
    if getattr(Tok, "_br_core_traced", False):
        return
    overriders = [c for c in _all_subclasses(Tok) if "_init_core" in vars(c) and c is not Tok]
    if overriders:
        print(f"trace_attrs: _init_core overridden in {overriders}; core tracing off", file=sys.stderr)
        return
    fmap = core_field_map()
    n = 0
    for field, names in fmap.items():
        md = Core.__dict__.get(field)
        if md is None or not hasattr(md, "__set__") or not names:
            continue
        _CORE_FIELDS[field] = CoreRec(field, md, tuple(names))
        setattr(Core, field, _CORE_FIELDS[field])
        _INIT_NAMES.update(a for _, a in names)
        n += 1
    orig_init, orig_tok = Tok._init_core, Tok.tokenize

    def _init_core(self):
        _in_init[0] += 1
        try:
            core = orig_init(self)
        finally:
            _in_init[0] -= 1
        _wrap_containers(core, type(self), type(self.dialect))
        return core

    def tokenize(self, sql):
        _ctx.append((type(self), type(self.dialect)))
        try:
            return orig_tok(self, sql)
        finally:
            _ctx.pop()

    Tok._init_core = _init_core
    Tok.tokenize = tokenize
    Tok._br_core_traced = True
    print(f"trace_attrs: {n} tokenizer core fields traced", file=sys.stderr)


def _all_subclasses(c):
    seen, todo = set(), [c]
    while todo:
        x = todo.pop()
        if x not in seen:
            seen.add(x)
            todo.extend(x.__subclasses__())
    return seen


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
    install_core_tracing()


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
    # Core fields read outside Tokenizer.tokenize can't be tied to a class; build_meta
    # refuses to build the map if this list is not empty.
    _out["UNCTX"] = sorted(_unctx)
    if _unctx:
        print(f"trace_attrs: core fields read outside tokenize: {sorted(_unctx)}", file=sys.stderr)
    if out:
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        with open(f"{out}.{worker}.json", "w") as fh:
            json.dump(_out, fh)
