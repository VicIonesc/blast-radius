"""Blast Radius selector: patch -> tests that can catch it -> split across workers.

Run:  python -m blast.select            (reads $DATA_DIR, default /data)
Prints one JSON line per commit on stdout; everything else goes to stderr.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import sys
import time
import tokenize
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.environ.get("DATA_DIR", "/data")
MAP_DIR = os.environ.get("MAP_DIR", os.path.join(ROOT, "map"))
SQLGLOT_DIR = os.environ.get("SQLGLOT_DIR", os.path.join(ROOT, "vendor", "sqlglot"))
MAX_WORKERS = 8
WORKER_COST = 2000  # ms


def log(*a):
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------- patch parsing


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list = field(default_factory=list)  # (old_start, old_len, new_start, new_len, lines)

    @property
    def path(self):
        return self.old_path or self.new_path

    def changed_old_lines(self):
        """Old line numbers that were removed or replaced."""
        out = set()
        for old_start, _, _, _, lines in self.hunks:
            n = old_start
            for ln in lines:
                if ln.startswith("-"):
                    out.add(n)
                    n += 1
                elif ln.startswith(" "):
                    n += 1
        return out

    def insert_points(self):
        """{P: indent} for pure insertions between old lines P and P+1 (no old line
        removed there). indent = indentation of the first non-blank inserted line."""
        out = {}
        for old_start, old_len, _, _, lines in self.hunks:
            n = old_start if old_len else old_start + 1
            prev_removed = False
            for ln in lines:
                if ln.startswith("-"):
                    prev_removed = True
                    n += 1
                elif ln.startswith(" "):
                    prev_removed = False
                    n += 1
                elif ln.startswith("+") and not prev_removed:
                    body = ln[1:]
                    if body.strip() and out.get(n - 1) is None:
                        out[n - 1] = len(body) - len(body.lstrip())
                    else:
                        out.setdefault(n - 1, None)
        return out

    def added_text(self):
        return "\n".join(ln[1:] for h in self.hunks for ln in h[4] if ln.startswith("+"))

    def removed_text(self):
        return "\n".join(ln[1:] for h in self.hunks for ln in h[4] if ln.startswith("-"))


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    files = []
    cur = None
    hunk = None
    old_left = new_left = 0  # lines still expected in the current hunk
    for line in text.splitlines():
        if old_left > 0 or new_left > 0:
            tag = line[:1] if line else " "
            body = line if line else " "
            if tag == "\\":  # "\ No newline at end of file"
                continue
            hunk[4].append(body)
            if tag in (" ", "-"):
                old_left -= 1
            if tag in (" ", "+"):
                new_left -= 1
            continue
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)", line)
            cur = FilePatch(m.group(1), m.group(2)) if m else FilePatch(None, None)
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if line.startswith("--- "):
            p = line[4:].split("\t")[0]
            cur.old_path = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
        elif line.startswith("+++ "):
            p = line[4:].split("\t")[0]
            cur.new_path = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif line.startswith("rename from "):
            cur.old_path = line[len("rename from ") :]
        elif line.startswith("rename to "):
            cur.new_path = line[len("rename to ") :]
        else:
            m = HUNK_RE.match(line)
            if m:
                a, b, c, d = m.groups()
                b = int(b) if b is not None else 1
                d = int(d) if d is not None else 1
                hunk = [int(a), b, int(c), d, []]
                cur.hunks.append(hunk)
                old_left, new_left = b, d
    return files


def apply_patch(old_text, fp: FilePatch):
    """Apply one file's hunks to old text. Returns new text or None if it doesn't apply."""
    old = old_text.splitlines(keepends=True)
    out = []
    i = 0
    for old_start, _, _, _, lines in fp.hunks:
        start = old_start - 1 if old_start else 0
        if start < i:
            return None
        out.extend(old[i:start])
        i = start
        for ln in lines:
            tag, body = ln[:1], ln[1:]
            if tag == " ":
                if i >= len(old) or old[i].rstrip("\r\n") != body:
                    return None
                out.append(old[i])
                i += 1
            elif tag == "-":
                i += 1
            elif tag == "+":
                out.append(body + "\n")
    out.extend(old[i:])
    return "".join(out)


# --------------------------------------------------------------------------- source helpers

_src_cache: dict = {}


def old_source(path):
    if path not in _src_cache:
        p = os.path.join(SQLGLOT_DIR, path)
        try:
            with open(p, encoding="utf-8") as fh:
                _src_cache[path] = fh.read()
        except (OSError, UnicodeDecodeError):  # missing or binary
            _src_cache[path] = None
    return _src_cache[path]


def code_tokens(text):
    """Token stream without comments / layout, or None if it doesn't tokenize."""
    try:
        toks = tokenize.generate_tokens(io.StringIO(text).readline)
        return [
            (t.type, t.string)
            for t in toks
            if t.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE)
        ]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None


class Scopes:
    """Statement/function/class spans of one old source file."""

    def __init__(self, text):
        self.tree = ast.parse(text)
        self.funcs = []  # (start, body_start, end, node)
        self.classes = []  # (start, end, node, qualname)
        self.stmts = []  # (start, end, node)
        self.func_qual = {}  # id(FunctionDef) -> co_qualname
        self._walk(self.tree, "")

    def outermost_func(self, line):
        """Outermost function (not nested in another function) containing line."""
        best = None
        for f in self.funcs:
            if f[0] <= line <= f[2] and "<locals>" not in self.func_qual[id(f[3])]:
                if best is None or f[0] < best[0]:
                    best = f
        return best

    def _walk(self, node, prefix):
        for child in ast.iter_child_nodes(node):
            qual = prefix
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                self.funcs.append((start, child.body[0].lineno, child.end_lineno, child))
                self.func_qual[id(child)] = prefix + child.name
                qual = f"{prefix}{child.name}.<locals>."
            elif isinstance(child, ast.ClassDef):
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                self.classes.append((start, child.end_lineno, child, prefix + child.name))
                qual = f"{prefix}{child.name}."
            if isinstance(child, ast.stmt):
                self.stmts.append((child.lineno, child.end_lineno, child))
            self._walk(child, qual)

    def innermost_func(self, line):
        best = None
        for s, b, e, n in self.funcs:
            if s <= line <= e and (best is None or s >= best[0]):
                best = (s, b, e, n)
        return best

    def innermost_class(self, line):
        best = None
        for s, e, n, q in self.classes:
            if s <= line <= e and (best is None or s >= best[0]):
                best = (s, e, n, q)
        return best

    def innermost_stmt(self, lo, hi, within):
        """Innermost statement containing lines lo..hi, inside span `within` (s, e)."""
        best = None
        for s, e, n in self.stmts:
            if s <= lo and hi <= e and within[0] <= s and e <= within[1]:
                if best is None or (s, -e) > (best[0], -best[1]):
                    best = (s, e, n)
        return best


def header_end(node):
    """Last line of a statement's own code: the whole statement if simple, or the
    line before its first child statement if compound."""
    body = getattr(node, "body", None)
    if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
        return max(node.lineno, body[0].lineno - 1)
    return node.end_lineno


_scope_cache: dict = {}


def scopes_for(path):
    if path not in _scope_cache:
        src = old_source(path)
        try:
            _scope_cache[path] = Scopes(src) if src is not None else None
        except SyntaxError:
            _scope_cache[path] = None
    return _scope_cache[path]


WORD_RE = re.compile(r"""["']([A-Za-z_][A-Za-z0-9_ ]{2,})["']""")


ENTRY_RE = re.compile(r"^\s*exp\.([A-Za-z_]\w*)(?:\.[A-Za-z_][\w.]*)?\s*(?::.*|,)\s*(?:#.*)?$")


def entry_refs(fp):
    """If every changed line in this file is a dict/set entry keyed by `exp.Name`,
    return the referenced expression class names; otherwise None."""
    if fp is None:
        return None
    refs = set()
    for h in fp.hunks:
        for ln in h[4]:
            if ln[:1] not in "+-" or not ln[1:].strip():
                continue
            mm = ENTRY_RE.match(ln[1:])
            if not mm:
                return None
            refs.add(mm.group(1))
    return refs or None


def patch_words(fp):
    """String literals on the changed lines (e.g. SQL function names)."""
    if fp is None:
        return []
    text = fp.added_text() + "\n" + fp.removed_text()
    return sorted({w.strip() for w in WORD_RE.findall(text) if len(w.strip()) >= 3})


# Huge integration tests and the cheap tests that exercise the same machinery.
GIANT_PROXIES = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": (
        "tests/test_executor.py::",
        "tests/test_optimizer.py::TestOptimizer::test_tpcds",
        "tests/test_optimizer.py::TestOptimizer::test_optimize",
    ),
}
if os.environ.get("BR_MAXNODES_PROXIES") == "1":  # under evaluation
    GIANT_PROXIES["tests/test_parser.py::TestParser::test_max_nodes"] = (
        "tests/test_parser.py::",
        "tests/test_transpile.py::",
        "tests/test_expressions.py::",
    )

# --------------------------------------------------------------------------- the maps


class Maps:
    def __init__(self):
        with open(os.path.join(MAP_DIR, "covmap.json")) as fh:
            c = json.load(fh)
        with open(os.path.join(MAP_DIR, "meta.json")) as fh:
            m = json.load(fh)
        self.tests = c["tests"]
        self.runtime = c["runtime"]
        self.sets = c["sets"]
        self.files = {f: {int(k): v for k, v in lines.items()} for f, lines in c["files"].items()}
        self.all = set(range(len(self.tests)))
        self.meta = m
        self.test_files = {k: set(v) for k, v in m["test_files"].items()}
        self.file_of_dialect = {}
        for d, fs in m["dialect_files"].items():
            for f in fs:
                self.file_of_dialect[f] = d
        covered = set()
        for s in self.sets:
            covered.update(s)
        # Tests that run no measured line (subprocess / static inspection tests).
        self.uncovered = self.all - covered
        self._file_union = {}
        self.import_ran = {f: set(v) for f, v in c.get("import_ran", {}).items()}
        self.cpu = {}
        self.bench_ref = None
        self.class_setup = {}
        cpu_path = os.path.join(MAP_DIR, "cpu.json")
        if os.path.exists(cpu_path):
            with open(cpu_path) as fh:
                cj = json.load(fh)
            tid = {t: i for i, t in enumerate(self.tests)}
            self.cpu = {tid[t]: v for t, v in cj["test"].items() if t in tid}
            self.bench_ref = cj.get("bench")
            self.class_setup = cj.get("class_setup", {})
        self.fixture_readers = {k: set(v) for k, v in m.get("fixture_readers", {}).items()}
        self.traced_classes = set(m.get("traced_classes", []))
        self.attr_tests = {k: set(v) for k, v in m.get("attr_tests", {}).items()}
        self.attr_subclasses = m.get("attr_subclasses", {})
        self.attr_own = m.get("attr_own", {})
        self.attr_mro = m.get("attr_mro", {})
        self.import_read_attrs = set(m.get("import_read_attrs", []))
        self.import_reads = set(m.get("import_reads", []))
        self.collect_reads = set(m.get("collect_reads", []))
        tid = {t: i for i, t in enumerate(self.tests)}
        self.giant_proxies = {}
        for g, prefixes in GIANT_PROXIES.items():
            if g in tid:
                self.giant_proxies[tid[g]] = {
                    i for i, t in enumerate(self.tests)
                    if t != g and any(t.startswith(p) for p in prefixes)
                }
        self.class_tests = m.get("class_tests", {})
        self._test_src = {}

    def expression_tests(self, name):
        """Tests that instantiate the expression class `name` (or a subclass)."""
        if not hasattr(self, "_expr_index"):
            idx = {}
            for key, ids in self.class_tests.items():
                path, qual = key.split(":", 1)
                if path.startswith("sqlglot/expressions/"):
                    idx.setdefault(qual.rsplit(".", 1)[-1], set()).update(ids)
            self._expr_index = idx
        return self._expr_index.get(name, set())

    def tests_mentioning(self, words):
        """Tests whose own function source contains any of `words` (case-insensitive)."""
        words = [w.lower() for w in words]
        out = set()
        if not words:
            return out
        for path, spans in self.meta["test_spans"].items():
            src = self._test_src.get(path)
            if src is None:
                try:
                    with open(os.path.join(SQLGLOT_DIR, path), encoding="utf-8") as fh:
                        src = fh.read().lower().splitlines()
                except OSError:
                    src = []
                self._test_src[path] = src
            for a, b, i in spans:
                body = "\n".join(src[a - 1 : b])
                if any(w in body for w in words):
                    out.add(i)
        return out

    def line(self, path, line):
        k = self.files.get(path, {}).get(line)
        return set(self.sets[k]) if k is not None else set()

    def span(self, path, lo, hi):
        lines = self.files.get(path, {})
        out = set()
        for ln, k in lines.items():
            if lo <= ln <= hi:
                out.update(self.sets[k])
        return out

    def file_union(self, path):
        if path not in self._file_union:
            out = set()
            for k in set(self.files.get(path, {}).values()):
                out.update(self.sets[k])
            self._file_union[path] = out
        return self._file_union[path]

    def dialect_family_tests(self, dialect):
        out = set()
        for d in self.meta["dialect_family"].get(dialect, [dialect]):
            out.update(self.meta["dialect_tests"].get(d, []))
        return out

    def importer_closure(self, test_path):
        """Test files that import test_path (transitively), including itself."""
        mod = test_path[:-3] if test_path.endswith(".py") else test_path
        seen = {test_path}
        todo = [mod]
        while todo:
            cur = todo.pop()
            for f in self.meta["importers"].get(cur, []):
                if f not in seen:
                    seen.add(f)
                    todo.append(f[:-3])
        return seen


# --------------------------------------------------------------------------- selection


SETTING_RE = re.compile(r"^_?[A-Z][A-Z0-9_]*$")


class Selector:
    def __init__(self, maps: Maps):
        self.m = maps
        self._new_trees = {}

    def select(self, patch_text):
        """Returns (test ids, reasons, plan). plan is a list of verification steps for
        blast.verify, or None if this commit can't be verified by hot-patching."""
        reasons = []
        sel = set()
        self.plan = []
        self.files = []  # [(path, new text or None)] for fresh-process verification
        self.touch_sets = []  # candidate set of each changed point (code files)
        self.direct = set()  # tests the patch edits directly: near-certain failures
        self._new_trees = {}
        for fp in parse_patch(patch_text):
            self._step = None  # set by select_file when the file can be hot-patched
            n_touch = len(self.touch_sets)
            got, why = self.select_file(fp)
            if len(self.touch_sets) == n_touch:
                self.touch_sets.append(got)  # fixtures, tests, module-level: one set per file
            sel |= got
            reasons.append(f"{fp.path}:{why}:{len(got)}")
            if self.plan is not None:
                if self._step is None:
                    self.plan = None
                else:
                    self.plan.extend(self._step)
            if self.files is not None:
                self.files = self._file_contents(fp, self.files)
        return sel, reasons, self.plan

    @staticmethod
    def _file_contents(fp, files):
        path = fp.path
        if path is None:
            return None
        if not (path.startswith("sqlglot/") or path.startswith("tests/")):
            return files  # nothing the tests can see
        if fp.new_path is None:
            return files + [(fp.old_path, None)]
        if not fp.hunks:
            if fp.old_path and fp.old_path != fp.new_path and path.endswith(".py"):
                old = old_source(fp.old_path)  # bare rename: same text, new place
                if old is not None:
                    return files + [(fp.new_path, old), (fp.old_path, None)]
            return None  # binary, or nothing we can reproduce
        old = old_source(fp.old_path) if fp.old_path else ""
        new = apply_patch(old if old is not None else "", fp)
        if new is None:
            return None
        out = files + [(fp.new_path, new)]
        if fp.old_path and fp.old_path != fp.new_path:
            out.append((fp.old_path, None))
        return out

    def select_file(self, fp: FilePatch):
        m = self.m
        path = fp.path
        if path is None:
            return set(), "nopath"

        if path.startswith("tests/fixtures/"):
            if fp.old_path is None:
                return set(), "new-fixture"
            if fp.new_path is None or fp.old_path != fp.new_path or not fp.hunks:
                # Deleted, moved or binary: whoever reads it is affected; can't verify.
                return set(m.meta["fixture_tests"].get(fp.old_path, [])), "fixture-moved"
            old = old_source(path)
            new = apply_patch(old, fp) if old is not None and fp.hunks else None
            if new is not None:
                readers = sorted(m.tests[i] for i in m.fixture_readers.get(path, ()))
                self._step = [("fixture", path, None, new, readers)]
            # Tests traced reading this exact file will almost surely fail.
            self.direct |= set(m.fixture_readers.get(path, ()))
            return set(m.meta["fixture_tests"].get(path, [])), "fixture"

        is_py = path.endswith(".py")
        in_pkg = path.startswith("sqlglot/")
        in_tests = path.startswith("tests/")
        if not is_py or not (in_pkg or in_tests):
            self._step = []
            return set(), "non-code"

        old = old_source(fp.old_path) if fp.old_path else None
        if fp.new_path is None or (fp.old_path and fp.old_path != fp.new_path):
            # Deleted or moved: everything importing the old path breaks.
            if in_tests:
                return self.test_file_all(path), "deleted-test-file"
            return set(m.all), "deleted-module"
        if old is None:  # new file
            return set(), "new-file"

        new = apply_patch(old, fp)
        if new is None:
            log(f"  ! patch does not apply cleanly to {path}; being conservative")
            return (self.test_file_all(path) if in_tests else set(m.all)), "noapply"
        try:
            compile(new, path, "exec")
        except SyntaxError:
            return (self.test_file_all(path) if in_tests else set(m.all)), "syntax-error"
        if code_tokens(old) == code_tokens(new):
            self._step = []
            return set(), "comment-only"

        sc = scopes_for(path)
        if sc is None:
            return set(m.all), "noscope"

        sel = set()
        kinds = set()
        touched = []
        for ln in sorted(fp.changed_old_lines()):
            got, kind = self.for_line(path, sc, ln, ln, fp=fp)
            sel |= got
            kinds.add(kind)
            touched.append(ln)
            self.touch_sets.append(got)
        for p, indent in sorted(fp.insert_points().items()):
            got, kind = self.for_line(path, sc, p, p + 1, insertion=True, indent=indent, fp=fp)
            sel |= got
            kinds.add(kind)
            touched.append(p)
            self.touch_sets.append(got)
        if in_tests:
            order = [path] + sorted(self.m.importer_closure(path) - {path})
            self._step = [("test", path, None, new, None)] + [
                ("test", f, None, None, None) for f in order[1:]
            ]
        elif kinds <= {"stmt", "block", "func-body"}:
            self._step = self.code_step(path, sc, old, new, touched)
        return sel, "+".join(sorted(kinds))

    def code_step(self, path, sc, old, new, lines):
        """Hot-patch plan for changes that sit entirely inside function bodies that did
        not run at import time. None if that can't be done faithfully."""
        ran = self.m.import_ran.get(path, set())
        funcs = set()
        for ln in lines:
            f = sc.outermost_func(ln)
            if f is None or ln < f[1] - 1:
                return None
            start, body_start, end, node = f
            if any(body_start <= x <= end for x in ran):
                return None  # called while importing: results computed with old code
            funcs.add(sc.func_qual[id(node)])
        return [("code", path, old, new, sorted(funcs))]

    def test_file_all(self, path):
        out = set()
        for f in self.m.importer_closure(path):
            out |= self.m.test_files.get(f, set())
        return out

    def settings_change(self, path, c, fp):
        """If the only change to class c is to UPPERCASE settings that are read at runtime
        only (never while classes are being built), return the tests that read those
        settings on c or a subclass. Otherwise None."""
        m = self.m
        key = f"{path}:{c[3]}"
        subtree = m.attr_subclasses.get(key)
        if not subtree or fp is None:
            return None
        if path not in self._new_trees:
            new = apply_patch(old_source(path), fp)
            try:
                self._new_trees[path] = Scopes(new) if new is not None else None
            except SyntaxError:
                self._new_trees[path] = None
        new_sc = self._new_trees[path]
        if new_sc is None:
            return None
        new_cls = [x for x in new_sc.classes if x[3] == c[3]]
        if len(new_cls) != 1:
            return None

        def parts(node):
            settings, other = {}, []
            for st in node.body:
                target = None
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    target = st.targets[0].id
                elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
                    target = st.target.id
                if target is not None and SETTING_RE.match(target):
                    settings[target] = ast.dump(st.value)
                elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    other.append(("def", st.name))  # bodies are scoped separately
                elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
                    continue  # docstring
                else:
                    other.append(("stmt", ast.dump(st)))
            return settings, sorted(other)

        old_set, old_other = parts(c[2])
        new_set, new_other = parts(new_cls[0][2])
        if old_other != new_other:
            return None
        changed = {n for n in set(old_set) | set(new_set) if old_set.get(n) != new_set.get(n)}
        if not changed:
            return None
        inherited = set()
        for k in m.attr_mro.get(key, ()):
            inherited.update(m.attr_own.get(k, ()))
        got = set()
        for a in changed:
            if a in m.import_read_attrs or a not in inherited:
                return None  # baked in at import, or a brand-new name (getattr defaults)
            for s in subtree:
                k = f"{s}.{a}"
                if k in m.import_reads or k in m.collect_reads:
                    return None
                got |= m.attr_tests.get(k, set())
        return got

    def module_level(self, path, sc=None, line=None, fp=None):
        """A change outside any function body: it runs at import or class creation."""
        m = self.m
        if path.startswith("tests/"):
            return self.test_file_all(path), "test-module"
        c = sc.innermost_class(line) if sc is not None and line is not None else None
        if c is not None:
            key = f"{path}:{c[3]}"
            narrowed = self.settings_change(path, c, fp)
            if narrowed is not None:
                return narrowed, "class-setting"
            if key in m.traced_classes:
                # Class attributes of an expression/parser/generator/tokenizer class only
                # matter to tests that build an instance of it (or of a subclass). Names
                # added or removed in the patch may also change what other SQL parses to.
                got = set(m.class_tests.get(key, []))
                refs = entry_refs(fp)
                if refs:
                    # Every changed line is a `exp.Foo: ...` / `exp.Foo,` entry: it only
                    # matters when an instance of Foo meets an instance of this class.
                    users = set()
                    for r in refs:
                        users |= m.expression_tests(r)
                    return got & users, "class-entry"
                got |= m.tests_mentioning(patch_words(fp))
                return got, "class-traced"
        d = m.file_of_dialect.get(path)
        if d is not None:
            return m.dialect_family_tests(d) | m.uncovered, "dialect-module"
        return set(m.all), "core-module"

    def for_line(self, path, sc: Scopes, lo, hi, insertion=False, indent=None, fp=None):
        got, kind = self._for_line(path, sc, lo, hi, insertion, indent, fp)
        m = self.m
        if path.startswith("sqlglot/"):
            f = sc.outermost_func(max(lo, 1))
            qual = sc.func_qual[id(f[3])] if f else None
            # Runs once per generator class per process (cached): coverage credits only
            # the first test per class, but every generating test depends on it.
            if path == "sqlglot/generator.py" and (
                qual == "_build_dispatch" or (qual == "Generator.__init__" and 1004 <= lo <= 1009)
            ):
                return got | set(m.class_tests.get("sqlglot/generator.py:Generator", [])), "once"
            # A changed line that ran at import (factory bodies, lazy loaders, ...) has
            # effects baked in at import time: treat it like module-level code.
            ran = m.import_ran.get(path, ())
            if kind in ("stmt", "block", "func-body") and (
                (not insertion and lo in ran) or (insertion and lo in ran and f and lo >= f[1])
            ):
                wide, _ = self.module_level(path, sc, lo, fp)
                return got | wide, "import-run"
        return got, kind

    def _for_line(self, path, sc: Scopes, lo, hi, insertion=False, indent=None, fp=None):
        m = self.m
        lo = max(lo, 1)
        f = sc.innermost_func(lo) if not insertion else None
        if insertion:
            # Inside a function only if the inserted code is indented deeper than the
            # def and the point is within that function's body.
            f1 = sc.innermost_func(lo)
            if f1 is not None and lo >= f1[1] - 1:
                def_indent = f1[3].col_offset
                if indent is None or indent > def_indent:
                    f = f1
        if f is None:
            cls_line = None if (insertion and indent == 0) else lo
            return self.module_level(path, sc, cls_line, fp)

        fstart, body_start, fend, fnode = f
        if path.startswith("tests/") and fnode.name.startswith("test"):
            # A test function itself: just that test (its id comes from the span table).
            for a, b, i in m.meta["test_spans"].get(path, []):
                if a <= lo <= b:
                    self.direct.add(i)
                    return {i}, "test-body"
        if lo < body_start and not insertion:
            # def line / decorator / signature: anyone who calls it.
            return m.span(path, body_start, fend), "func-header"
        # Innermost statement inside the function that contains the change.
        hi_eff = hi if insertion else lo
        st = sc.innermost_stmt(lo, hi_eff, (body_start, fend))
        if st is None:
            return m.span(path, body_start, fend) | m.line(path, body_start), "func-body"
        s, e, node = st
        # Coverage on 3.12+ credits the line each sub-expression runs on, so take the
        # union over the statement's own lines (the header only, for compound ones).
        got = m.span(path, s, header_end(node))
        if insertion or hasattr(node, "body"):
            return got, "block"  # every test that reached this block
        return got, "stmt"


# --------------------------------------------------------------------------- tests.json


def load_tests_json(maps: Maps, path):
    """Map our test ids to the grader's numbers; runtimes in whole milliseconds.

    Format: {"tests": [{"number", "id" (pytest node id), "file", "seconds"}], "skipped": [...]}
    Returns (to_theirs, runtime_ms, unknown) where unknown lists grader tests we have no
    map entry for, as (number, file).
    """
    with open(path) as fh:
        raw = json.load(fh)
    ours = {t: i for i, t in enumerate(maps.tests)}
    to_theirs, runtime, unknown = {}, {}, []
    for e in raw["tests"]:
        num = int(e["number"])
        runtime[num] = int(round(float(e["seconds"]) * 1000))
        i = ours.get(e["id"])
        if i is None:
            unknown.append((num, e.get("file", ""), e["id"]))
        else:
            to_theirs[i] = num
    return to_theirs, runtime, unknown


# --------------------------------------------------------------------------- scheduling


def schedule(nums, runtime):
    """Longest-first onto the least loaded worker; pick the worker count with lowest cost."""
    if not nums:
        return [], 0
    items = sorted(nums, key=lambda n: (-runtime.get(n, 0), n))
    best = None
    for k in range(1, min(MAX_WORKERS, len(items)) + 1):
        loads = [0] * k
        bins = [[] for _ in range(k)]
        for n in items:
            j = loads.index(min(loads))
            loads[j] += runtime.get(n, 0)
            bins[j].append(n)
        cost = max(loads) + WORKER_COST * k
        if best is None or cost < best[1]:
            best = (bins, cost)
    return best


# --------------------------------------------------------------------------- main


VERIFY = os.environ.get("VERIFY", "1") == "1"
# Grader CPU vs. the machine cpu.json came from. "auto": measured at startup with a fixed
# sqlglot benchmark against the reference time stored in cpu.json.
SLOWDOWN_SETTING = os.environ.get("SLOWDOWN", "auto")
SLOWDOWN = 1.8 if SLOWDOWN_SETTING == "auto" else float(SLOWDOWN_SETTING)
# Real verification runs ~1.3x the per-test numbers (fork copy-on-write, memory effects
# the small benchmark doesn't feel).
BENCH_MARGIN = float(os.environ.get("BENCH_MARGIN", "1.35"))
BATCH_BUDGET = float(os.environ.get("BATCH_BUDGET", "140"))  # wall seconds for verification
# Tests we never try to verify in-process: they inspect the process itself.
NEVER_VERIFY = {
    "tests/dialects/test_dialect.py::TestDialect::test_lazy_load",
}


FORK_CPU = 0.03  # seconds of CPU per verification child beyond the tests themselves
FRESH_CPU = float(os.environ.get("FRESH_CPU", "0.6"))  # fresh interpreter + imports


def choose_verify(maps, ours, never, to_theirs, runtime, overhead=FORK_CPU):
    """Pick which candidates to run here. Assumes they pass (the common case) and
    compares: ship the rest unverified vs. the CPU to verify (grader seconds)."""
    ok = [i for i in ours if i not in never and i in to_theirs and i in maps.cpu]
    if not ok:
        return []

    def rec(i):
        return runtime.get(to_theirs[i], 0) / 1000

    ok.sort(key=lambda i: SLOWDOWN * maps.cpu[i] / max(rec(i), 0.001))
    rest_all = [to_theirs[i] for i in ours if i in to_theirs]
    best_cost = schedule(rest_all, runtime)[1] / 1000
    best_n = 0
    cuts = set()
    for thr in (0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.5, 2.5, 4.0):
        n = sum(1 for i in ok if SLOWDOWN * maps.cpu[i] / max(rec(i), 0.001) <= thr)
        cuts.add(n)
    cuts.add(len(ok))
    for n in sorted(cuts):
        if n == 0:
            continue
        chosen = set(ok[:n])
        classes = {maps.tests[i].rsplit("::", 1)[0] for i in chosen}
        cpu = sum(maps.cpu[i] for i in chosen) + sum(maps.class_setup.get(c, 0) for c in classes)
        rest = [to_theirs[i] for i in ours if i in to_theirs and i not in chosen]
        cost = schedule(rest, runtime)[1] / 1000 + SLOWDOWN * (cpu + overhead)
        if cost < best_cost:
            best_cost, best_n = cost, n
    return ok[:best_n]


def verify_commit(maps, sel, state, ours, plan, to_theirs, runtime, never, wall0, k, n_commits):
    """Run the candidates worth running here; returns (tests to ship, log note)."""
    keep = set(ours)
    fresh = plan is None and sel.files is not None
    if not (VERIFY and (plan is not None or fresh) and ours):
        return keep, ""
    # A giant test may be left to its proxies: cheap tests that run the same
    # machinery. It's dropped only if every changed point that reaches it also
    # reaches a proxy we verify, and every such proxy passes.
    giants = {}
    for g, proxies in maps.giant_proxies.items():
        if g in ours and all(not (g in ts) or (ts & proxies) for ts in sel.touch_sets):
            giants[g] = (proxies & set(ours)) - never
    pool = set(ours) - set(giants) - sel.direct
    check = choose_verify(maps, pool, never, to_theirs, runtime, FRESH_CPU if fresh else FORK_CPU)
    must = set().union(*giants.values()) if giants else set()
    if must - set(check):
        check = sorted(set(check) | must)
    left = BATCH_BUDGET - (time.monotonic() - wall0)
    budget = min(30.0, max(0.0, left) / max(1, n_commits - k) * 3)
    if not check or budget < 1.0:
        return keep, ""
    if "verifier" not in state:
        from blast.verify import Verifier

        state["verifier"] = Verifier(SQLGLOT_DIR, sorted({t.split("::")[0] for t in maps.tests}))
    verifier = state["verifier"]
    nodes = [maps.tests[i] for i in check]
    limits = {maps.tests[i]: max(3.0, 10 * runtime.get(to_theirs[i], 0) / 1000) for i in check}
    import resource as ru

    c0 = ru.getrusage(ru.RUSAGE_CHILDREN)
    # A failing proxy means the giant ships anyway, and next to it the rest packs onto
    # other workers nearly free: stop verifying right there.
    stop_on = frozenset(maps.tests[i] for i in must)
    if fresh:
        failed, unverified = verifier.run_fresh(sel.files, nodes, limits, budget, stop_on)
    else:
        failed, unverified = verifier.run(plan, nodes, limits, budget, stop_on)
        if verifier.last_apply_error and sel.files is not None:
            fresh = True  # hot patch didn't apply: redo it from a shadow tree
            failed, unverified = verifier.run_fresh(sel.files, nodes, limits, budget, stop_on)
    c1 = ru.getrusage(ru.RUSAGE_CHILDREN)
    child_cpu = (c1.ru_utime + c1.ru_stime) - (c0.ru_utime + c0.ru_stime)
    bad = failed | unverified
    keep = (set(ours) - set(check)) | {i for i in check if maps.tests[i] in bad}
    dropped = []
    for g, proxies in giants.items():
        if proxies and not any(maps.tests[i] in bad for i in proxies):
            keep.discard(g)
            dropped.append(maps.tests[g].split("::")[-1])
    classes = {maps.tests[i].rsplit("::", 1)[0] for i in check}
    predicted = (sum(maps.cpu.get(i, 0) for i in check)
                 + sum(maps.class_setup.get(c, 0) for c in classes)
                 + (FRESH_CPU if fresh else FORK_CPU))
    note = (f" {'fresh' if fresh else 'fork'}-verified {len(check)}: {len(failed)} fail,"
            f" predicted {predicted:.2f}s,"
            f" {len(unverified)} undecided, child cpu {child_cpu:.2f}s"
            + (f" dropped {dropped}" if dropped else ""))
    return keep, note


def main():
    t0 = time.process_time()
    wall0 = time.monotonic()
    maps = Maps()
    sel = Selector(maps)
    tests_json = os.path.join(DATA_DIR, "tests.json")
    if os.path.exists(tests_json):
        to_theirs, runtime, unknown = load_tests_json(maps, tests_json)
        if unknown:
            log(f"{len(unknown)} tests in tests.json not in our map, e.g. {unknown[:3]}")
    else:
        to_theirs = {i: i for i in range(len(maps.tests))}
        runtime = {i: int(round(r * 1000)) for i, r in enumerate(maps.runtime)}
        unknown = []
        log("no tests.json; using our own numbering")
    # Grader tests with no map entry: include whenever their file is touched by a patch.
    unknown_by_file = {}
    for num, f, _ in unknown:
        unknown_by_file.setdefault(f, []).append(num)

    never = set(maps.uncovered) | {i for i, t in enumerate(maps.tests) if t in NEVER_VERIFY}
    state = {}  # holds the Verifier
    if VERIFY:
        try:
            from blast.verify import Verifier, bench

            state["verifier"] = Verifier(SQLGLOT_DIR, sorted({t.split("::")[0] for t in maps.tests}))
            if SLOWDOWN_SETTING == "auto" and maps.bench_ref and state["verifier"].ok:
                global SLOWDOWN
                here = bench()
                SLOWDOWN = min(5.0, max(0.7, BENCH_MARGIN * here / maps.bench_ref))
                log(f"cpu benchmark {here:.4f}s vs {maps.bench_ref:.4f}s reference: SLOWDOWN={SLOWDOWN:.2f}")
        except Exception as e:
            log(f"verifier setup failed: {e!r}")

    commit_dir = os.path.join(DATA_DIR, "commits")
    names = sorted(fn for fn in os.listdir(commit_dir) if fn.endswith(".patch"))
    total = 0
    for k, fn in enumerate(names):
        cid = fn[: -len(".patch")]
        with open(os.path.join(commit_dir, fn), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        try:
            ours, reasons, plan = sel.select(text)
        except Exception as e:  # never crash: fall back to everything, unverified
            log(f"{cid}: selector error {e!r}; selecting all")
            ours, reasons, plan = set(maps.all), ["error"], None
            sel.files = None
        extra = set()
        for f in re.findall(r"^diff --git a/(\S+) b/(\S+)", text, re.M):
            for p in f:
                extra.update(unknown_by_file.get(p, []))
                if p.startswith("sqlglotc/"):  # tests/test_sqlglotc.py loads sqlglotc/setup.py
                    extra.update(unknown_by_file.get("tests/test_sqlglotc.py", []))

        try:
            keep, note = verify_commit(maps, sel, state, ours, plan, to_theirs, runtime,
                                       never, wall0, k, len(names))
        except Exception as e:  # verification is an optimization: never let it crash a run
            log(f"{cid}: verification error {e!r}; keeping all candidates")
            keep, note = set(ours), " verify-error"
        nums = sorted({to_theirs[i] for i in keep if i in to_theirs} | extra)
        bins, cost = schedule(nums, runtime)
        total += cost
        print(json.dumps({"commit": cid, "workers": bins}, separators=(",", ":")), flush=True)
        log(f"{cid}: {len(ours)} cand -> {len(nums)} tests, est {cost / 1000:.1f}s"
            f"{note}  [{'; '.join(reasons)}]")
    ru = __import__("resource")
    child = ru.getrusage(ru.RUSAGE_CHILDREN)
    log(f"estimated test cost {total / 1000:.1f}s, cpu self {time.process_time() - t0:.2f}s"
        f" + children {child.ru_utime + child.ru_stime:.2f}s, wall {time.monotonic() - wall0:.1f}s")


if __name__ == "__main__":
    main()
