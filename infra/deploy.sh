#!/bin/bash
# Build the app, ship it to the server, and (re)start it behind Caddy HTTPS.
# Usage: infra/deploy.sh do        (after: infra/tf.sh do apply)
set -euo pipefail
PROVIDER=${1:-do}
cd "$(dirname "$0")/.."
TF="infra/tf.sh $PROVIDER"
IP=$($TF output -raw public_ip)
HOST=$($TF output -raw hostname)
USER_NAME=$($TF output -raw ssh_user)
KEY=infra/idlegrid_deploy_key
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 $USER_NAME@$IP"

echo "Waiting for $IP to finish first-boot setup ..."
for i in $(seq 1 60); do
  $SSH test -f /var/lib/cloud/idlegrid-ready 2>/dev/null && break
  sleep 10
done
$SSH test -f /var/lib/cloud/idlegrid-ready

echo "Building wheel ..."
rm -rf dist && uv build --wheel -q --out-dir dist
WHEEL=$(ls dist/*.whl)

echo "Shipping $WHEEL ..."
scp -i "$KEY" -q "$WHEEL" infra/idlegrid.service "$USER_NAME@$IP:/tmp/"
$SSH "sudo bash -s" <<EOF
set -euo pipefail
[ -d /opt/idlegrid/venv ] || python3.12 -m venv /opt/idlegrid/venv
/opt/idlegrid/venv/bin/pip install -q --upgrade pip
/opt/idlegrid/venv/bin/pip install -q --force-reinstall --no-deps /tmp/$(basename "$WHEEL")
/opt/idlegrid/venv/bin/pip install -q /tmp/$(basename "$WHEEL")
install -m 644 /tmp/idlegrid.service /etc/systemd/system/idlegrid.service
cat > /etc/caddy/Caddyfile <<CADDY
$HOST {
  encode gzip
  reverse_proxy 127.0.0.1:8000 {
    flush_interval -1
  }
}
CADDY
systemctl daemon-reload
systemctl enable --now idlegrid
systemctl restart idlegrid
systemctl reload caddy || systemctl restart caddy
EOF

echo "Waiting for HTTPS certificate ..."
for i in $(seq 1 30); do
  curl -sf "https://$HOST/healthz" >/dev/null && break
  sleep 5
done
curl -sf "https://$HOST/healthz" && echo && echo "Live at https://$HOST"
