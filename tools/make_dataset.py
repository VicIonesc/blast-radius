"""Turn synthetic patches + ground truth into a grader-style data dir.

Usage: python tools/make_dataset.py <patch_dir> <tests.json> <out_dir>
Creates <out_dir>/commits/*.patch, <out_dir>/tests.json (copied) and
<out_dir>/answers.json, so `python kit/score.py <out_dir> <out_dir>/answers.json out.jsonl`
scores a full run exactly like the sample set.
"""

import json
import os
import shutil
import sys


def main(patch_dir, tests_json, out):
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    shutil.copy(tests_json, os.path.join(out, "tests.json"))
    tests = json.load(open(tests_json))["tests"]
    num = {t["id"]: t["number"] for t in tests}
    by_file = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t["number"])
    answers = {}
    for fn in sorted(os.listdir(patch_dir)):
        if not fn.endswith(".patch"):
            continue
        cid = fn[:-6]
        fpath = os.path.join(patch_dir, cid + ".fail.json")
        if not os.path.exists(fpath):
            continue
        fails = set()
        for f in json.load(open(fpath)):
            if f.startswith("COLLECT::"):
                fails.update(by_file.get(f[len("COLLECT::"):], []))
            elif f == "TIMEOUT":
                fails.update(num.values())
            elif f in num:
                fails.add(num[f])
        shutil.copy(os.path.join(patch_dir, fn), os.path.join(out, "commits", fn))
        answers[cid] = {"failing": sorted(fails), "kind": cid.split("_", 2)[-1]}
    json.dump({"commits": answers}, open(os.path.join(out, "answers.json"), "w"))
    print(f"{len(answers)} commits -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:4])
