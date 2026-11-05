#!/bin/bash
# Run Terraform for one provider with its credentials loaded from your Mac.
# Usage: infra/tf.sh do plan      infra/tf.sh do apply      infra/tf.sh aws plan
set -euo pipefail
cd "$(dirname "$0")"
PROVIDER=${1:?usage: infra/tf.sh <do|aws> <terraform args...>}
shift
if [ "$PROVIDER" = "do" ]; then
  TOKEN_FILE="$HOME/.config/idlegrid/do_token"
  [ -s "$TOKEN_FILE" ] || { echo "Save your DigitalOcean token to $TOKEN_FILE first." >&2; exit 1; }
  export DIGITALOCEAN_TOKEN="$(cat "$TOKEN_FILE")"
fi
exec ./bin/terraform -chdir="$PROVIDER" "$@"
