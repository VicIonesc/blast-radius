"""Class attribute reads that happen while sqlglot (and the test modules) load.

Each sqlglot module gets recording descriptors on its classes right after it finishes
executing, so reads by every later module (subclass creation, metaclasses reading base
settings, module-level parsing) are recorded against the exact class read through.

Usage: python tools/import_reads.py <sqlglot_dir> <out.json>
Writes ["sqlglot/path.py:Qual.ATTR", ...].
"""

import importlib
import importlib.abc
import inspect
import json
import os
import pkgutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tools import trace_attrs_plugin as T  # noqa: E402  (Rec, _is_setting, _class_key)


def _install_on_module(mod):
    from enum import Enum

    todo = [v for v in vars(mod).values() if inspect.isclass(v) and v.__module__ == mod.__name__]
    seen = set()
    while todo:
        cls = todo.pop()
        if cls in seen:
            continue
        seen.add(cls)
        todo.extend(v for v in vars(cls).values() if inspect.isclass(v) and v.__module__ == mod.__name__)
        if issubclass(cls, Enum) or isinstance(cls, type) and issubclass(cls, type):
            continue
        for name, value in list(vars(cls).items()):
            if T._is_setting(name, value):
                try:
                    setattr(cls, name, T.Rec(name, value))
                except (AttributeError, TypeError):
                    pass


class Hook(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.")):
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            spec = finder.find_spec(name, path, target) if hasattr(finder, "find_spec") else None
            if spec is not None and spec.loader is not None:
                orig = spec.loader.exec_module

                def exec_module(module, _orig=orig):
                    _orig(module)
                    _install_on_module(module)

                spec.loader.exec_module = exec_module
                return spec
        return None


def main(sqlglot_dir, out):
    sqlglot_dir = os.path.abspath(sqlglot_dir)
    os.chdir(sqlglot_dir)
    sys.path.insert(0, sqlglot_dir)
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.meta_path.insert(0, Hook())
    T._current[0] = "IMPORT"
    import sqlglot
    from sqlglot.dialects.dialect import Dialect

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name != "sqlglot.__main__":
            try:
                importlib.import_module(info.name)
            except Exception as e:
                print(f"skip {info.name}: {e!r}", file=sys.stderr)
    for d in list(Dialect.classes):
        if d:
            Dialect.get_or_raise(d)
    for root, _, files in os.walk("tests"):
        for fn in files:
            if fn.endswith(".py") and "fixtures" not in root:
                try:
                    importlib.import_module(os.path.join(root, fn)[:-3].replace(os.sep, "."))
                except Exception:
                    pass
    keys = set()
    for cls, name in T._seen:
        k = T._class_key(cls)
        if k:
            keys.add(f"{k}.{name}")
    json.dump(sorted(keys), open(out, "w"))
    print(f"{len(keys)} class attributes read at import", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1], os.path.abspath(sys.argv[2]))
