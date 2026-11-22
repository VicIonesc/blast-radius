"""Stress-test the giant-test proxy rule on synthetic bugs placed on lines the giant covers.

Usage: python tools/proxy_check.py <sqlglot_dir> <out_dir> [count] [seed] [workers]
For each mutant, runs only the giant + its proxies (pytest, its own scratch copy) and
writes <out_dir>/<id>.patch and <id>.fail.json. Then reports, for every mutant where the
selector would leave the giant to its proxies, whether the giant failed while they passed.
"""

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from blast.select import GIANT_PROXIES, Maps, Selector  # noqa: E402
from tools.mutate import CMP_SWAP, unified  # noqa: E402
from tools.truth import failing  # noqa: E402

GIANT = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def mutants(sqlglot_dir, maps, count, seed):
    rng = random.Random(seed)
    g = maps.tests.index(GIANT)
    lines = [
        (f, ln) for f, ls in maps.files.items() if f.startswith("sqlglot/")
        for ln, k in ls.items() if g in maps.sets[k]
    ]
    rng.shuffle(lines)
    out = []
    for f, ln in lines:
        if len(out) >= count:
            break
        src = open(os.path.join(sqlglot_dir, f), encoding="utf-8").read()
        rows = src.splitlines(keepends=True)
        line = rows[ln - 1]
        body = line.strip()
        if not body or body.startswith(("#", "def ", "class ", "@", "elif", "else", "try", "except",
                                         "finally", "with ", "for ", "while ", "if ", "return\n")):
            continue
        ops = []
        for a, b in CMP_SWAP:
            if a in line:
                ops.append(("cmp", line.replace(a, b, 1)))
        m = re.search(r'"([A-Za-z_][A-Za-z_ ]{2,})"', line)
        if m:
            ops.append(("string", line[: m.end(1) - 1] + "X" + line[m.end(1):]))
        m = re.search(r"(?<![\w.])(\d+)(?![\w.])", line)
        if m:
            ops.append(("number", line[: m.start()] + str(int(m.group(1)) + 1) + line[m.end():]))
        indent = line[: len(line) - len(line.lstrip())]
        if body.startswith("return ") and not line.rstrip().endswith(("(", "[", "{", ",")):
            ops.append(("return", indent + "return None\n"))
        if not line.rstrip().endswith((":", "(", "[", "{", ",", "\\")) and not body.startswith(("return", "raise", "yield")):
            ops.append(("delete", indent + "pass\n"))
        if not ops:
            continue
        kind, new_line = rng.choice(ops)
        new_rows = rows[:]
        new_rows[ln - 1] = new_line
        new = "".join(new_rows)
        try:
            compile(new, f, "exec")
        except SyntaxError:
            continue
        out.append((f"p{seed}_{len(out):03d}_{kind}", unified(f, src, new)))
    return out


def run_one(args):
    cid, patch, sqlglot_dir, out_dir, nodes, slot = args
    scratch = os.path.join(tempfile.gettempdir(), f"br_proxy_{slot}")
    if not os.path.exists(scratch):
        shutil.copytree(sqlglot_dir, scratch, ignore=shutil.ignore_patterns(".git"))
    pfile = os.path.join(out_dir, cid + ".patch")
    open(pfile, "w").write(patch)
    subprocess.run(["patch", "-p1", "-s", "-i", pfile], cwd=scratch, check=True)
    junit = os.path.join(scratch, "junit.xml")
    py = os.path.join(ROOT, ".venv", "bin", "python")
    env = dict(os.environ, PATH=os.path.dirname(py) + ":" + os.environ["PATH"])
    env.pop("SKIP_INTEGRATION", None)
    try:
        subprocess.run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--timeout=300",
                        f"--junitxml={junit}", *nodes],
                       cwd=scratch, env=env, capture_output=True, timeout=900)
        fails = failing(junit)
    except subprocess.TimeoutExpired:
        fails = ["TIMEOUT"]
    finally:
        subprocess.run(["patch", "-p1", "-R", "-s", "-i", pfile], cwd=scratch, check=True)
    json.dump(fails, open(os.path.join(out_dir, cid + ".fail.json"), "w"))
    return cid, fails


def main(sqlglot_dir, out_dir, count=30, seed=7, workers=4):
    sqlglot_dir = os.path.abspath(sqlglot_dir)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    maps = Maps()
    prefixes = GIANT_PROXIES[GIANT]
    nodes = [t for t in maps.tests if t == GIANT or any(t.startswith(p) for p in prefixes)]
    todo = [(cid, p) for cid, p in mutants(sqlglot_dir, maps, int(count), int(seed))
            if not os.path.exists(os.path.join(out_dir, cid + ".fail.json"))]
    slots = int(workers)
    jobs = [(cid, p, sqlglot_dir, out_dir, nodes, i % slots) for i, (cid, p) in enumerate(todo)]
    # One scratch copy per slot; run slot-by-slot batches so copies never collide.
    with ThreadPoolExecutor(slots) as ex:
        for b in range(0, len(jobs), slots):
            for cid, fails in ex.map(run_one, jobs[b:b + slots]):
                print(f"{cid}: giant={'FAIL' if GIANT in fails else 'pass'} fails={len(fails)}",
                      file=sys.stderr)
    report(out_dir, maps)


def report(out_dir, maps):
    sel = Selector(maps)
    g = maps.tests.index(GIANT)
    proxies = maps.giant_proxies[g]
    stats = {"giant_in_cand": 0, "rule_applies": 0, "giant_fails": 0, "missed": 0, "saved": 0}
    for fn in sorted(os.listdir(out_dir)):
        if not fn.endswith(".fail.json"):
            continue
        cid = fn[:-10]
        fails = set(json.load(open(os.path.join(out_dir, fn))))
        ours, _, _ = sel.select(open(os.path.join(out_dir, cid + ".patch")).read())
        if g not in ours:
            continue
        stats["giant_in_cand"] += 1
        applies = all(not (g in ts) or (ts & proxies) for ts in sel.touch_sets)
        if not applies:
            continue
        stats["rule_applies"] += 1
        cand_proxies = {maps.tests[i] for i in proxies & ours}
        proxy_fail = bool(cand_proxies & fails)
        if GIANT in fails:
            stats["giant_fails"] += 1
            if not proxy_fail:
                stats["missed"] += 1
                print(f"MISS {cid}: giant failed, candidate proxies all passed", file=sys.stderr)
        elif not proxy_fail:
            stats["saved"] += 1
    print(json.dumps(stats))


if __name__ == "__main__":
    main(*sys.argv[1:])
