"""Score the selector on synthetic patches with known failing tests.

Usage: python tools/evaluate.py build/mutants [-v]
Uses our own runtimes; the real score uses tests.json runtimes.
"""

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from blast.select import Maps, Selector, schedule  # noqa: E402

MISS = 300.0


def main(patch_dir, *flags):
    verbose = "-v" in flags
    maps = Maps()
    sel = Selector(maps)
    tid = {t: i for i, t in enumerate(maps.tests)}
    runtime = {i: int(round(r * 1000)) for i, r in enumerate(maps.runtime)}
    total = misses = n = 0
    t0 = time.process_time()
    rows = []
    for fn in sorted(os.listdir(patch_dir)):
        if not fn.endswith(".patch"):
            continue
        cid = fn[:-6]
        fpath = os.path.join(patch_dir, cid + ".fail.json")
        if not os.path.exists(fpath):
            continue
        fails = json.load(open(fpath))
        want = set()
        for f in fails:
            if f.startswith("COLLECT::"):
                want |= maps.test_files.get(f[len("COLLECT::"):], set())
            elif f == "TIMEOUT":
                want |= maps.all
            elif f in tid:
                want.add(tid[f])
        got, reasons = sel.select(open(os.path.join(patch_dir, fn)).read())
        missed = want - got
        bins, cost = schedule(sorted(got), runtime)
        cost = cost / 1000 + (MISS if missed else 0)
        total += cost
        misses += bool(missed)
        n += 1
        rows.append((cid, len(want), len(got), cost, missed, reasons))
    for cid, nw, ng, cost, missed, reasons in rows:
        flag = "MISS" if missed else "    "
        if verbose or missed:
            print(f"{flag} {cid:28s} fail={nw:4d} sel={ng:4d} cost={cost:6.1f}  {'; '.join(reasons)}")
            for i in sorted(missed)[:5]:
                print(f"       missed {maps.tests[i]}")
    oracle = 0.0
    for cid, nw, *_ in rows:
        pass
    print(f"\n{n} patches, {misses} missed, total {total:.1f}s "
          f"(avg {total / max(n, 1):.2f}s/commit), selector cpu {time.process_time() - t0:.2f}s")


if __name__ == "__main__":
    main(*sys.argv[1:])
