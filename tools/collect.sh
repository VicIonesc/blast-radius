#!/usr/bin/env bash
# Rebuild map/covmap.json and map/meta.json from a sqlglot checkout at the pinned commit.
# Usage: tools/collect.sh [sqlglot_dir] [python]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SQLGLOT="$(cd "${1:-$ROOT/vendor/sqlglot}" && pwd)"
PY="${2:-$ROOT/.venv/bin/python}"
BUILD="$ROOT/build"
mkdir -p "$BUILD" "$ROOT/map"
rm -f "$BUILD"/.coverage* "$BUILD"/files.*.json "$BUILD"/classes.*.json

cat > "$BUILD/coveragerc" <<EOF
[run]
source = sqlglot,tests
omit = tests/fixtures/*
relative_files = true
data_file = $BUILD/.coverage
parallel = true
EOF

cd "$SQLGLOT"
export PATH="$(dirname "$PY"):$PATH"
COMMON=(-q -p no:cacheprovider -n auto --ignore=tests/test_sqlglotc.py tests)

echo "1/3 coverage run" >&2
PYTHONPATH="$ROOT" "$PY" -m pytest "${COMMON[@]}" -p tools.inline_pools_plugin --cov --cov-config="$BUILD/coveragerc" --cov-context=test --cov-report= | tail -1 || true

echo "2/3 timing run" >&2
"$PY" -m pytest "${COMMON[@]}" --junitxml="$BUILD/junit.xml" | tail -1 || true

echo "3/3 file-read + class trace run" >&2
PYTHONPATH="$ROOT" TRACE_FILES_OUT="$BUILD/files" TRACE_CLASSES_OUT="$BUILD/classes" \
  "$PY" -m pytest "${COMMON[@]}" -p tools.inline_pools_plugin -p tools.trace_files_plugin -p tools.trace_classes_plugin | tail -1 || true
"$PY" - "$BUILD" <<'EOF'
import glob, json, sys
b = sys.argv[1]
for kind in ("files", "classes"):
    m = {}
    for f in glob.glob(f"{b}/{kind}.*.json"):
        m.update(json.load(open(f)))
    json.dump(m, open(f"{b}/{kind}_by_test.json", "w"))
EOF

cd "$ROOT"
"$PY" tools/build_map.py "$BUILD/.coverage" "$BUILD/junit.xml" map/covmap.json
"$PY" tools/build_meta.py "$SQLGLOT" map/covmap.json "$BUILD/files_by_test.json" map/meta.json "$BUILD/classes_by_test.json"
