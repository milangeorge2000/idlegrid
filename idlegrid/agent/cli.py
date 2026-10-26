"""idlegrid-node: share this computer with the IdleGrid network.

  idlegrid-node info
  idlegrid-node start --server https://... --join-token join_...     (first run)
  idlegrid-node start                                                 (afterwards)
"""

import argparse
import asyncio
import json
import logging
from pathlib import Path

from . import hardware
from .agent import Agent, register
from .backends import MLXBackend, MockBackend
from .guards import Rules

DEFAULT_STATE = Path.home() / ".idlegrid" / "node.json"


def build_backend(a):
    if a.backend == "mock":
        return MockBackend(models=a.models.split(",") if a.models else None, tps=a.mock_tps, cheat=a.mock_cheat)
    return MLXBackend(allowed=set(a.models.split(",")) if a.models else None)


def main():
    ap = argparse.ArgumentParser(prog="idlegrid-node", description="Share this computer with IdleGrid")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="show what this machine can contribute")
    s = sub.add_parser("start", help="join the network and start serving")
    s.add_argument("--server", help="coordinator URL, e.g. https://idlegrid.example.com")
    s.add_argument("--join-token", help="one-time token from the dashboard (first run only)")
    s.add_argument("--state", type=Path, default=DEFAULT_STATE, help="where the node credentials are stored")
    s.add_argument("--backend", choices=["mlx", "mock"], default="mlx")
    s.add_argument("--models", help="comma-separated model ids this node may serve (default: any that fit)")
    s.add_argument("--no-auto-download", action="store_true", help="never download models on request")
    s.add_argument("--allow-battery", action="store_true", help="keep working on battery power")
    s.add_argument("--pause-when-active", action="store_true", help="pause while you use the computer")
    s.add_argument("--idle-seconds", type=int, default=120)
    s.add_argument("--hours", help="only work in this window, e.g. 23-7")
    s.add_argument("--name", help="display name for this machine")
    s.add_argument("--mock-tps", type=float, default=40.0, help=argparse.SUPPRESS)
    s.add_argument("--mock-cheat", choices=["dumb", "smart"], help=argparse.SUPPRESS)
    s.add_argument("--mock-mem", type=float, default=24.0, help=argparse.SUPPRESS)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    hw = hardware.detect()
    if a.cmd == "info":
        ok, why = hardware.eligibility(hw)
        print(json.dumps(hw, indent=2))
        print("eligible" if ok else f"not eligible: {why}")
        return

    if a.backend == "mock":
        hw = {**hw, "platform": "mock", "chip": "Mock GPU", "memory_gb": a.mock_mem,
              "usable_mem_gb": a.mock_mem, "backends": ["mlx"]}
    if a.name:
        hw["name"] = a.name
    ok, why = hardware.eligibility(hw)
    if not ok:
        raise SystemExit(f"This machine cannot join yet: {why}")

    backend = build_backend(a)
    creds = json.loads(a.state.read_text()) if a.state.exists() else None
    server = a.server or (creds or {}).get("server")
    if not server:
        raise SystemExit("Pass --server the first time.")
    if not creds:
        if not a.join_token:
            raise SystemExit("First run: create a join token in the dashboard (My machines > Add a machine) "
                             "and pass it with --join-token.")
        print(f"Registering {hw['name']} ({hw['chip']}, {hw['usable_mem_gb']} GB usable) ...")
        creds = register(server, a.join_token, hw, backend.benchmark(), a.state)
        print(f"Registered as {creds['node_id']}. Credentials saved to {a.state}")

    hours = tuple(int(x) for x in a.hours.split("-")) if a.hours else None
    rules = Rules(require_ac=not a.allow_battery, pause_when_active=a.pause_when_active,
                  idle_seconds=a.idle_seconds, hours=hours)
    agent = Agent(server, creds, backend, rules, hw, benchmark=None, auto_download=not a.no_auto_download)
    try:
        asyncio.run(agent.run_forever())
    except KeyboardInterrupt:
        print("Stopped. Unfinished work goes back to the queue automatically.")


if __name__ == "__main__":
    main()
