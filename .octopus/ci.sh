# The pipeline of this repo for Octopus CI: team machines and the hosted fallback both run it.
# It is the same as .github/workflows/ci.yml minus the matrix: lint, then the unit tests.
set -eu

PY=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 \
    && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
[ -n "$PY" ] || { echo "no Python 3.10 or newer found"; exit 1; }

"$PY" -m venv .venv
if [ -f .venv/bin/activate ]; then . .venv/bin/activate; else . .venv/Scripts/activate; fi
pip install -q -e '.[dev]'
ruff check .
pytest -q
