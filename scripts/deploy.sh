#!/usr/bin/env bash
# Deploy macro-monitor under a dedicated nologin service user.
#
# The paper.db read path is GROUP-READ by convention: the service user reads a periodically
# refreshed, read-only snapshot via membership in a dedicated read-only group -- this script does
# NOT provision an SSH-forced-command key or a sudoers Cmnd_Alias; it verifies the group-read
# grant instead. See ops/ for one worked example of producing that snapshot. Run as root (sudo).
#
# Configure via env vars (all optional, sensible defaults shown):
#   MACRO_MONITOR_SERVICE_USER   (default: macro-monitor)
#   MACRO_MONITOR_APP_DIR        (default: /home/${SERVICE_USER}/app)
#   MACRO_MONITOR_SNAPSHOT_PATH  (default: /var/lib/macro-monitor/paper.db)
#   MACRO_MONITOR_SNAPSHOT_GROUP (default: paper-readers)
#
# Usage: scripts/deploy.sh [--dry-run]
set -euo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

SERVICE_USER="${MACRO_MONITOR_SERVICE_USER:-macro-monitor}"
APP_DIR="${MACRO_MONITOR_APP_DIR:-/home/${SERVICE_USER}/app}"
SNAPSHOT="${MACRO_MONITOR_SNAPSHOT_PATH:-/var/lib/macro-monitor/paper.db}"
SNAPSHOT_GROUP="${MACRO_MONITOR_SNAPSHOT_GROUP:-paper-readers}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "DRY-RUN: $*"
    else
        eval "$@"
    fi
}

# ---- deploy-time uncommitted-file gate -------------------------------------
# Refuses to ship a file this deploy would push if it is not committed here (scoped to exactly
# what step 3/5 below actually ship -- src, pyproject.toml, README.md, config.example.yaml, and
# the two systemd unit files -- NOT the whole repo: this deploy does not rsync tests/, etc).
# Local git only. Escape hatch: DEPLOY_GIT_GATE=skip (loud banner, not silent).
SHIPPED_PATHS=(
  config.example.yaml
  pyproject.toml
  README.md
  scripts/deploy.sh
  src
  systemd/macro-monitor-collect.service
  systemd/macro-monitor-collect.timer
)
if [ "${DEPLOY_GIT_GATE:-}" = "skip" ]; then
  cat >&2 <<'BANNER'
############################################################################
# DEPLOY_GIT_GATE=skip -- uncommitted-file check BYPASSED.
# This deploy may ship files that exist nowhere but this machine's disk --
# including another session's unfinished work, if one is active in this repo.
############################################################################
BANNER
else
  echo "==> Preflight: checking this deploy's files are committed"
  set +e
  "${REPO_ROOT}/scripts/check_deploy_clean.py" "${SHIPPED_PATHS[@]}"
  GATE_RC=$?
  set -e
  case "$GATE_RC" in
    0) : ;;
    1)
      cat >&2 <<'BLOCKEDMSG'

DEPLOY BLOCKED: files this deploy would ship are not committed in this repo (see above).

  1. Review:  git status --porcelain -- <path>
  2. Commit them (or leave them if another session is mid-work), then re-run.

To ship deliberately: DEPLOY_GIT_GATE=skip $0
BLOCKEDMSG
      exit 1
      ;;
    *)
      echo "check_deploy_clean.py: could not determine whether shipped files are committed (exit ${GATE_RC})." >&2
      echo "Refusing to deploy blind. Fix the check, or set DEPLOY_GIT_GATE=skip if you accept the risk." >&2
      exit 2
      ;;
  esac
fi
# -----------------------------------------------------------------------------

echo "==> Deploying macro-monitor to ${APP_DIR} (service user: ${SERVICE_USER})"

# 1. Service user must already exist (provisioned once, out of scope for this script). Verify, don't create.
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "ERROR: service user ${SERVICE_USER} does not exist -- provision it before deploying." >&2
    exit 1
fi

# 2. Read-path check: the service user must be in the snapshot's read-only group.
if ! id -nG "${SERVICE_USER}" | tr ' ' '\n' | grep -qx "${SNAPSHOT_GROUP}"; then
    echo "ERROR: ${SERVICE_USER} is not in the ${SNAPSHOT_GROUP} group (paper.db read path)." >&2
    exit 1
fi
if [[ $DRY_RUN -eq 0 ]]; then
    if ! sudo -u "${SERVICE_USER}" test -r "${SNAPSHOT}"; then
        echo "WARN: ${SERVICE_USER} cannot yet read ${SNAPSHOT} -- is your snapshot-refresh timer active?" >&2
    fi
fi

# 3. Sync code (src, pyproject, config example, systemd units).
run "install -d -o ${SERVICE_USER} -g ${SERVICE_USER} ${APP_DIR} ${APP_DIR}/data ${APP_DIR}/reports"
run "rsync -a --delete '${REPO_ROOT}/src' '${REPO_ROOT}/pyproject.toml' '${REPO_ROOT}/README.md' ${APP_DIR}/"
run "test -f ${APP_DIR}/config.yaml || install -o ${SERVICE_USER} -g ${SERVICE_USER} -m 640 '${REPO_ROOT}/config.example.yaml' ${APP_DIR}/config.yaml"
run "chown -R ${SERVICE_USER}:${SERVICE_USER} ${APP_DIR}"

# 4. Create/refresh the service-user venv and install the package.
run "sudo -u ${SERVICE_USER} python3 -m venv ${APP_DIR}/.venv"
run "sudo -u ${SERVICE_USER} ${APP_DIR}/.venv/bin/pip install --upgrade pip"
run "sudo -u ${SERVICE_USER} ${APP_DIR}/.venv/bin/pip install ${APP_DIR}"

# 5. Install + enable systemd timer.
run "install -m 644 '${REPO_ROOT}/systemd/macro-monitor-collect.service' /etc/systemd/system/"
run "install -m 644 '${REPO_ROOT}/systemd/macro-monitor-collect.timer' /etc/systemd/system/"
run "systemctl daemon-reload"
run "systemctl enable --now macro-monitor-collect.timer"

# 6. Smoke: version + a dry collection run as the service user.
run "sudo -u ${SERVICE_USER} ${APP_DIR}/.venv/bin/macro-monitor --version"

echo "==> Done. Check: systemctl status macro-monitor-collect.timer"
