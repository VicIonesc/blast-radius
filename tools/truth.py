"""Ground truth for synthetic patches: apply each one to a scratch copy of sqlglot,
run the full suite, and record which tests fail.

Usage: python tools/truth.py <sqlglot_dir> <patch_dir> [workers]
Writes <patch_dir>/<id>.fail.json = sorted list of failing pytest node ids.
Skips patches that already have a .fail.json.
"""

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tools.build_map import junit_runtimes  # noqa: E402  (reuse node id logic)


def failing(junit_path):
    out = set()
    root = ET.parse(junit_path).getroot()
    for case in root.iter("testcase"):
        bad = case.find("failure") is not None or case.find("error") is not None
        if not bad:
            continue
        cls, name = case.get("classname", ""), case.get("name", "")
        if not cls:  # collection error: name is the module path, e.g. tests.test_parser
            out.add("COLLECT::" + name.replace(".", "/") + ".py")
            continue
        parts = cls.split(".")
        for i in range(len(parts), 0, -1):
            if parts[i - 1].startswith("test_"):
                out.add("::".join(["/".join(parts[:i]) + ".py", *parts[i:], name]))
                break
    return sorted(out)


def main(sqlglot_dir, patch_dir, workers="auto"):
    sqlglot_dir, patch_dir = os.path.abspath(sqlglot_dir), os.path.abspath(patch_dir)
    scratch = os.path.join(ROOT, "build", "truth_sqlglot")
    if not os.path.exists(scratch):
        shutil.copytree(sqlglot_dir, scratch, ignore=shutil.ignore_patterns(".git"))
    py = os.path.join(ROOT, ".venv", "bin", "python")
    env = dict(os.environ, PATH=os.path.dirname(py) + ":" + os.environ["PATH"])
    env.pop("SKIP_INTEGRATION", None)
    for fn in sorted(os.listdir(patch_dir)):
        if not fn.endswith(".patch"):
            continue
        cid = fn[:-6]
        dest = os.path.join(patch_dir, cid + ".fail.json")
        if os.path.exists(dest):
            continue
        patch = os.path.join(patch_dir, fn)
        r = subprocess.run(["patch", "-p1", "-s", "-i", patch], cwd=scratch, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"{cid}: patch failed: {r.stdout}{r.stderr}", file=sys.stderr)
            continue
        junit = os.path.join(ROOT, "build", "truth_junit.xml")
        try:
            subprocess.run(
                [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", str(workers),
                 "--timeout=120", "--ignore=tests/test_sqlglotc.py", f"--junitxml={junit}", "tests"],
                cwd=scratch, env=env, capture_output=True, text=True, timeout=900,
            )
            fails = failing(junit)
            # Deflake: rerun the individual failures once; keep only those that fail again.
            singles = [f for f in fails if not f.startswith("COLLECT::")]
            if singles and len(singles) <= 50:
                subprocess.run(
                    [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--timeout=120",
                     f"--junitxml={junit}", *singles],
                    cwd=scratch, env=env, capture_output=True, text=True, timeout=900,
                )
                again = set(failing(junit))
                flaky = [f for f in singles if f not in again]
                if flaky:
                    print(f"{cid}: flaky, dropped {flaky}", file=sys.stderr)
                fails = sorted(set(fails) - set(flaky))
        except subprocess.TimeoutExpired:
            fails = ["TIMEOUT"]
        finally:
            subprocess.run(["patch", "-p1", "-R", "-s", "-i", patch], cwd=scratch, check=True)
        json.dump(fails, open(dest, "w"), indent=0)
        print(f"{cid}: {len(fails)} failing", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
