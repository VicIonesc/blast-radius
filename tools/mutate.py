"""Generate synthetic single-commit patches against the pinned sqlglot.

Usage: python tools/mutate.py <sqlglot_dir> <out_dir> [count] [seed]
Writes <out_dir>/<id>.patch (git-style unified diffs, like /data/commits).
"""

import ast
import difflib
import os
import random
import re
import sys


def unified(path, old, new):
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        f"a/{path}",
        f"b/{path}",
        n=3,
    )
    body = "".join(diff)
    return f"diff --git a/{path} b/{path}\n" + body if body else ""


def py_files(root, sub):
    out = []
    for dp, _, fns in os.walk(os.path.join(root, sub)):
        for fn in fns:
            if fn.endswith(".py"):
                out.append(os.path.relpath(os.path.join(dp, fn), root))
    return sorted(out)


CMP_SWAP = [(" == ", " != "), (" != ", " == "), (" < ", " <= "), (" > ", " >= "),
            (" <= ", " < "), (" >= ", " > "), (" not in ", " in "), (" is not ", " is "),
            (" and ", " or "), (" or ", " and ")]


def func_body_lines(tree):
    """Single-line simple statements inside function bodies."""
    out = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for st in ast.walk(fn):
                if isinstance(st, ast.stmt) and st is not fn and st.lineno == st.end_lineno:
                    out.append(st)
    return out


def class_body_lines(tree):
    """Lines inside class bodies but outside methods (attributes, dict entries)."""
    out = []
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            for st in cls.body:
                if isinstance(st, (ast.Assign, ast.AnnAssign)):
                    out.append(st)
    return out


def mutate_once(root, rng, kind):
    if kind == "fixture":
        fx = []
        for dp, _, fns in os.walk(os.path.join(root, "tests/fixtures")):
            fx += [os.path.relpath(os.path.join(dp, f), root) for f in fns if f.endswith(".sql")]
        path = rng.choice(sorted(fx))
        old = open(os.path.join(root, path), encoding="utf-8").read()
        lines = old.splitlines(keepends=True)
        cand = [i for i, l in enumerate(lines) if l.strip() and not l.startswith("--") and len(l) > 10]
        if not cand:
            return None
        i = rng.choice(cand)
        sub = rng.choice([("SELECT", "SELECT DISTINCT"), (" a", " b"), ("1", "2"), (",", " ,")])
        if sub[0] not in lines[i]:
            return None
        lines[i] = lines[i].replace(sub[0], sub[1], 1)
        return path, old, "".join(lines)

    pool = py_files(root, "sqlglot")
    if kind == "test-edit":
        pool = [p for p in py_files(root, "tests") if os.path.basename(p).startswith("test_")]
    path = rng.choice(pool)
    old = open(os.path.join(root, path), encoding="utf-8").read()
    try:
        tree = ast.parse(old)
    except SyntaxError:
        return None
    lines = old.splitlines(keepends=True)

    if kind in ("cmp", "delete", "string", "number", "return", "test-edit"):
        sts = func_body_lines(tree)
        rng.shuffle(sts)
        for st in sts:
            i = st.lineno - 1
            l = lines[i]
            if kind == "cmp":
                opts = [s for s in CMP_SWAP if s[0] in l]
                if not opts:
                    continue
                a, b = rng.choice(opts)
                lines[i] = l.replace(a, b, 1)
            elif kind == "delete":
                if isinstance(st, (ast.Return, ast.Raise, ast.Pass)) or l.rstrip().endswith((":", "(", "[", "{", ",")):
                    continue
                indent = l[: len(l) - len(l.lstrip())]
                lines[i] = indent + "pass\n"
            elif kind in ("string", "test-edit"):
                m = re.search(r'"([A-Za-z_][A-Za-z_ ]{2,})"', l)
                if not m:
                    continue
                s = m.group(1)
                lines[i] = l[: m.start(1)] + s[:-1] + ("X" if s[-1] != "X" else "Y") + l[m.end(1):]
            elif kind == "number":
                m = re.search(r"(?<![\w.])(\d+)(?![\w.])", l)
                if not m or l.lstrip().startswith("#"):
                    continue
                lines[i] = l[: m.start()] + str(int(m.group(1)) + 1) + l[m.end():]
            elif kind == "return":
                if not isinstance(st, ast.Return) or st.value is None:
                    continue
                indent = l[: len(l) - len(l.lstrip())]
                lines[i] = indent + "return None\n"
            new = "".join(lines)
            if new != old:
                try:
                    compile(new, path, "exec")
                except SyntaxError:
                    lines[i] = l
                    continue
                return path, old, new
        return None

    if kind in ("class-attr", "dict-entry"):
        sts = class_body_lines(tree)
        rng.shuffle(sts)
        for st in sts:
            if kind == "class-attr" and st.lineno == st.end_lineno:
                i = st.lineno - 1
                l = lines[i]
                for a, b in (("True", "False"), ("False", "True"), ("None", '"x"')):
                    if f"= {a}" in l:
                        lines[i] = l.replace(f"= {a}", f"= {b}", 1)
                        return path, old, "".join(lines)
            if kind == "dict-entry" and isinstance(getattr(st, "value", None), ast.Dict):
                d = st.value
                entries = [(k, v) for k, v in zip(d.keys, d.values) if k is not None and k.lineno == v.end_lineno]
                if not entries:
                    continue
                k, v = rng.choice(entries)
                i = k.lineno - 1
                if lines[i].strip().endswith(","):
                    del lines[i]
                    new = "".join(lines)
                    try:
                        compile(new, path, "exec")
                    except SyntaxError:
                        return None
                    return path, old, new
        return None

    if kind == "comment":
        sts = func_body_lines(tree)
        if not sts:
            return None
        st = rng.choice(sts)
        i = st.lineno - 1
        l = lines[i]
        indent = l[: len(l) - len(l.lstrip())]
        lines.insert(i, indent + "# NOTE: keep this in sync with the generator\n")
        return path, old, "".join(lines)
    return None


KINDS = ["cmp", "delete", "string", "number", "return", "class-attr", "dict-entry",
         "comment", "fixture", "test-edit"]


def main(root, out, count=40, seed=1):
    rng = random.Random(int(seed))
    os.makedirs(out, exist_ok=True)
    made = 0
    tries = 0
    while made < int(count) and tries < int(count) * 50:
        tries += 1
        kind = KINDS[made % len(KINDS)]
        r = mutate_once(root, rng, kind)
        if not r:
            continue
        path, old, new = r
        text = unified(path, old, new)
        if not text:
            continue
        cid = f"m{seed}_{made:03d}_{kind}"
        open(os.path.join(out, cid + ".patch"), "w").write(text)
        made += 1
    print(f"wrote {made} patches to {out}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
