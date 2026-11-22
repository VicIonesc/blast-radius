"""Run candidate tests against a patched sqlglot, cheaply, inside the container.

The parent imports sqlglot and the test modules once. For each commit it forks a child
that hot-patches the changed functions in place (swapping __code__), serves patched
fixture files and re-executes patched test modules, then runs the candidate tests with
unittest and streams back which ones failed. Anything the child can't do faithfully is
reported as "unverified" and the caller falls back to including every candidate.
"""

from __future__ import annotations

import builtins
import gc
import importlib
import io
import json
import os
import pickle
import select
import signal
import sys
import time
import types
import unittest
from concurrent.futures import Future


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- inline pools


def _cross(obj):
    return pickle.loads(pickle.dumps(obj))


class InlineExecutor:
    """ProcessPoolExecutor stand-in: same pickling at the boundary, no processes."""

    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*_cross(tuple(initargs)))

    def submit(self, fn, *args, **kwargs):
        fut = Future()
        try:
            args, kwargs = _cross((args, kwargs))
            fut.set_result(_cross(fn(*args, **kwargs)))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [_cross(fn(*_cross(a))) for a in zip(*iterables)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --------------------------------------------------------------------------- code helpers


def code_objects(code, out=None):
    """{(qualname, firstlineno): code} for every function code object in a module code."""
    if out is None:
        out = {}
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            out[(c.co_qualname, c.co_firstlineno)] = c
            code_objects(c, out)
    return out


class TestTimeout(Exception):
    pass


class Result(unittest.TestResult):
    def __init__(self, emit, limits):
        super().__init__()
        self.emit = emit
        self.limits = limits
        self.bad = set()

    def _id(self, test):
        return getattr(test, "_br_node", None) or test.id()

    def startTest(self, test):
        super().startTest(test)
        lim = self.limits.get(self._id(test), 30.0)
        signal.setitimer(signal.ITIMER_REAL, lim)

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        super().stopTest(test)
        node = self._id(test)
        self.emit(("F " if node in self.bad else "P ") + node)

    def _fail(self, test):
        node = getattr(test, "_br_node", None)
        if node is not None:
            self.bad.add(node)
        else:
            # Class/module fixture error: _ErrorHolder whose description names the class.
            self.emit("E " + str(test))

    def addError(self, test, err):
        super().addError(test, err)
        self._fail(test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._fail(test)

    def addSubTest(self, test, subtest, outcome):
        super().addSubTest(test, subtest, outcome)
        if outcome is not None:
            self._fail(test)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._fail(test)


def _alarm(signum, frame):
    raise TestTimeout("test exceeded its time limit")


def run_suite(get_module, nodes, limits, emit):
    """Run node ids (grouped by class) with unittest; emit P/F lines per test."""
    signal.signal(signal.SIGALRM, _alarm)
    suite = unittest.TestSuite()
    for node in nodes:
        f, cls, meth = node.split("::")
        m = get_module(f)
        if m is None:
            emit("F " + node)
            continue
        if getattr(m, "ProcessPoolExecutor", None) is not None:
            m.ProcessPoolExecutor = InlineExecutor
        case = getattr(m, cls)(meth)
        case._br_node = node
        suite.addTest(case)
    res = Result(emit, limits)
    suite.run(res)
    suite._tearDownPreviousClass(None, res)


def build_shadow(src, dst):
    """Mirror src/{sqlglot,tests} into dst with symlinks (pyc caches shared)."""
    for top in ("sqlglot", "tests"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(src, top)):
            rel = os.path.relpath(dirpath, src)
            os.makedirs(os.path.join(dst, rel), exist_ok=True)
            if "__pycache__" in dirnames:
                dirnames.remove("__pycache__")
                os.symlink(os.path.join(dirpath, "__pycache__"), os.path.join(dst, rel, "__pycache__"))
            for fn in filenames:
                os.symlink(os.path.join(dirpath, fn), os.path.join(dst, rel, fn))


def fresh_child_main(fd):
    """Entry point of a fresh verification process (sqlglot comes from the shadow tree)."""
    out = os.fdopen(fd, "w", buffering=1)

    def emit(line):
        out.write(line + "\n")

    try:
        job = json.loads(sys.stdin.read())
        os.environ["SKIP_INTEGRATION"] = "0"

        def get_module(f):
            try:
                return importlib.import_module(f[:-3].replace("/", "."))
            except Exception:
                return None

        run_suite(get_module, job["nodes"], job["limits"], emit)
        emit("DONE")
    except BaseException as e:  # noqa: BLE001
        emit(f"X {type(e).__name__}: {e}")
    finally:
        out.flush()
        os._exit(0)


BENCH_SQL = [
    "SELECT a, SUM(b) AS s FROM t JOIN u ON t.id = u.id WHERE c > 1 GROUP BY a HAVING SUM(b) > 2 ORDER BY s DESC LIMIT 10",
    "WITH x AS (SELECT * FROM y WHERE z IN (SELECT w FROM v)) SELECT CASE WHEN a THEN 1 ELSE 2 END FROM x",
    "SELECT DATE_TRUNC('month', ts), COUNT(DISTINCT id) OVER (PARTITION BY k ORDER BY ts) FROM events",
    "SELECT CAST(a AS DECIMAL(10, 2)), COALESCE(b, c, d), REGEXP_REPLACE(e, 'x+', 'y') FROM tbl",
    "INSERT INTO t (a, b) SELECT a, b FROM s UNION ALL SELECT c, d FROM r",
]


def bench_once():
    """A fixed sqlglot workload: parse, transpile and optimize a few queries."""
    import sqlglot
    from sqlglot.optimizer import optimize

    for _ in range(3):
        for q in BENCH_SQL:
            for dialect in ("duckdb", "snowflake", "bigquery", "tsql"):
                sqlglot.transpile(q, read="postgres", write=dialect)
        optimize(sqlglot.parse_one(BENCH_SQL[0]), schema={"t": {"a": "int", "b": "int", "c": "int", "id": "int"},
                                                          "u": {"id": "int"}})


def bench():
    """CPU seconds of bench_once in a forked child (keeps caches out of the parent).
    Best of three."""
    import resource

    best = None
    for _ in range(3):
        c0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        pid = os.fork()
        if pid == 0:
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                bench_once()
            finally:
                os._exit(0)
        os.waitpid(pid, 0)
        c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        t = (c1.ru_utime + c1.ru_stime) - (c0.ru_utime + c0.ru_stime)
        best = t if best is None else min(best, t)
    return best


# --------------------------------------------------------------------------- verifier


class Verifier:
    def __init__(self, sqlglot_dir, test_files):
        t0 = time.process_time()
        sqlglot_dir = os.path.abspath(sqlglot_dir)
        self.dir = sqlglot_dir
        self.ok = False
        os.environ["SKIP_INTEGRATION"] = "0"
        if sqlglot_dir not in sys.path:
            sys.path.insert(0, sqlglot_dir)
        try:
            from blast.worlddiff import load_world

            # Load every module now: one imported lazily in the child would come from
            # disk unpatched, and its functions would be missing from the index.
            self.modules_files = list(test_files)
            self.modules = load_world(sqlglot_dir, test_files)
            for f in sorted(test_files):
                if f not in self.modules:  # import error: verified by fallback
                    log(f"verify: cannot import {f}")
            # Index every live sqlglot function by where its code came from.
            roots = {sqlglot_dir + os.sep, os.path.realpath(sqlglot_dir) + os.sep}
            self.funcs = {}
            for obj in gc.get_objects():
                if isinstance(obj, types.FunctionType):
                    c = obj.__code__
                    fn = os.path.abspath(c.co_filename)
                    for root in roots:
                        if fn.startswith(root):
                            rel = fn[len(root):].replace(os.sep, "/")
                            if rel.startswith("sqlglot/"):
                                key = (rel, c.co_qualname, c.co_firstlineno)
                                self.funcs.setdefault(key, []).append(obj)
                            break
            gc.collect()
            gc.freeze()
            self.ok = True
        except Exception as e:
            log(f"verify: setup failed: {e!r}")
        log(f"verify: ready={self.ok} in {time.process_time() - t0:.2f}s cpu")

    # ---------------------------------------------------------------- child side

    def _apply(self, plan):
        """Apply the patch inside the child. plan: list of (kind, path, old_text, new_text, funcs)."""
        overrides = {}
        reexec = []
        self._readers = {}
        for kind, path, old, new, funcs in plan:
            if kind == "code":
                old_code = compile(old, os.path.join(self.dir, path), "exec")
                new_code = compile(new, os.path.join(self.dir, path), "exec")
                olds = code_objects(old_code)
                news = {}
                for (q, ln), c in code_objects(new_code).items():
                    news.setdefault(q, []).append(c)
                for q in funcs:
                    old_keys = [k for k in olds if k[0] == q]
                    cands = news.get(q, [])
                    if len(old_keys) != 1 or len(cands) != 1:
                        raise RuntimeError(f"ambiguous function {q} in {path}")
                    live = self.funcs.get((path, q, old_keys[0][1]), [])
                    if not live:
                        raise RuntimeError(f"no live function {q} in {path}")
                    for fn in live:
                        fn.__code__ = cands[0]
            elif kind == "fixture":
                overrides[os.path.join(self.dir, path)] = new
                self._readers[os.path.join(self.dir, path)] = set(funcs or ())
            elif kind == "test":
                reexec.append((path, new))  # new is None: unchanged dependent, re-run as is
        return overrides, reexec

    def _child(self, plan, nodes, limits, wfd):
        out = os.fdopen(wfd, "w", buffering=1)

        def emit(line):
            out.write(line + "\n")

        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            overrides, reexec = self._apply(plan)
            read = set()
            if overrides:
                real_open, real_io_open = builtins.open, io.open

                def fake_open(file, mode="r", *a, **k):
                    try:
                        p = os.path.abspath(os.fspath(file))
                    except TypeError:
                        p = None
                    if p in overrides:
                        read.add(p)
                        data = overrides[p]
                        if "b" in mode:
                            return io.BytesIO(data if isinstance(data, bytes) else data.encode())
                        text = data if isinstance(data, str) else data.decode("utf-8")
                        return io.StringIO(text, newline=k.get("newline"))
                    return real_open(file, mode, *a, **k)

                builtins.open = fake_open  # gzip.open and helpers look this up per call
                io.open = fake_open
            mods = dict(self.modules)
            # Re-execute patched test modules, then the test modules that import them
            # (reexec is ordered dependencies-first by the parent).
            for path, src in reexec:
                name = path[:-3].replace("/", ".")
                if src is None:
                    with open(os.path.join(self.dir, path), encoding="utf-8") as fh:
                        src = fh.read()
                m = types.ModuleType(name)
                m.__file__ = os.path.join(self.dir, path)
                m.__package__ = name.rpartition(".")[0]
                sys.modules[name] = m
                parent = sys.modules.get(m.__package__)
                if parent is not None:
                    setattr(parent, name.rpartition(".")[2], m)
                exec(compile(src, m.__file__, "exec"), m.__dict__)
                mods[path] = m
            run_suite(mods.get, nodes, limits, emit)
            for p in overrides:
                # A known reader of this fixture ran but never saw the patched text:
                # the override missed, so its "pass" means nothing.
                if p not in read and self._readers.get(p, set()) & set(nodes):
                    emit("X override never read: " + p)
            emit("DONE")
        except BaseException as e:  # noqa: BLE001
            emit(f"X {type(e).__name__}: {e}")
        finally:
            out.flush()
            os._exit(0)

    # ---------------------------------------------------------------- parent side

    @staticmethod
    def _order(nodes, first):
        # Tests in `first` run first; tests of one class stay together for setUpClass.
        return sorted(nodes, key=lambda n: (n not in first, n.rsplit("::", 1)[0], n))

    def run(self, plan, nodes, limits, budget, stop_on=frozenset()):
        """Hot-patch in a forked child. Returns (failed_nodes, unverified_nodes).
        If a test in stop_on fails, stop there and leave the rest unverified."""
        self.last_apply_error = False
        if not self.ok or not nodes:
            return set(), set(nodes)
        nodes = self._order(nodes, stop_on)
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            self._child(plan, nodes, limits, w)
        os.close(w)
        return self._collect(r, pid, nodes, budget, stop_on)

    def _shadow_write(self, files):
        shadow = os.path.join(os.environ.get("TMPDIR_BR", "/tmp"), "br_shadow")
        if not os.path.isdir(os.path.join(shadow, "sqlglot")):
            build_shadow(self.dir, shadow)
        touched = []
        try:
            for path, new in files:
                dst = os.path.join(shadow, path)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.lexists(dst):
                    os.unlink(dst)
                touched.append(path)
                if new is not None:
                    with open(dst, "w", encoding="utf-8") as fh:
                        fh.write(new)
        except OSError:
            self._shadow_restore(shadow, touched)
            raise
        return shadow, touched

    def _shadow_restore(self, shadow, touched):
        for path in touched:
            dst = os.path.join(shadow, path)
            if os.path.lexists(dst):
                os.unlink(dst)
            src = os.path.join(self.dir, path)
            if os.path.exists(src):
                os.symlink(src, dst)

    def _spawn(self, shadow, mode, w, job):
        import subprocess

        env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=shadow + os.pathsep + os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        proc = subprocess.Popen(
            [sys.executable, "-m", "blast.verify", mode, str(w)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            pass_fds=(w,), cwd=shadow, env=env,
        )
        os.close(w)
        proc.stdin.write(json.dumps(job).encode())
        proc.stdin.close()
        return proc

    def run_fresh(self, files, nodes, limits, budget, stop_on=frozenset()):
        """Write the patched files into a /tmp shadow tree and run the candidates in a
        fresh interpreter. files: [(path, new_text or None for deleted)]."""
        self.last_apply_error = False
        if not nodes:
            return set(), set()
        nodes = self._order(nodes, stop_on)
        try:
            shadow, touched = self._shadow_write(files)
        except OSError as e:
            log(f"verify: shadow tree failed: {e!r}")
            return set(), set(nodes)
        try:
            r, w = os.pipe()
            proc = self._spawn(shadow, "--child", w, {"nodes": nodes, "limits": limits})
            return self._collect(r, proc.pid, nodes, budget, stop_on)
        finally:
            self._shadow_restore(shadow, touched)

    def import_diff(self, files, timeout=20.0):
        """Keys (see blast.worlddiff) whose value after loading everything differs once
        `files` are patched, or None if that can't be told."""
        from blast import worlddiff

        if not self.ok or os.environ.get("PYTHONHASHSEED") is None:
            return None  # set iteration order must match between parent and child
        t0 = time.process_time()
        if getattr(self, "_world", None) is None:
            self._world = worlddiff.fingerprints(self.dir)
        try:
            shadow, touched = self._shadow_write(files)
        except OSError as e:
            log(f"verify: shadow tree failed: {e!r}")
            return None
        data = b""
        try:
            r, w = os.pipe()
            proc = self._spawn(shadow, "--diff", w, {"test_files": sorted(self.modules_files), "root": shadow})
            deadline = time.time() + timeout
            while True:
                left = deadline - time.time()
                if left <= 0 or not select.select([r], [], [], left)[0]:
                    proc.kill()
                    break
                chunk = os.read(r, 1 << 16)
                if not chunk:
                    break
                data += chunk
            proc.wait()
            os.close(r)
        finally:
            self._shadow_restore(shadow, touched)
        try:
            other = json.loads(data.decode())
        except ValueError:
            log("verify: import diff child failed")
            return None
        if not isinstance(other, dict):
            log(f"verify: import diff child error {other!r}")
            return None
        keys = worlddiff.diff(self._world, other)
        log(f"verify: import diff {len(keys)} keys in {time.process_time() - t0:.2f}s cpu (parent)")
        return keys

    def _collect(self, r, pid, nodes, budget, stop_on=frozenset()):
        failed, passed = set(), set()
        class_errors = []
        buf = b""
        deadline = time.monotonic() + budget
        aborted = None
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                aborted = "budget"
                break
            ready, _, _ = select.select([r], [], [], left)
            if not ready:
                continue
            chunk = os.read(r, 65536)
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                line = raw.decode("utf-8", "replace")
                tag, _, rest = line.partition(" ")
                if tag == "F":
                    failed.add(rest)
                    if rest in stop_on:
                        aborted = "stop"
                elif tag == "P":
                    passed.add(rest)
                elif tag == "E":
                    class_errors.append(rest)
                elif tag == "X":
                    aborted = rest
                elif tag == "DONE":
                    pass
            if aborted == "stop":
                break
        if aborted:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.close(r)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        if class_errors:
            # A setUpClass error fails every test of that class: be conservative.
            log(f"verify: class fixture errors {class_errors[:2]}")
            failed |= set(nodes) - passed
        if aborted and aborted not in ("stop", "budget"):
            passed = set()  # the child hit an error: none of its passes can be trusted
        unverified = set(nodes) - failed - passed
        if aborted and aborted != "stop":
            log(f"verify: aborted ({aborted}); {len(unverified)} unverified")
        self.last_apply_error = (
            bool(aborted) and not passed and not failed and aborted not in ("budget", "stop")
        )
        return failed, unverified


def diff_child_main(fd):
    """Entry point of an import-diff process: load everything (patched) and fingerprint it."""
    out = os.fdopen(fd, "w")
    try:
        from blast import worlddiff

        job = json.loads(sys.stdin.read())
        os.environ["SKIP_INTEGRATION"] = "0"
        root = job["root"]  # as spelled on sys.path (getcwd would resolve /tmp symlinks)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        worlddiff.load_world(root, job["test_files"])
        out.write(json.dumps(worlddiff.fingerprints(root)))
    except BaseException as e:  # noqa: BLE001
        out.write(json.dumps(f"{type(e).__name__}: {e}"))
    finally:
        out.flush()
        os._exit(0)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        fresh_child_main(int(sys.argv[2]))
    elif len(sys.argv) == 3 and sys.argv[1] == "--diff":
        diff_child_main(int(sys.argv[2]))
