"""Lines that run at import time, measured the way the verifier's parent loads things:
every sqlglot module, every dialect instantiated, every test module imported.

Usage: python tools/import_cov.py <sqlglot_dir> map/covmap.json
Rewrites covmap["import_ran"] = {file: [lines]}.
"""

import json
import os
import sys

import coverage


def main(sqlglot_dir, covmap):
    sqlglot_dir = os.path.abspath(sqlglot_dir)
    os.chdir(sqlglot_dir)
    sys.path.insert(0, sqlglot_dir)
    os.environ["SKIP_INTEGRATION"] = "0"
    cov = coverage.Coverage(source=["sqlglot", "tests"], data_file=None, omit=["tests/fixtures/*"])
    cov.start()
    import importlib
    import pkgutil

    import sqlglot
    from sqlglot.dialects.dialect import Dialect

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name != "sqlglot.__main__":
            try:
                importlib.import_module(info.name)
            except Exception:
                pass
    for d in list(Dialect.classes):
        if d:
            try:
                Dialect.get_or_raise(d)
            except Exception:
                pass
    # Snapshot: what sqlglot itself runs while loading (before any test module).
    cov.stop()
    own = {}
    d0 = cov.get_data()
    for f in d0.measured_files():
        rel = os.path.relpath(f, sqlglot_dir).replace(os.sep, "/")
        if rel.startswith("sqlglot/"):
            own[rel] = sorted(d0.lines(f) or [])
    cov.start()
    for root, _, files in os.walk("tests"):
        for fn in files:
            if fn.endswith(".py") and "fixtures" not in root:
                name = os.path.join(root, fn)[:-3].replace(os.sep, ".")
                try:
                    importlib.import_module(name)
                except Exception:
                    pass
    cov.stop()
    data = cov.get_data()
    ran = {}
    for f in data.measured_files():
        rel = os.path.relpath(f, sqlglot_dir).replace(os.sep, "/")
        lines = sorted(data.lines(f) or [])
        if lines:
            ran[rel] = lines
    c = json.load(open(covmap))
    c["import_ran"] = ran
    c["sqlglot_import_ran"] = own
    json.dump(c, open(covmap, "w"), separators=(",", ":"))
    print(f"import-time lines: {sum(len(v) for v in ran.values())} in {len(ran)} files", file=sys.stderr)


if __name__ == "__main__":
    main(os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2]))
