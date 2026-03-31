#!/bin/bash
# ─── gitdrop deploy script ───────────────────────────────────
# Скопируй на сервер и запусти: bash deploy.sh
# Или одной командой с локальной машины:
#   scp gitdrop.py deploy.sh root@SERVER:~ && ssh root@SERVER 'bash deploy.sh'
# ──────────────────────────────────────────────────────────────

set -e

PORT=7070
TOKEN=$(openssl rand -hex 16)  # случайный токен, поменяй если хочешь

echo "── gitdrop deploy ──"

# 1. Python + pip
echo "[1/4] checking python..."
apt-get update -qq && apt-get install -y -qq python3 python3-pip > /dev/null 2>&1 || true
pip3 install flask --break-system-packages -q 2>/dev/null || pip3 install flask -q

# 2. Копируем файл
echo "[2/4] setting up gitdrop..."
mkdir -p /opt/gitdrop
cp gitdrop.py /opt/gitdrop/gitdrop.py
chmod +x /opt/gitdrop/gitdrop.py

# 3. systemd сервис
echo "[3/4] creating systemd service..."
cat > /etc/systemd/system/gitdrop.service << EOF
[Unit]
Description=gitdrop file server
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/gitdrop
ExecStart=/usr/bin/python3 /opt/gitdrop/gitdrop.py --port $PORT --token $TOKEN
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable gitdrop
systemctl restart gitdrop

# 4. Firewall
echo "[4/4] opening port $PORT..."
ufw allow $PORT/tcp 2>/dev/null || iptables -A INPUT -p tcp --dport $PORT -j ACCEPT 2>/dev/null || true

echo ""
echo "┌───────────────────────────────────────────────┐"
echo "│  gitdrop запущен!                             │"
echo "│                                               │"
echo "│  URL:   http://SERVER:$PORT              │"
echo "│  Token: $TOKEN  │"
echo "│                                               │"
echo "│  Сохрани токен! Без него не зайдёшь.          │"
echo "│                                               │"
echo "│  Команды:                                     │"
echo "│    systemctl status gitdrop  — статус          │"
echo "│    systemctl restart gitdrop — перезапуск      │"
echo "│    journalctl -u gitdrop -f  — логи            │"
echo "└───────────────────────────────────────────────┘"
