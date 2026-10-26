"""The node agent: register once, then keep one outbound WebSocket to the coordinator,
send heartbeats, and run whatever work arrives - only while the owner's rules allow."""

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import websockets

from . import hardware
from .backends import Backend, Cancelled
from .guards import Rules

log = logging.getLogger("idlegrid.agent")
HEARTBEAT_EVERY = 5


def ws_url(server: str) -> str:
    return server.rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/node/ws"


def register(server: str, join_token: str, hw: dict, benchmark: dict, state_path: Path) -> dict:
    r = httpx.post(server.rstrip("/") + "/api/nodes/register", timeout=30,
                   json={"join_token": join_token, **hw, "benchmark": benchmark})
    if r.status_code != 200:
        raise SystemExit(f"Registration failed: {r.json().get('error', {}).get('message', r.text)}")
    creds = {"server": server, **r.json()}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(creds, indent=2))
    os.chmod(state_path, 0o600)  # the node secret is a credential: owner-only file
    return creds


class Agent:
    def __init__(self, server: str, creds: dict, backend: Backend, rules: Rules, hw: dict,
                 benchmark: dict | None = None, auto_download=True):
        self.server, self.creds, self.backend, self.rules, self.hw = server, creds, backend, rules, hw
        self.benchmark = benchmark or {}
        self.auto_download = auto_download
        self.pool = ThreadPoolExecutor(max_workers=1)    # one generation at a time on one GPU
        self.prep_pool = ThreadPoolExecutor(max_workers=1)
        self.running: dict[str, threading.Event] = {}
        self.paused, self.pause_reason = False, None
        self._ready_cache: tuple[float, list] = (0, [])
        self.stop = asyncio.Event()

    def ready_models(self):
        ts, models = self._ready_cache
        if time.time() - ts > 15:
            models = self.backend.ready_models()
            self._ready_cache = (time.time(), models)
        return models

    async def run_forever(self):
        backoff = 1
        while not self.stop.is_set():
            try:
                await self._session()
                backoff = 1
            except SystemExit:
                raise
            except Exception as e:
                log.warning("connection lost (%s); reconnecting in %ss", e.__class__.__name__, backoff)
            if self.stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _session(self):
        async with websockets.connect(ws_url(self.server), max_size=2**24, ping_interval=20) as ws:
            self.ws, self.send_lock = ws, asyncio.Lock()
            await ws.send(json.dumps({"node_id": self.creds["node_id"], "node_secret": self.creds["node_secret"],
                                      "benchmark": self.benchmark}))
            welcome = json.loads(await ws.recv())
            if welcome.get("type") == "rejected":
                raise SystemExit(f"Coordinator rejected this node: {welcome.get('reason')}")
            self.backend.set_catalog(welcome["catalog"])
            self._ready_cache = (0, [])
            log.info("connected as %s (status: %s)", welcome["node_id"], welcome["status"])
            hb = asyncio.create_task(self._heartbeat_loop())
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg["type"] == "run":
                        asyncio.create_task(self._run(msg))
                    elif msg["type"] == "prepare":
                        asyncio.create_task(self._prepare(msg["model"]))
                    elif msg["type"] == "cancel":
                        ev = self.running.get(msg["id"])
                        if ev:
                            ev.set()
            finally:
                hb.cancel()
                for ev in self.running.values():
                    ev.set()

    async def send(self, msg):
        async with self.send_lock:
            await self.ws.send(json.dumps(msg))

    async def _heartbeat(self):
        self.paused, self.pause_reason = await asyncio.to_thread(self.rules.check)
        if self.paused:
            for ev in self.running.values():
                ev.set()  # stop work in progress; it is handed back to the queue
        ready = await asyncio.to_thread(self.ready_models)
        await self.send({"type": "heartbeat", "state": {
            "paused": self.paused, "pause_reason": self.pause_reason, "slots": 1,
            "ready_models": ready, "auto_download": self.auto_download, "busy": len(self.running)}})

    async def _heartbeat_loop(self):
        while True:
            try:
                await self._heartbeat()
            except Exception as e:
                log.debug("heartbeat failed: %s", e)
            await asyncio.sleep(HEARTBEAT_EVERY)

    async def _prepare(self, model_id):
        log.info("downloading %s ahead of demand ...", model_id)
        try:
            await asyncio.get_running_loop().run_in_executor(self.prep_pool, self.backend.prepare, model_id)
            self._ready_cache = (0, [])
            log.info("%s is ready", model_id)
            await self._heartbeat()   # advertise the new model before clearing "preparing"
            await self.send({"type": "prepared", "model": model_id})
        except Exception as e:
            log.error("could not prepare %s: %s", model_id, e)
            await self.send({"type": "prepared", "model": model_id, "error": str(e)[:300]})

    async def _run(self, msg):
        rid = msg["id"]
        if self.paused:
            await self.send({"type": "release", "id": rid})
            return
        loop = asyncio.get_running_loop()
        cancel = threading.Event()
        self.running[rid] = cancel
        stream = msg.get("stream", False)

        def on_text(text):
            if stream:
                asyncio.run_coroutine_threadsafe(self.send({"type": "chunk", "id": rid, "text": text}), loop)

        t0 = time.time()
        try:
            output, usage, finish = await loop.run_in_executor(
                self.pool, self.backend.generate, msg["model"], msg["messages"], msg.get("max_tokens", 256),
                msg.get("temperature", 0.0), on_text, cancel)
            await asyncio.sleep(0)  # let queued chunk sends go out before "done"
            await self.send({"type": "done", "id": rid, "model": msg["model"], "output": output, "usage": usage, "finish_reason": finish,
                             "seconds": round(time.time() - t0, 2)})
            log.info("finished %s on %s: %s tokens in %.1fs", rid, msg["model"],
                     usage.get("completion_tokens"), time.time() - t0)
        except Cancelled:
            await self.send({"type": "release", "id": rid})
            log.info("handed %s back (%s)", rid, self.pause_reason or "cancelled")
        except Exception as e:
            log.exception("task %s failed", rid)
            await self.send({"type": "error", "id": rid, "error": f"{e.__class__.__name__}: {e}"[:500]})
        finally:
            self.running.pop(rid, None)
