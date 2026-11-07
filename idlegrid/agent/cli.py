"""idlegrid-node: share this computer with the IdleGrid network.

  idlegrid-node info                                        what this machine can contribute
  idlegrid-node register --server URL --join-token join_... link this machine to your account
  idlegrid-node prepare qwen2.5-7b                          download a model ahead of time
  idlegrid-node start                                       join the network and serve
"""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from . import hardware
from .agent import Agent, register
from .backends import MLXBackend, MockBackend
from .guards import Rules

HOME = Path(os.environ.get("IDLEGRID_HOME", Path.home() / ".idlegrid"))
DEFAULT_STATE = HOME / "node.json"


def catalog_sources() -> dict:
    from ..coordinator.catalog import MODELS
    return {k: v["sources"] for k, v in MODELS.items()}


def models_that_fit(hw: dict) -> list[str]:
    from ..coordinator.catalog import MEM_HEADROOM, MODELS
    fit = [(m["mem_gb"], k) for k, m in MODELS.items()
           if set(m["sources"]) & set(hw["backends"]) and hw["usable_mem_gb"] >= m["mem_gb"] * MEM_HEADROOM]
    return [k for _, k in sorted(fit)]


def build_backend(a):
    if getattr(a, "backend", "mlx") == "mock":
        return MockBackend(catalog_sources(), models=a.models.split(",") if a.models else None,
                           tps=a.mock_tps, cheat=a.mock_cheat)
    return MLXBackend(catalog_sources(), allowed=set(a.models.split(",")) if getattr(a, "models", None) else None)


def mock_hw(hw, mem):
    return {**hw, "platform": "mock", "chip": "Mock GPU", "memory_gb": mem, "usable_mem_gb": mem, "backends": ["mlx"]}


def main():
    ap = argparse.ArgumentParser(prog="idlegrid-node", description="Share this computer with IdleGrid")
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("info", help="show what this machine can contribute")
    i.add_argument("--json", action="store_true")

    r = sub.add_parser("register", help="link this machine to your account (one time)")
    r.add_argument("--server", required=True)
    r.add_argument("--join-token", required=True)
    r.add_argument("--name")
    r.add_argument("--state", type=Path, default=DEFAULT_STATE)

    p = sub.add_parser("prepare", help="download models ahead of time")
    p.add_argument("models", nargs="+", help="model ids, or 'recommended' for the largest that fits")

    s = sub.add_parser("start", help="join the network and start serving")
    s.add_argument("--server", help="coordinator URL (saved after the first run)")
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
    logging.getLogger("httpx").setLevel(logging.WARNING)

    hw = hardware.detect()
    if a.cmd == "info":
        ok, why = hardware.eligibility(hw)
        info = {**hw, "eligible": ok, "reason": None if ok else why, "models_that_fit": models_that_fit(hw)}
        if a.json:
            print(json.dumps(info))
        else:
            print(json.dumps(info, indent=2))
        return

    if a.cmd == "prepare":
        backend = MLXBackend(catalog_sources())
        wanted = []
        for m in a.models:
            if m == "recommended":
                fits = models_that_fit(hw)
                wanted += fits[-1:] if fits else []
            else:
                wanted.append(m)
        for m in dict.fromkeys(wanted):
            print(f"Downloading {m} ...", flush=True)
            backend.prepare(m)
            print(f"{m} ready.")
        return

    if getattr(a, "backend", "mlx") == "mock":
        hw = mock_hw(hw, a.mock_mem)
    if a.name:
        hw["name"] = a.name
    ok, why = hardware.eligibility(hw)
    if not ok:
        raise SystemExit(f"This machine cannot join yet: {why}")

    if a.cmd == "register":
        if a.state.exists():
            creds = json.loads(a.state.read_text())
            print(f"Already registered as {creds['node_id']} (credentials in {a.state}).")
            return
        print(f"Registering {hw['name']} ({hw['chip']}, {hw['usable_mem_gb']} GB usable) ...")
        creds = register(a.server, a.join_token, hw, {}, a.state)
        print(f"Registered as {creds['node_id']}.")
        return

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
