#!/usr/bin/env bash
# Deploy Macro Context Monitor to the desktop under the internal-monitor-service nologin service user.
#
# Mirrors internal-corpus-service/scripts/deploy.sh and internal-research-service's service-user convention. Per
# docs/DECISIONS.md, the FR-17 paper.db read path is GROUP-READ (internal-monitor-service ∈ paper-readers reads
# /srv/paper-share/paper.db) — this script does NOT provision an SSH-forced-command key or a
# sudoers Cmnd_Alias; it verifies the group-read grant instead. Run as root (sudo).
#
# Usage: scripts/deploy.sh [--dry-run]
set -euo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

SERVICE_USER=internal-monitor-service
APP_DIR=/home/${SERVICE_USER}/app
SNAPSHOT=/srv/paper-share/paper.db
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "DRY-RUN: $*"
    else
        eval "$@"
    fi
}

echo "==> Deploying macro-monitor to ${APP_DIR} (service user: ${SERVICE_USER})"

# 1. Service user must already exist (provisioned once; see docs/DECISIONS.md). Verify, don't create.
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "ERROR: service user ${SERVICE_USER} does not exist — provision it before deploying." >&2
    exit 1
fi

# 2. FR-17 read-path check: internal-monitor-service must be in paper-readers and able to read the snapshot.
if ! id -nG "${SERVICE_USER}" | tr ' ' '\n' | grep -qx paper-readers; then
    echo "ERROR: ${SERVICE_USER} is not in the paper-readers group (FR-17 read path)." >&2
    exit 1
fi
if [[ $DRY_RUN -eq 0 ]]; then
    if ! sudo -u "${SERVICE_USER}" test -r "${SNAPSHOT}"; then
        echo "WARN: ${SERVICE_USER} cannot yet read ${SNAPSHOT} — is paper-db-snapshot.timer active?" >&2
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
