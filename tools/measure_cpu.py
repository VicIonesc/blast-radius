"""Measure each test's in-process CPU cost the way blast.verify runs it
(one process, unittest, process pools replaced by inline execution).

Usage: python tools/measure_cpu.py <sqlglot_dir> map/covmap.json map/cpu.json
Writes {"test": {node: cpu_s}, "class_setup": {"file::Class": cpu_s}} (this machine's CPU).
"""

import importlib
import json
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from blast.verify import InlineExecutor  # noqa: E402


class Timed(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.times = {}
        self._t = None

    def startTest(self, test):
        super().startTest(test)
        self._t = time.process_time()

    def stopTest(self, test):
        self.times[test._br_node] = time.process_time() - self._t
        super().stopTest(test)


def main(sqlglot_dir, covmap, out):
    sqlglot_dir = os.path.abspath(sqlglot_dir)
    sys.path.insert(0, sqlglot_dir)
    os.chdir(sqlglot_dir)
    os.environ["SKIP_INTEGRATION"] = "0"
    tests = json.load(open(covmap))["tests"]
    by_class = {}
    for t in tests:
        by_class.setdefault(t.rsplit("::", 1)[0], []).append(t)
    test_cpu, setup_cpu = {}, {}
    for cls_id, nodes in sorted(by_class.items()):
        f, cls = cls_id.split("::")
        try:
            mod = importlib.import_module(f[:-3].replace("/", "."))
        except Exception as e:
            print(f"skip {f}: {e!r}", file=sys.stderr)
            continue
        if getattr(mod, "ProcessPoolExecutor", None) is not None:
            mod.ProcessPoolExecutor = InlineExecutor
        suite = unittest.TestSuite()
        for n in nodes:
            case = getattr(mod, cls)(n.rsplit("::", 1)[1])
            case._br_node = n
            suite.addTest(case)
        res = Timed()
        t0 = time.process_time()
        suite.run(res)
        suite._tearDownPreviousClass(None, res)  # run tearDownClass inside the timing
        total = time.process_time() - t0
        test_cpu.update({k: round(v, 4) for k, v in res.times.items()})
        setup_cpu[cls_id] = round(max(0.0, total - sum(res.times.values())), 4)
        bad = len(res.failures) + len(res.errors)
        print(f"{cls_id}: {total:.2f}s ({setup_cpu[cls_id]:.2f} setup){' FAIL ' + str(bad) if bad else ''}",
              file=sys.stderr)
    json.dump({"test": test_cpu, "class_setup": setup_cpu}, open(out, "w"), separators=(",", ":"))


if __name__ == "__main__":
    main(*[os.path.abspath(a) if i < 3 else a for i, a in enumerate(sys.argv[1:4])])
