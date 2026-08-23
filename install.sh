#!/usr/bin/env bash
set -Eeuo pipefail

[[ ${EUID} -eq 0 ]] || exec sudo bash "$0" "$@"

APP_DIR=/opt/dloader
DATA_DIR=/var/lib/dloader
LOG_DIR=/var/log/dloader
CONFIG_DIR=/etc/dloader
ENV_FILE="$CONFIG_DIR/dloader.env"
REPO_URL="${DLOADER_REPO:-https://github.com/Sownix21/RestDL.git}"
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
STAGE_DIR=""
BACKUP_DIR=""
WAS_ACTIVE=0
SWAPPED=0
INSTALL_SUCCEEDED=0
LEGACY_NAME="rest""dl"
LEGACY_WAS_ACTIVE=0
SOURCE_IS_TEMP=0

set_config() {
  local key="$1" value="$2" temporary
  temporary="$(mktemp)"
  if [[ -f "$ENV_FILE" ]]; then grep -v "^${key}=" "$ENV_FILE" > "$temporary" || true; fi
  printf '%s=%s\n' "$key" "$value" >> "$temporary"
  install -m 600 -o root -g dloader "$temporary" "$ENV_FILE"
  rm -f -- "$temporary"
}

rollback() {
  local exit_code=$?
  if [[ "$INSTALL_SUCCEEDED" -eq 0 ]]; then
    echo "DLoader installation failed; rolling back the application release." >&2
    systemctl stop dloader.service 2>/dev/null || true
    if [[ "$SWAPPED" -eq 1 ]]; then
      if [[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" ]]; then
        rm -rf -- "$APP_DIR"
        mv -- "$BACKUP_DIR" "$APP_DIR"
      else
        echo "No prior release exists; keeping the application so 'dloader configure' can repair first-time settings." >&2
      fi
    fi
    if [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]]; then rm -rf -- "$STAGE_DIR" || true; fi
    if [[ "$SOURCE_IS_TEMP" -eq 1 && -d "$SOURCE_DIR" ]]; then rm -rf -- "$SOURCE_DIR" || true; fi
    systemctl daemon-reload 2>/dev/null || true
    if [[ "$WAS_ACTIVE" -eq 1 ]]; then systemctl start dloader.service 2>/dev/null || true; fi
    if [[ "$LEGACY_WAS_ACTIVE" -eq 1 ]]; then systemctl start "${LEGACY_NAME}.service" 2>/dev/null || true; fi
  fi
  exit "$exit_code"
}
trap rollback EXIT

command -v apt-get >/dev/null || { echo "This installer supports Debian and Ubuntu."; exit 1; }
apt-get update
apt-get install -y --no-install-recommends python3 python3-venv python3-pip git ca-certificates
id dloader >/dev/null 2>&1 || useradd --system --create-home --home-dir "$DATA_DIR" --shell /usr/sbin/nologin dloader
install -d -m 750 -o dloader -g dloader "$DATA_DIR" "$DATA_DIR/downloads" "$LOG_DIR"
install -d -m 750 -o root -g dloader "$CONFIG_DIR"

if systemctl is-active --quiet dloader.service 2>/dev/null; then WAS_ACTIVE=1; fi
if systemctl is-active --quiet "${LEGACY_NAME}.service" 2>/dev/null; then LEGACY_WAS_ACTIVE=1; fi

LEGACY_CONFIG_DIR="/etc/${LEGACY_NAME}"
LEGACY_DATA_DIR="/var/lib/${LEGACY_NAME}"
LEGACY_ENV_FILE="${LEGACY_CONFIG_DIR}/${LEGACY_NAME}.env"
if [[ -f "$LEGACY_ENV_FILE" && ! -f "$ENV_FILE" ]]; then
  install -m 600 -o root -g dloader "$LEGACY_ENV_FILE" "$ENV_FILE"
  sed -i -e "s#/opt/${LEGACY_NAME}#/opt/dloader#g" \
    -e "s#${LEGACY_DATA_DIR}#${DATA_DIR}#g" \
    -e "s#/var/log/${LEGACY_NAME}#${LOG_DIR}#g" "$ENV_FILE"
fi
if [[ -d "$LEGACY_DATA_DIR" ]]; then cp -a -n "$LEGACY_DATA_DIR/." "$DATA_DIR/"; fi

if [[ ! -f "$SOURCE_DIR/main.py" ]]; then
  SOURCE_DIR="$(mktemp -d /tmp/dloader-source.XXXXXX)"
  SOURCE_IS_TEMP=1
  git clone --depth 1 "$REPO_URL" "$SOURCE_DIR"
fi

# Build and validate the next release before stopping a working service.
STAGE_DIR="$(mktemp -d /opt/dloader.next.XXXXXX)"
tar -C "$SOURCE_DIR" \
  --exclude='./.git' --exclude='./.env' --exclude='./.venv' \
  --exclude='./__pycache__' --exclude='./database' \
  --exclude='./downloads' --exclude='./logs' \
  -cf - . | tar -C "$STAGE_DIR" -xf -
python3 -m venv "$STAGE_DIR/.venv"
"$STAGE_DIR/.venv/bin/pip" install --upgrade pip wheel
"$STAGE_DIR/.venv/bin/pip" install -r "$STAGE_DIR/requirements.txt"
"$STAGE_DIR/.venv/bin/python" -m compileall -q "$STAGE_DIR"
chown -R root:dloader "$STAGE_DIR"
chmod -R go-w "$STAGE_DIR"
chmod 755 "$STAGE_DIR"
chmod -R g+rX "$STAGE_DIR"
runuser -u dloader -- test -r "$STAGE_DIR/main.py"
runuser -u dloader -- test -x "$STAGE_DIR/.venv/bin/python"

systemctl stop dloader.service 2>/dev/null || true
if [[ -d "$APP_DIR" ]]; then
  BACKUP_DIR="/opt/dloader.previous.$(date +%Y%m%d%H%M%S)"
  mv -- "$APP_DIR" "$BACKUP_DIR"
fi
mv -- "$STAGE_DIR" "$APP_DIR"
STAGE_DIR=""
SWAPPED=1
install -m 644 "$APP_DIR/deploy/dloader.service" /etc/systemd/system/dloader.service
install -m 755 "$APP_DIR/deploy/dloader-cli" /usr/local/bin/dloader
systemctl daemon-reload
systemctl enable dloader.service

if [[ ! -s "$ENV_FILE" ]]; then
  echo "Starting the first-time administrator configuration."
  /usr/local/bin/dloader configure
fi
# Existing configurations predate the read-only release directory. Force all
# runtime writes into persistent service-owned paths during every update.
set_config DATA_DIR "$DATA_DIR"
set_config DOWNLOAD_DIR "$DATA_DIR/downloads"
set_config LOG_DIR "$LOG_DIR"
/usr/local/bin/dloader repair --no-restart
systemctl reset-failed dloader.service 2>/dev/null || true
systemctl restart dloader.service

healthy=0
for _ in {1..18}; do
  if runuser -u dloader -- "$APP_DIR/.venv/bin/python" "$APP_DIR/scripts/healthcheck.py" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 5
done
if [[ "$healthy" -ne 1 ]]; then
  systemctl --no-pager --full status dloader.service >&2 || true
  journalctl -u dloader.service -n 80 --no-pager >&2 || true
  exit 1
fi

systemctl disable --now "${LEGACY_NAME}.service" 2>/dev/null || true
rm -f -- "/etc/systemd/system/${LEGACY_NAME}.service" "/usr/local/bin/${LEGACY_NAME}"
systemctl daemon-reload
INSTALL_SUCCEEDED=1
trap - EXIT
if [[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" ]]; then rm -rf -- "$BACKUP_DIR" || true; fi
if [[ "$SOURCE_IS_TEMP" -eq 1 && -d "$SOURCE_DIR" ]]; then rm -rf -- "$SOURCE_DIR" || true; fi
echo "DLoader is installed and healthy. Type 'dloader' anywhere to manage it."
