#!/bin/bash
# First-boot setup: packages, a locked-down service user, and directories.
# The app itself is shipped by deploy.sh.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3.12-venv caddy unattended-upgrades
useradd --system --home /var/lib/idlegrid --shell /usr/sbin/nologin idlegrid || true
mkdir -p /var/lib/idlegrid /opt/idlegrid
chown idlegrid:idlegrid /var/lib/idlegrid
# 1 GB swap so pip installs and traffic spikes don't OOM a 2 GB instance.
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
touch /var/lib/cloud/idlegrid-ready
