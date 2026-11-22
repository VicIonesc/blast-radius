# Blast Radius — happyDev

Pick the sqlglot tests a patch can break, split them over ≤ 8 workers.
Score = busiest worker's test seconds + 2 s/worker + 300 s per commit that misses a failing test + our CPU.

## Layout

| Path | What |
|---|---|
| `blast/select.py` | The selector that runs in the image. Reads `/data/commits/*.patch` and `/data/tests.json`, prints one JSON line per commit. |
| `map/covmap.json` | Per-test line coverage of sqlglot (and `tests/`) at `eb11aec`: line → set of tests, plus per-test runtime. |
| `map/meta.json` | Dialect families, test function spans, fixture → tests, class → tests that instantiate it. |
| `tools/collect.sh` | Rebuilds both maps (about 3 minutes on a laptop). |
| `tools/mutate.py` | Makes synthetic bug patches (operator flips, deleted statements, dict entries, fixture edits…). |
| `tools/truth.py` | Runs the real suite on each synthetic patch to get the failing tests. |
| `tools/evaluate.py` | Scores the selector against that ground truth (misses and cost). |
| `Dockerfile` | Submission image: sqlglot source at the pinned commit + selector + maps. |

## Setup

```bash
python3.13 -m venv .venv
./.venv/bin/pip install pytest pytest-xdist pytest-cov pytest-timeout coverage duckdb pandas python-dateutil pytz typing_extensions
mkdir -p vendor && git clone https://github.com/tobymao/sqlglot vendor/sqlglot && git -C vendor/sqlglot checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
tools/collect.sh
```

## Loop

```bash
./.venv/bin/python tools/mutate.py vendor/sqlglot build/mutants 40 1   # new seed = new patches
./.venv/bin/python tools/truth.py vendor/sqlglot build/mutants 12      # slow: full suite per patch
./.venv/bin/python tools/evaluate.py build/mutants                     # -v for every patch
DATA_DIR=path/to/sample ./.venv/bin/python -m blast.select             # run like the grader
```

## How a patch is mapped to tests

1. Non-code files → nothing. A `.py` change whose tokens are unchanged (comments and blank lines only) → nothing.
2. Patched file doesn't compile → every test (or every test in that test file and the files importing it).
3. A line inside a function body → tests whose coverage includes the innermost statement containing it. Insertions → tests that reached the enclosing block.
4. A function's `def`, decorator or signature → every test that ran its body.
5. A class body (attributes, dicts like `TRANSFORMS`) of an expression, parser, generator or tokenizer class → tests that instantiated that class or a subclass, plus tests whose source names a string the patch adds or removes.
6. Other module-level or class-level code: in a dialect file → every test touching that dialect family (including subclass dialects); in core → every test.
7. `tests/fixtures/...` → tests traced reading that file, plus tests whose class/module setup names it.
8. A test function body → that test; other test-file code → the whole file and the files importing it.
