"""Static metadata the selector needs on top of the coverage map.

Usage (from the sqlglot checkout, with it importable):
  python tools/build_meta.py <sqlglot_dir> map/covmap.json build/files_by_test.json map/meta.json

Writes:
  dialect_files:   {dialect: [source files that belong to it]}
  dialect_family:  {dialect: [itself + every dialect that subclasses it]}
  dialect_tests:   {dialect: [test ids that cover its files or name it in their source]}
  test_files:      {tests/x.py: [test ids defined in it]}
  test_spans:      {tests/x.py: [[start, end, test id], ...]}  body span of each test function
  importers:       {tests/x.py or tests/x: [test files that import it]}
  fixture_tests:   {tests/fixtures/...: [test ids]}  traced reads + textual references
"""

import ast
import json
import os
import re
import sys
from collections import defaultdict


def traced_class_keys():
    """Every sqlglot class that is an Expr/Parser/Generator/Tokenizer (what the class
    trace can see), as 'sqlglot/path.py:QualName'."""
    import importlib
    import inspect
    import pkgutil

    import sqlglot
    from sqlglot.expressions.core import Expr
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    bases = (Expr, Parser, Generator, Tokenizer)
    keys = set()
    seen = set()

    def visit(cls):
        if cls in seen:
            return
        seen.add(cls)
        if issubclass(cls, bases):
            mod = sys.modules.get(cls.__module__)
            f = getattr(mod, "__file__", "") or ""
            i = f.rfind("/sqlglot/")
            if i >= 0:
                keys.add(f"{f[i + 1:]}:{cls.__qualname__}")
        for _, v in vars(cls).items():
            if inspect.isclass(v) and v.__module__ == cls.__module__:
                visit(v)

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            mod = importlib.import_module(info.name)
        except Exception:
            continue
        for _, v in vars(mod).items():
            if inspect.isclass(v) and v.__module__ == mod.__name__:
                visit(v)
    return sorted(keys)


def main(sqlglot_dir, covmap_path, files_by_test_path, out_path, classes_by_test_path=None):
    sys.path.insert(0, sqlglot_dir)
    os.chdir(sqlglot_dir)
    m = json.load(open(covmap_path))
    tests = m["tests"]
    tid = {t: i for i, t in enumerate(tests)}
    sets = m["sets"]

    # ---- dialects ---------------------------------------------------------
    from sqlglot.dialects.dialect import Dialect, Dialects

    names = [d.value for d in Dialects if d.value]
    classes = {}
    for n in names:
        try:
            classes[n] = Dialect.get_or_raise(n).__class__
        except Exception as e:  # pragma: no cover
            print("skip dialect", n, e, file=sys.stderr)
    by_class = {c: n for n, c in classes.items()}

    family = defaultdict(set)
    for n, c in classes.items():
        for base in c.__mro__:
            if base in by_class:
                family[by_class[base]].add(n)

    dialect_files = {}
    for n in classes:
        fs = [
            f"sqlglot/{sub}/{n}.py"
            for sub in ("dialects", "parsers", "generators", "typing")
            if os.path.exists(f"sqlglot/{sub}/{n}.py")
        ]
        dialect_files[n] = fs

    def cover_of_file(f):
        out = set()
        for k in set(m["files"].get(f, {}).values()):
            out.update(sets[k])
        return out

    # ---- test sources -----------------------------------------------------
    test_files = defaultdict(list)
    for i, t in enumerate(tests):
        test_files[t.split("::")[0]].append(i)

    test_spans = {}
    mentions = defaultdict(set)  # dialect -> test ids
    name_set = set(names)
    imports = defaultdict(set)
    all_test_py = []
    for root, _, files in os.walk("tests"):
        for fn in files:
            if fn.endswith(".py"):
                all_test_py.append(os.path.join(root, fn))

    for path in all_test_py:
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
        # imports of other test modules
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("tests"):
                mod = node.module
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("tests"):
                        imports[a.name.replace(".", "/")].add(path)
            if mod:
                imports[mod.replace(".", "/")].add(path)
                for a in node.names:  # from tests.dialects import test_dialect
                    imports[f"{mod.replace('.', '/')}/{a.name}"].add(path)

        spans = []
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            cls_dialects = {
                c.value
                for stmt in cls.body
                if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                for c in ast.walk(stmt)
                if isinstance(c, ast.Constant) and c.value in name_set
            }
            for fn in cls.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                node_id = f"{path}::{cls.name}::{fn.name}"
                if node_id not in tid:
                    continue
                i = tid[node_id]
                start = min([fn.lineno] + [d.lineno for d in fn.decorator_list])
                spans.append([start, fn.end_lineno, i])
                ds = cls_dialects | {
                    c.value
                    for c in ast.walk(fn)
                    if isinstance(c, ast.Constant) and c.value in name_set
                }
                for d in ds:
                    mentions[d].add(i)
        test_spans[path] = sorted(spans)

    dialect_tests = {}
    for n in classes:
        s = set(mentions[n])
        for f in dialect_files[n]:
            s |= cover_of_file(f)
        dialect_tests[n] = sorted(s)

    # ---- fixtures ---------------------------------------------------------
    fixture_tests = defaultdict(set)
    traced = json.load(open(files_by_test_path))
    for t, fs in traced.items():
        if t in tid:
            for f in fs:
                fixture_tests[f].add(tid[t])
    fixtures = []
    for root, _, files in os.walk("tests/fixtures"):
        for fn in files:
            fixtures.append(os.path.join(root, fn))
    # Textual references: a string constant naming the fixture or a directory above it.
    # References outside a test function (setUpClass, module level) count for the whole class/file.
    for path in all_test_py:
        tree = ast.parse(open(path, encoding="utf-8").read())
        spans = test_spans.get(path, [])
        # Reads inside helper functions run in a test's context, so the trace already has
        # them. Only constants in tests, setUp*/class bodies or module level need text matching.
        helper_spans = [
            (fn.lineno, fn.end_lineno)
            for fn in ast.walk(tree)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not fn.name.startswith(("test", "setUp"))
        ]
        for c in ast.walk(tree):
            if isinstance(c, ast.Constant) and any(a <= c.lineno <= b for a, b in helper_spans):
                continue
            if not (isinstance(c, ast.Constant) and isinstance(c.value, str)):
                continue
            s = c.value.strip("/")
            if not s or len(s) < 4 or "\n" in s:
                continue
            hits = []
            for f in fixtures:
                rel = "/" + f[len("tests/fixtures/") :]
                if rel.endswith("/" + s) or ("/" + s + "/") in rel:
                    hits.append(f)
            if not hits:
                continue
            inside = [i for a, b, i in spans if a <= c.lineno <= b]
            ids = inside or test_files.get(path, [])
            for f in hits:
                fixture_tests[f].update(ids)

    # ---- class instantiation map ------------------------------------------
    class_tests = defaultdict(set)
    if classes_by_test_path and os.path.exists(classes_by_test_path):
        for t, names in json.load(open(classes_by_test_path)).items():
            if t in tid:
                for n in names:
                    class_tests[n].add(tid[t])
    traced = traced_class_keys()

    out = {
        "traced_classes": traced,
        "class_tests": {k: sorted(v) for k, v in class_tests.items()},
        "dialect_files": dialect_files,
        "dialect_family": {k: sorted(v) for k, v in family.items()},
        "dialect_tests": dialect_tests,
        "test_files": dict(test_files),
        "test_spans": test_spans,
        "importers": {k: sorted(v) for k, v in imports.items()},
        "fixture_tests": {k: sorted(v) for k, v in fixture_tests.items()},
    }
    json.dump(out, open(out_path, "w"), separators=(",", ":"))
    print(
        f"{len(classes)} dialects, {len(fixture_tests)} fixtures mapped, "
        f"{sum(len(v) for v in test_spans.values())} test spans",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main(*[os.path.abspath(a) for a in sys.argv[1:6]])
