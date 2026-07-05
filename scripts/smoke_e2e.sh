#!/usr/bin/env bash
# Real-snapshot end-to-end smoke, run AS the internal-monitor-service service user (the only identity that can
# read /srv/paper-share/paper.db, per FR-17/docs/DECISIONS.md). This is the leg the pytest suite
# cannot exercise as an ordinary user; run it once after deploy to confirm the live read path.
#
# Usage (on the desktop):  sudo -u internal-monitor-service bash scripts/smoke_e2e.sh
set -euo pipefail

APP=/home/internal-monitor-service/app
MM="${APP}/.venv/bin/macro-monitor --config ${APP}/config.yaml"
TODAY="$(date -u +%Y-%m-%d)"

echo "== version =="
${MM} --version

echo "== collect-rss (real feeds, no LLM) =="
${MM} collect-rss || true   # exit 1 only if ALL feeds fail; don't abort the smoke on a flaky feed

echo "== correlate ${TODAY} against the real /srv/paper-share/paper.db snapshot =="
${MM} correlate --date "${TODAY}"

echo "== OK: live read path works =="
