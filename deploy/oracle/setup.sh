#!/usr/bin/env bash
# Installs Flight Price Tracker on a fresh Ubuntu 24.04 server (built for Oracle Cloud Always Free).
#
# What it sets up:
#   - the app in /opt/flight-price-tracker, running as a background service
#   - HTTPS at https://<your-ip-with-dashes>.sslip.io (free certificate, renewed automatically)
#   - the password from FT_PASSWORD (required)
#   - auto-update: pulls new code from GitHub main every 10 minutes and restarts
#
# Usage (as root):
#   FT_PASSWORD='your password' bash setup.sh
# Safe to run again: it updates in place and keeps your password unless you pass a new one.

set -euo pipefail

REPO="${FT_REPO:-https://github.com/cchigu/flight-price-tracker.git}"
BRANCH="${FT_BRANCH:-main}"
APP_DIR=/opt/flight-price-tracker
APP_USER=flighttracker
ENV_FILE=/etc/flight-tracker.env
LOG=/var/log/flight-tracker-setup.log

exec > >(tee -a "$LOG") 2>&1
echo "=== Flight Price Tracker setup $(date -Is) ==="

if [[ $EUID -ne 0 ]]; then echo "Run as root (sudo)." >&2; exit 1; fi

# ---- password -------------------------------------------------------------------------
if [[ -z "${FT_PASSWORD:-}" ]] && [[ -f $ENV_FILE ]] && grep -q '^FT_PASSWORD=' $ENV_FILE; then
  FT_PASSWORD="$(grep '^FT_PASSWORD=' $ENV_FILE | head -1 | cut -d= -f2-)"
fi
if [[ -z "${FT_PASSWORD:-}" || "$FT_PASSWORD" == "choose-a-password" ]]; then
  echo "Set FT_PASSWORD to the password you want for the page." >&2; exit 1
fi

# ---- packages ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
for _ in 1 2 3 4 5; do  # the first boot sometimes still has apt locked
  apt-get update -y && break || sleep 20
done
apt-get install -y python3 python3-venv python3-pip git caddy curl iptables-persistent

# ---- public address ---------------------------------------------------------------------
PUBLIC_IP="${FT_PUBLIC_IP:-}"
for url in https://api.ipify.org https://ifconfig.me https://icanhazip.com; do
  [[ -n "$PUBLIC_IP" ]] && break
  PUBLIC_IP="$(curl -fsS --max-time 10 "$url" | tr -d '[:space:]' || true)"
done
if ! [[ "$PUBLIC_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Couldn't find this server's public IP. Rerun with FT_PUBLIC_IP=x.x.x.x" >&2; exit 1
fi
HOST="${PUBLIC_IP//./-}.sslip.io"
echo "Public address: https://$HOST"

# ---- app --------------------------------------------------------------------------------
id -u $APP_USER >/dev/null 2>&1 || useradd --system --home $APP_DIR --shell /usr/sbin/nologin $APP_USER
if [[ -d $APP_DIR/.git ]]; then
  git -C $APP_DIR fetch -q origin "$BRANCH" && git -C $APP_DIR reset -q --hard "origin/$BRANCH"
else
  rm -rf $APP_DIR && git clone -q --branch "$BRANCH" "$REPO" $APP_DIR
fi
python3 -m venv $APP_DIR/.venv
$APP_DIR/.venv/bin/pip install -q --upgrade pip
$APP_DIR/.venv/bin/pip install -q -r $APP_DIR/requirements.txt
mkdir -p /var/lib/flight-tracker
chown -R $APP_USER:$APP_USER $APP_DIR /var/lib/flight-tracker
git config --system --add safe.directory $APP_DIR

umask 077
cat > $ENV_FILE <<EOF
FT_PASSWORD=$FT_PASSWORD
FT_HOST=127.0.0.1
FT_PORT=8000
FT_SECURE_COOKIE=1
FT_PROVIDER=google-flights
FT_DATA_DIR=/var/lib/flight-tracker
FT_PUBLIC_URL=https://$HOST
EOF
chmod 600 $ENV_FILE
umask 022

cat > /etc/systemd/system/flight-tracker.service <<EOF
[Unit]
Description=Flight Price Tracker
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python -m flighttracker.web
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/flight-tracker
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# ---- auto-update from GitHub -------------------------------------------------------------
cat > /usr/local/bin/flight-tracker-update <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd $APP_DIR
git fetch -q origin $BRANCH
if [[ "\$(git rev-parse HEAD)" != "\$(git rev-parse origin/$BRANCH)" ]]; then
  git reset -q --hard origin/$BRANCH
  $APP_DIR/.venv/bin/pip install -q -r requirements.txt
  chown -R $APP_USER:$APP_USER $APP_DIR
  systemctl restart flight-tracker
  echo "Updated to \$(git log -1 --format='%h %s')"
fi
EOF
chmod 755 /usr/local/bin/flight-tracker-update

cat > /etc/systemd/system/flight-tracker-update.service <<'EOF'
[Unit]
Description=Pull Flight Price Tracker updates from GitHub
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/flight-tracker-update
EOF

cat > /etc/systemd/system/flight-tracker-update.timer <<'EOF'
[Unit]
Description=Check GitHub for Flight Price Tracker updates every 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
EOF

# ---- HTTPS ------------------------------------------------------------------------------
cat > /etc/caddy/Caddyfile <<EOF
$HOST {
	encode gzip
	reverse_proxy 127.0.0.1:8000
}

# Typing the bare IP address sends you to the HTTPS name.
http:// {
	redir https://$HOST{uri} permanent
}
EOF

# ---- firewall: Oracle's Ubuntu image only lets SSH in ----------------------------------
for port in 80 443; do
  iptables -C INPUT -p tcp --dport $port -m state --state NEW -j ACCEPT 2>/dev/null \
    || iptables -I INPUT 1 -p tcp --dport $port -m state --state NEW -j ACCEPT
done
netfilter-persistent save

# ---- start ------------------------------------------------------------------------------
systemctl daemon-reload
systemctl enable --now flight-tracker.service flight-tracker-update.timer
systemctl restart flight-tracker.service
systemctl enable caddy
systemctl restart caddy

for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break || sleep 2
done
curl -fsS http://127.0.0.1:8000/healthz >/dev/null && echo "App is running." || echo "App didn't start; see: journalctl -u flight-tracker"

cat > /etc/motd <<EOF

  Flight Price Tracker: https://$HOST
  Logs:    journalctl -u flight-tracker -f
  Update:  sudo flight-tracker-update      (also runs every 10 minutes)
  Setup log: $LOG

EOF
echo "=== Done. Open https://$HOST (the certificate can take a minute on first visit) ==="
