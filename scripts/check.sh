#!/usr/bin/env bash
set -euo pipefail

python3 -m unittest discover -s tests -v
python3 -m compileall -q agent_recovery examples tests
printf 'checks: ok\n'
