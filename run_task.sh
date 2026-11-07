#!/usr/bin/env bash
#
# Unified host-side entry point for Power Attacker tasks.
#
# Usage:
#   ./run_task.sh <taskID>[,<taskID>...] [--case NAME] [--tasktime SECONDS]
#                          [--trace-path DIR] [--evaluation-path DIR]
#                          [--no-build] [--force-build] [--keep-containers]
#
#   --case NAME   Target power system: ieee14 (default), ieee39, ieee57,
#                 ieee118 (definitions in cases/<name>.json).
#
# Build behavior (mutually exclusive flags):
#   (default)     Build images only on first run; reuse them afterwards.
#   --no-build    Never build; reuse existing images (fails if missing).
#   --force-build Always rebuild all images before starting containers.
#
# Example:
#   export ANTHROPIC_AUTH_TOKEN='your-local-model-token'
#   export ANTHROPIC_BASE_URL='http://host.docker.internal:8000'
#   export ANTHROPIC_MODEL='your-local-model-name'
#   ./run_task.sh t-01 --case ieee39 --tasktime 600 \
#       --trace-path ./traces --evaluation-path ./evaluations

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "${SCRIPT_DIR}/host/task_manager.py" "$@"
