#!/usr/bin/env bash
# Local test runner: ruff check, ruff format --check, pytest, bash -n, shellcheck.
# Tools that are not installed are reported as skipped; CI runs all of them.
#
# Interpreter selection: $PYTHON, then .venv/bin/python, then python3.
# Create a venv with:  python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
set -u
cd "$(dirname "$0")/.." || exit 2

pick_python() {
  local candidates=() c
  if [ -n "${PYTHON:-}" ]; then candidates+=("$PYTHON"); fi
  candidates+=(".venv/bin/python" "python3" "python")
  for c in "${candidates[@]}"; do
    if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then
      if "$c" -c "import pytest, yaml, requests" >/dev/null 2>&1; then
        echo "$c"
        return 0
      fi
    fi
  done
  return 1
}

PY=$(pick_python) || {
  echo "no Python with pytest, pyyaml and requests found."
  echo "run: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'"
  exit 2
}
echo "python: $PY ($("$PY" --version 2>&1))"

status=0
results=()

run_step() {
  local name=$1
  shift
  echo "== $name"
  if "$@"; then
    results+=("ok       $name")
  else
    results+=("FAILED   $name")
    status=1
  fi
}

skip_step() {
  echo "== $1: skipped: $2 not installed"
  results+=("skipped  $1 ($2 not installed)")
}

if "$PY" -m ruff --version >/dev/null 2>&1; then
  run_step "ruff check" "$PY" -m ruff check .
  run_step "ruff format --check" "$PY" -m ruff format --check .
else
  skip_step "ruff check" ruff
  skip_step "ruff format --check" ruff
fi

run_step "pytest" "$PY" -m pytest -q

mapfile -t shell_scripts < <(find . -name '*.sh' -not -path './.git/*' -not -path './.venv/*' | sort)
syntax_ok=0
for f in "${shell_scripts[@]}"; do
  bash -n "$f" || syntax_ok=1
done
run_step "bash -n (${#shell_scripts[@]} script(s))" test "$syntax_ok" -eq 0

if command -v shellcheck >/dev/null 2>&1; then
  run_step "shellcheck" shellcheck "${shell_scripts[@]}"
else
  skip_step "shellcheck" shellcheck
fi

echo
echo "== summary"
printf '%s\n' "${results[@]}"
if [ "$status" -eq 0 ]; then
  echo "result: all steps passed"
else
  echo "result: some steps failed"
fi
exit "$status"
