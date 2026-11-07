#!/bin/bash
# IdleGrid node installer for Apple Silicon Macs.
#
#   curl -fsSL __IDLEGRID_SERVER__/install.sh | bash -s -- join_XXXX
#
# Options after the token:
#   --no-service          don't run in the background / at login
#   --allow-battery       keep working on battery power
#   --pause-when-active   pause while you use the computer
#   --hours 23-7          only work in this time window
#   --models a,b          only serve these models
#
# Everything goes into ~/.idlegrid (its own Python, no sudo, nothing system-wide).
# Uninstall: launchctl bootout gui/$(id -u)/ai.idlegrid.node; rm -rf ~/.idlegrid ~/Library/LaunchAgents/ai.idlegrid.node.plist
set -euo pipefail

SERVER="${IDLEGRID_SERVER:-__IDLEGRID_SERVER__}"
PACKAGE="${IDLEGRID_PACKAGE:-__IDLEGRID_PACKAGE__}"
IG_HOME="${IDLEGRID_HOME:-$HOME/.idlegrid}"
LABEL="${IDLEGRID_SERVICE_LABEL:-ai.idlegrid.node}"
TOKEN=""
SERVICE=1
START_ARGS=()

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --no-service) SERVICE=0 ;;
    --allow-battery|--pause-when-active|--no-auto-download) START_ARGS+=("$1") ;;
    --hours|--models) [ $# -ge 2 ] || die "$1 needs a value"; START_ARGS+=("$1" "$2"); shift ;;
    join_*) TOKEN="$1" ;;
    *) die "Unknown option: $1" ;;
  esac
  shift
done

# 1. Is this Mac eligible?
[ "$(uname -s)" = "Darwin" ] || die "This installer is for macOS. Linux/NVIDIA support is coming soon."
[ "$(uname -m)" = "arm64" ] || die "IdleGrid needs an Apple Silicon Mac (M1 or newer)."
MEM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
[ "$MEM_GB" -ge 8 ] || die "At least 8 GB of memory is needed (this Mac has ${MEM_GB} GB)."
say "$(sysctl -n machdep.cpu.brand_string) with ${MEM_GB} GB memory: eligible"
if [ ! -f "$IG_HOME/node.json" ] && [ -z "$TOKEN" ]; then
  die "Missing join token. Get one at $SERVER (My machines > Add a machine)."
fi

# 2. A private Python just for IdleGrid.
mkdir -p "$IG_HOME/bin"
export UV_PYTHON_INSTALL_DIR="$IG_HOME/python" UV_CACHE_DIR="$IG_HOME/cache"
if [ ! -x "$IG_HOME/bin/uv" ]; then
  say "Installing uv (Python manager) into $IG_HOME/bin"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$IG_HOME/bin" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null
fi
UV="$IG_HOME/bin/uv"
say "Setting up Python 3.12"
"$UV" venv -q --allow-existing --python 3.12 "$IG_HOME/venv"

# 3. The node agent and the MLX runtime.
say "Installing the IdleGrid node agent and MLX (a minute or two)"
"$UV" pip install -q --python "$IG_HOME/venv/bin/python" --reinstall-package idlegrid "idlegrid[mlx] @ $PACKAGE"
ln -sf "$IG_HOME/venv/bin/idlegrid-node" "$IG_HOME/bin/idlegrid-node"
NODE="$IG_HOME/bin/idlegrid-node"
export IDLEGRID_HOME="$IG_HOME"
if [ -z "${IDLEGRID_NO_MODIFY_PATH:-}" ] && [ "$IG_HOME" = "$HOME/.idlegrid" ] && ! grep -qs '.idlegrid/bin' "$HOME/.zshrc"; then
  echo 'export PATH="$HOME/.idlegrid/bin:$PATH"  # added by the IdleGrid installer' >> "$HOME/.zshrc"
fi

# 4. Link this Mac to the account that created the token.
if [ -f "$IG_HOME/node.json" ]; then
  say "This Mac is already registered; keeping its identity"
else
  "$NODE" register --server "$SERVER" --join-token "$TOKEN"
fi

# 5. A small starter model so the network can verify this Mac straight away.
say "Downloading a starter model (Qwen2.5 0.5B, about 300 MB)"
"$NODE" prepare qwen2.5-0.5b >/dev/null 2>&1 || "$NODE" prepare qwen2.5-0.5b

# 6. Run in the background and at every login.
if [ "$SERVICE" = "1" ]; then
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
  ARGS_XML="<string>$(xml "$NODE")</string><string>start</string>"
  for a in "${START_ARGS[@]+"${START_ARGS[@]}"}"; do ARGS_XML="$ARGS_XML<string>$(xml "$a")</string>"; done
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>$ARGS_XML</array>
  <key>EnvironmentVariables</key><dict><key>IDLEGRID_HOME</key><string>$(xml "$IG_HOME")</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>Nice</key><integer>5</integer>
  <key>StandardOutPath</key><string>$(xml "$IG_HOME")/node.log</string>
  <key>StandardErrorPath</key><string>$(xml "$IG_HOME")/node.log</string>
</dict></plist>
PLIST
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  say "IdleGrid is running in the background and starts at every login"
  echo "    Logs:       tail -f $IG_HOME/node.log"
  echo "    Stop:       launchctl bootout gui/\$(id -u)/$LABEL"
  echo "    Uninstall:  launchctl bootout gui/\$(id -u)/$LABEL; rm -rf $IG_HOME $PLIST"
else
  say "Installed. Start serving with:  $NODE start ${START_ARGS[*]+"${START_ARGS[*]}"}"
fi

if pmset -g batt 2>/dev/null | grep -q "Battery Power" && [[ " ${START_ARGS[*]+"${START_ARGS[*]}"} " != *" --allow-battery "* ]]; then
  echo "    Note: this Mac is on battery, so it waits until you plug in (add --allow-battery to change that)."
fi
echo "    Bigger models earn more: $NODE prepare recommended"
echo "    See your machine at $SERVER (My machines)"
