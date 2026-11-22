"""What a class-level edit changes once everything is imported.

A setting like `NUMERIC_ESCAPES` may be read while classes are being built (a metaclass
deriving ESCAPED_SEQUENCES from it, a trie built from KEYWORDS...). Whether that read
bakes anything different into the loaded program can't be told from the source. So we
load sqlglot and the test modules twice — as they are, in the verifier's parent, and
patched, in a fresh interpreter — fingerprint every class attribute and module global,
and compare. Keys: "path.py:Qual.NAME" for class attributes, "path.py::NAME" for module
globals.
"""

from __future__ import annotations

import enum
import functools
import hashlib
import importlib
import os
import re
import sys
import types

SKIP_CLASS = {"__dict__", "__weakref__", "__module__", "__qualname__", "__firstlineno__",
              "__doc__", "__static_attributes__"}
SKIP_MODULE = {"__builtins__", "__file__", "__cached__", "__spec__", "__loader__", "__path__",
               "__doc__", "__name__", "__package__", "__annotations__"}
_PATTERN = type(re.compile(""))
_OPAQUE = (types.BuiltinFunctionType, types.MethodDescriptorType, types.WrapperDescriptorType,
           types.MemberDescriptorType, types.GetSetDescriptorType, types.ClassMethodDescriptorType,
           types.MethodWrapperType)
MAX_DEPTH = 8


def load_world(sqlglot_dir, test_files):
    """Import every sqlglot module, build every dialect, import the test modules."""
    import pkgutil

    import sqlglot
    from sqlglot.dialects.dialect import Dialect, Dialects

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name != "sqlglot.__main__":
            try:
                importlib.import_module(info.name)
            except Exception:
                pass
    for d in Dialects:
        if d.value:
            try:
                Dialect.get_or_raise(d.value)
            except Exception:
                pass
    modules = {}
    for f in sorted(test_files):
        try:
            modules[f] = importlib.import_module(f[:-3].replace("/", "."))
        except Exception:
            pass
    return modules


class Fingerprinter:
    def __init__(self, roots=()):
        self.roots = [r.rstrip(os.sep) for r in roots]
        self.memo = {}
        self.busy = set()
        self.keep = []

    def __call__(self, o, depth=0):
        t = type(o)
        if o is None or t is bool or t is int or t is float or t is complex:
            return repr(o)
        if t is str:
            for r in self.roots:
                if r in o:
                    o = o.replace(r, "<ROOT>")
            return "s" + o if len(o) < 64 else "S" + hashlib.blake2b(o.encode("utf-8", "surrogatepass"), digest_size=10).hexdigest()
        if t is bytes:
            return "b" + hashlib.blake2b(o, digest_size=10).hexdigest()
        i = id(o)
        r = self.memo.get(i)
        if r is not None:
            return r
        if i in self.busy:
            return "<cycle>"
        self.busy.add(i)
        try:
            r = self._fp(o, t, depth)
        finally:
            self.busy.discard(i)
        self.memo[i] = r
        self.keep.append(o)  # ids stay unique while the memo lives
        return r

    @staticmethod
    def _h(*parts):
        h = hashlib.blake2b(digest_size=10)
        for p in parts:
            h.update(p.encode("utf-8", "surrogatepass") if isinstance(p, str) else p)
            h.update(b"\x00")
        return h.hexdigest()

    def _fp(self, o, t, depth):
        fp = self.__call__
        d = depth + 1
        if isinstance(o, type):
            return f"K{o.__module__}.{o.__qualname__}"
        if isinstance(o, enum.Enum):
            return f"E{t.__module__}.{t.__qualname__}.{o.name}"
        if t is tuple or t is list:
            parts = [fp(x, d) for x in o]
            if parts and all(isinstance(x, type) for x in o):
                parts.sort()  # e.g. tuple({classes}): order is by id, and isinstance ignores it
            return self._h(t.__name__, *parts)
        if isinstance(o, dict):
            items = sorted(fp(k, d) + "=" + fp(v, d) for k, v in o.items())
            return self._h("D", t.__module__, t.__qualname__, *items)
        if isinstance(o, (set, frozenset)):
            return self._h("SET", t.__qualname__, *sorted(fp(x, d) for x in o))
        if t is types.FunctionType:
            cells = []
            for c in o.__closure__ or ():
                try:
                    cells.append(fp(c.cell_contents, d))
                except ValueError:
                    cells.append("<empty>")
            return self._h("F", o.__module__ or "", o.__qualname__, fp(o.__code__, d),
                           fp(o.__defaults__, d), fp(o.__kwdefaults__, d), *cells)
        if t is types.CodeType:
            return self._h("C", o.co_code, o.co_qualname, repr(o.co_names), repr(o.co_varnames),
                           repr(o.co_freevars), repr(o.co_cellvars), str(o.co_flags),
                           str(o.co_argcount), str(o.co_kwonlyargcount), o.co_exceptiontable,
                           *(fp(c, d) for c in o.co_consts))
        if t is types.ModuleType:
            return "M" + o.__name__
        if isinstance(o, (staticmethod, classmethod)):
            return self._h(t.__name__, fp(o.__func__, d))
        if isinstance(o, property):
            return self._h("P", fp(o.fget, d), fp(o.fset, d), fp(o.fdel, d))
        if isinstance(o, functools.partial):
            return self._h("PA", fp(o.func, d), fp(o.args, d), fp(o.keywords, d))
        if t is types.MethodType:
            return self._h("BM", fp(o.__func__, d), fp(o.__self__, d))
        if isinstance(o, _PATTERN):
            return self._h("RE", o.pattern if isinstance(o.pattern, str) else repr(o.pattern), str(o.flags))
        if isinstance(o, _OPAQUE):
            return "B" + getattr(o, "__qualname__", t.__qualname__) + "@" + str(getattr(o, "__module__", ""))
        if isinstance(o, functools._lru_cache_wrapper):
            return self._h("LRU", fp(o.__wrapped__, d))
        if depth > MAX_DEPTH:
            return f"O{t.__module__}.{t.__qualname__}"
        parts = [f"O{t.__module__}.{t.__qualname__}"]
        state = getattr(o, "__dict__", None)
        if isinstance(state, dict):
            parts.append(fp(state, d))
        for k in t.__mro__:
            for s in getattr(k, "__slots__", ()) if isinstance(getattr(k, "__slots__", ()), (tuple, list)) else ():
                if s in ("__dict__", "__weakref__"):
                    continue
                try:
                    parts.append(s + ":" + fp(object.__getattribute__(o, s), d))
                except AttributeError:
                    parts.append(s + ":<unset>")
                except Exception:
                    parts.append(s + ":<err>")
        if len(parts) == 1:
            r = repr(o)
            parts.append(t.__qualname__ if " at 0x" in r else r)
        return self._h(*parts)


def fingerprints(root):
    """{key: digest} over every loaded module under root/sqlglot and root/tests."""
    roots = sorted({os.path.abspath(root) + os.sep, os.path.realpath(root) + os.sep}, key=len, reverse=True)
    fp = Fingerprinter(roots)
    out = {}

    def walk_class(rel, cls, seen):
        if cls in seen:
            return
        seen.add(cls)
        for name, v in list(vars(cls).items()):
            if name in SKIP_CLASS:
                continue
            if isinstance(v, type) and v.__module__ == cls.__module__ and v.__qualname__.startswith(cls.__qualname__ + "."):
                walk_class(rel, v, seen)
            out[f"{rel}:{cls.__qualname__}.{name}"] = fp(v)

    for name, mod in sorted(sys.modules.items()):
        if not (name == "sqlglot" or name.startswith(("sqlglot.", "tests"))):
            continue
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        f = os.path.abspath(f)
        root = next((r for r in roots if f.startswith(r)), None)
        if root is None:
            continue  # compared by path relative to the tree the process loaded from
        rel = f[len(root):].replace(os.sep, "/")
        seen = set()
        for gname, v in list(vars(mod).items()):
            if gname in SKIP_MODULE:
                continue
            if isinstance(v, type) and v.__module__ == name:
                walk_class(rel, v, seen)
            out[f"{rel}::{gname}"] = fp(v)
    return out


def diff(a, b):
    return {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
