"""pytest plugin: record which non-.py files under tests/ each test opens.

Load with: -p tools.trace_files_plugin  (with this repo root on PYTHONPATH)
Writes JSON {nodeid: [relative paths]} to $TRACE_FILES_OUT (one file per xdist worker).
"""

import builtins
import io
import json
import os

import pytest

_current = [None]
_seen = {}
_root = [None]
_real_open = builtins.open
_real_io_open = io.open


def _record(path):
    node = _current[0]
    if node is None or not isinstance(path, (str, os.PathLike)):
        return
    p = os.path.abspath(os.fspath(path))
    root = _root[0]
    if root and p.startswith(root) and not p.endswith(".py"):
        _seen.setdefault(node, set()).add(os.path.relpath(p, root))


def _open(file, *a, **k):
    _record(file)
    return _real_open(file, *a, **k)


def _io_open(file, *a, **k):
    _record(file)
    return _real_io_open(file, *a, **k)


def pytest_configure(config):
    _root[0] = str(config.rootpath)
    builtins.open = _open
    io.open = _io_open


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _current[0] = item.nodeid
    yield
    _current[0] = None


def pytest_unconfigure(config):
    builtins.open = _real_open
    io.open = _real_io_open
    out = os.environ.get("TRACE_FILES_OUT")
    if out:
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        with _real_open(f"{out}.{worker}.json", "w") as fh:
            json.dump({k: sorted(v) for k, v in _seen.items()}, fh)
