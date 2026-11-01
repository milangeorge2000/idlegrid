"""Create a local demo account and join tokens for demo nodes.

Writes data/demo_account.json (git-ignored, owner-only) with the demo login and
one-time join tokens. Usage: python scripts/demo_seed.py http://127.0.0.1:8000 [n_tokens]
"""

import json
import os
import secrets
import sys
from pathlib import Path

import httpx

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
n = int(sys.argv[2]) if len(sys.argv) > 2 else 2
path = Path("data/demo_account.json")
acct = json.loads(path.read_text()) if path.exists() else {
    "email": "demo@idlegrid.local", "password": secrets.token_urlsafe(12)}

r = httpx.post(f"{base}/api/auth/login", json={"email": acct["email"], "password": acct["password"]})
if r.status_code != 200:
    r = httpx.post(f"{base}/api/auth/register", json={"email": acct["email"], "password": acct["password"]})
r.raise_for_status()
headers = {"Authorization": "Bearer " + r.json()["token"]}
acct["join_tokens"] = [httpx.post(f"{base}/api/nodes/join-token", headers=headers).json()["join_token"]
                       for _ in range(n)]
path.parent.mkdir(exist_ok=True)
path.write_text(json.dumps(acct, indent=2))
os.chmod(path, 0o600)
print(f"Demo account ready ({acct['email']}); {n} join tokens written to {path}")
