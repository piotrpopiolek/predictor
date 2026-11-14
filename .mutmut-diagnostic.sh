#!/bin/bash
set -euo pipefail
export TERM=dumb
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONUNBUFFERED=1
cd /app
mkdir -p /mutmut-out
exec > >(tee /mutmut-out/run.log) 2>&1

echo "===== PYTHON ====="
python -c 'import sys; print(sys.version)'

echo "===== INSTALL ====="
pip install 'mutmut==3.8.1'

echo "===== HELP ====="
mutmut --help > /mutmut-out/help.txt || true
mutmut results --help > /mutmut-out/help-results.txt || true
mutmut show --help > /mutmut-out/help-show.txt || true

cat > /app/setup.cfg << 'EOF'
[mutmut]
source_paths=
    src
only_mutate=
    src/predictor/services/advisor/domain.py
    src/predictor/services/advisor/llm/validate.py
    src/predictor/services/advisor/budget.py
    src/predictor/services/odds_parse.py
    src/predictor/client/retry.py
pytest_add_cli_args=
    -o
    addopts=--import-mode=importlib
    -p
    no:cov
    -p
    no:cacheprovider
    -p
    no:xdist
pytest_add_cli_args_test_selection=
    tests/unit/services/advisor/test_domain.py
    tests/unit/services/advisor/test_llm.py
    tests/unit/services/advisor/test_budget.py
    tests/unit/services/advisor/test_process_rules.py
    tests/unit/services/advisor/test_llm_note_rules.py
    tests/unit/services/ingest/test_odds_parse.py
    tests/unit/client/test_client.py
use_setproctitle=false
mutate_only_covered_lines=true
EOF

echo "===== BASELINE ====="
python -m pytest -o addopts='--import-mode=importlib' -p no:cov -p no:cacheprovider -p no:xdist \
  tests/unit/services/advisor/test_domain.py \
  tests/unit/services/advisor/test_llm.py \
  tests/unit/services/advisor/test_budget.py \
  tests/unit/services/advisor/test_process_rules.py \
  tests/unit/services/advisor/test_llm_note_rules.py \
  tests/unit/services/ingest/test_odds_parse.py \
  tests/unit/client/test_client.py \
  -q --tb=line
echo "BASELINE_OK"

echo "===== MUTMUT ====="
set +e
mutmut run
code=$?
set -e
echo "MUTMUT_EXIT:${code}"

mutmut results > /mutmut-out/results.txt || true
mutmut results --all true > /mutmut-out/results-all.txt || true
echo "===== RESULTS ====="
cat /mutmut-out/results.txt
echo "===== STATS ====="
mutmut export-cicd-stats | tee /mutmut-out/stats.json || true
python - << 'PY'
import pathlib
import re
import subprocess

results = pathlib.Path("/mutmut-out/results.txt").read_text(encoding="utf-8", errors="replace")
extra = pathlib.Path("/mutmut-out/results-all.txt")
blob = results + "\n" + (extra.read_text(encoding="utf-8", errors="replace") if extra.exists() else "")
names = []
for line in blob.splitlines():
    match = re.search(r"(\S+\.py:\S+)", line)
    if match and "survived" in line.lower():
        names.append(match.group(1))
if not names:
    names = re.findall(r"\S+\.py:\S+", results)
out = pathlib.Path("/mutmut-out/survived")
out.mkdir(exist_ok=True)
print(f"SHOWING {len(names)} mutants")
for name in names:
    proc = subprocess.run(["mutmut", "show", name], capture_output=True, text=True)
    safe = name.replace("/", "_").replace(":", "__")
    (out / f"{safe}.diff").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    print(f"===== {name} =====")
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr)
PY

if [ -d /app/mutants ]; then
  cp -a /app/mutants /mutmut-out/mutants
fi
echo "DONE"
