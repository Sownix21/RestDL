#!/usr/bin/env bash
set -Eeuo pipefail

[[ ${EUID} -eq 0 ]] || exec sudo bash "$0" "$@"
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/restdl

command -v apt-get >/dev/null || { echo "This installer currently supports Debian/Ubuntu."; exit 1; }
apt-get update
apt-get install -y --no-install-recommends python3 python3-venv python3-pip git ca-certificates
id restdl >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/restdl --shell /usr/sbin/nologin restdl
if [[ ! -f "$SOURCE_DIR/main.py" ]]; then
  RESTDL_REPO="${RESTDL_REPO:-https://github.com/Sownix21/RestDL.git}"
  SOURCE_DIR="$(mktemp -d)"
  git clone --depth 1 "$RESTDL_REPO" "$SOURCE_DIR"
fi
install -d -m 755 "$APP_DIR" /var/log/restdl
install -d -m 750 -o restdl -g restdl /var/lib/restdl "$APP_DIR/downloads" "$APP_DIR/logs"
tar -C "$SOURCE_DIR" \
  --exclude='./.env' --exclude='./.venv' --exclude='./__pycache__' \
  --exclude='./database' --exclude='./downloads' --exclude='./logs' \
  -cf - . | tar --no-overwrite-dir -C "$APP_DIR" -xf -
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip wheel
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
install -m 644 "$APP_DIR/deploy/restdl.service" /etc/systemd/system/restdl.service
install -m 755 "$APP_DIR/deploy/restdl-cli" /usr/local/bin/restdl
chown -R root:restdl "$APP_DIR"
chmod -R go-w "$APP_DIR"
chmod 755 "$APP_DIR"
chmod -R g+rX "$APP_DIR"
chown -R restdl:restdl "$APP_DIR/downloads" "$APP_DIR/logs" /var/lib/restdl /var/log/restdl
chmod 750 "$APP_DIR/downloads" "$APP_DIR/logs" /var/lib/restdl /var/log/restdl
systemctl daemon-reload
systemctl enable restdl.service
echo "Installation complete. Starting interactive configuration..."
/usr/local/bin/restdl configure
systemctl restart restdl.service
echo "Use 'restdl' anywhere to open the management menu."
