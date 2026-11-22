"""Build the line -> tests coverage map from a pytest-cov run with --cov-context=test.

Usage: python tools/build_map.py build/.coverage build/junit.xml map/covmap.json

Output (JSON, compact):
  tests:    list of pytest node ids (index = local test id)
  runtime:  recorded seconds per test (from junit.xml, summed over subtests)
  sets:     list of unique sorted test-id lists
  files:    {path: {line: set_index}}  for lines run inside at least one test
  import:   {path: [lines]}             lines only run at import/collection time
"""

import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

from coverage import CoverageData


def junit_runtimes(path):
    times = defaultdict(float)
    for case in ET.parse(path).getroot().iter("testcase"):
        cls = case.get("classname", "")  # e.g. tests.dialects.test_duckdb.TestDuckDB
        name = case.get("name", "")
        parts = cls.split(".")
        # classname is module path + class; turn it into a pytest node id.
        # The module is the longest prefix whose last part looks like test_*.
        for i in range(len(parts), 0, -1):
            if parts[i - 1].startswith("test_"):
                node = "::".join(["/".join(parts[:i]) + ".py", *parts[i:], name])
                break
        else:
            node = f"{cls}::{name}"
        times[node] += float(case.get("time", 0) or 0)
    return times


def main(cov_path, junit_path, out_path):
    data = CoverageData(basename=cov_path)
    data.read()

    rt = junit_runtimes(junit_path)
    # Every test from the timing run, even ones that left no coverage (e.g. subprocess tests).
    tests = sorted(
        {c[: -len("|run")] for c in data.measured_contexts() if c.endswith("|run")} | set(rt)
    )
    tid = {t: i for i, t in enumerate(tests)}
    runtime = [round(rt.get(t, 0.0), 4) for t in tests]
    missing = [t for t in tests if t not in rt]
    if missing:
        print(f"warning: {len(missing)} tests without junit time, e.g. {missing[:3]}", file=sys.stderr)

    set_index = {}
    sets = []
    files = {}
    import_only = {}
    for f in sorted(data.measured_files()):
        by_line = data.contexts_by_lineno(f)
        lines = {}
        imp = []
        for line, ctxs in sorted(by_line.items()):
            ids = tuple(sorted(tid[c[:-4]] for c in ctxs if c.endswith("|run") and c[:-4] in tid))
            if not ids:
                imp.append(line)
                continue
            k = set_index.get(ids)
            if k is None:
                k = set_index[ids] = len(sets)
                sets.append(list(ids))
            lines[line] = k
        files[f] = lines
        if imp:
            import_only[f] = imp

    out = {"tests": tests, "runtime": runtime, "sets": sets, "files": files, "import": import_only}
    with open(out_path, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"{len(tests)} tests, {len(files)} files, {len(sets)} unique sets", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:4])
