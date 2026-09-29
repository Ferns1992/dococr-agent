#!/bin/bash
set -euo pipefail

APP_DIR=/opt/docchat
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
VPS="${VPS:-~/.opencode/bin/vps}"

echo "==> syncing source to VPS:$APP_DIR"
tar -C "$SRC_DIR" -czf - backend frontend | "$VPS" "mkdir -p $APP_DIR && tar xzf - -C $APP_DIR"

echo "==> installing dependencies + service"
"$VPS" "bash -s" <<'REMOTE'
set -euo pipefail
APP_DIR=/opt/docchat
cd "$APP_DIR/backend"

if [ ! -d venv ]; then
  python3 -m venv venv
fi
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "created .env from example - add your keys"
fi

mkdir -p data/media

cat > /etc/systemd/system/docchat.service <<'UNIT'
[Unit]
Description=DocChat RAG frontend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/docchat/backend
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/docchat/backend/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8077 --workers 1
Restart=always
RestartSec=5
MemoryMax=600M

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable docchat >/dev/null
systemctl restart docchat
sleep 4
systemctl --no-pager --lines=0 status docchat || true
echo "--- health ---"
curl -s --max-time 10 http://127.0.0.1:8077/api/health || echo "no response yet"
REMOTE

echo
echo "==> done. status:  $VPS 'systemctl status docchat'"
