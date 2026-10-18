"""The hub: live node connections, the scheduler, leases, verification and billing.

Every node keeps one outbound WebSocket open to the coordinator. Work flows down
that socket ("run", "prepare") and results stream back up ("chunk", "done").
Nothing ever connects *into* a node, so home routers and NAT are not a problem.
"""

import asyncio
import difflib
import json
import logging
import random
import secrets
import time

from . import catalog
from .db import DB, new_id, now

log = logging.getLogger("idlegrid.hub")

HEARTBEAT_TIMEOUT = 30        # seconds without a heartbeat before a node is treated as offline
CANARY_RATE = 0.03            # share of assignments that are hidden known-answer tests
VERIFY_RATE = 0.05            # share of finished batch tasks re-run on another node
PROBATION_CANARIES = 3        # canaries a new node must pass before getting real work
MAX_ATTEMPTS = 3
MATCH_THRESHOLD = 0.90        # output similarity needed for a duplicate to "agree"
LIVE_WAIT_SECONDS = 20.0      # how long a live request waits for a free node


class NodeConn:
    def __init__(self, node_id: str, ws, info: dict):
        self.id = node_id
        self.ws = ws
        self.info = info                      # row from the nodes table
        self.state: dict = {}                 # latest heartbeat
        self.slots = 1
        self.active: dict[str, str] = {}      # task/request id -> "task" | "live"
        self.preparing: set[str] = set()
        self.send_lock = asyncio.Lock()
        self.last_seen = time.time()

    async def send(self, msg: dict):
        async with self.send_lock:
            await self.ws.send_text(json.dumps(msg))

    @property
    def ready_models(self) -> set[str]:
        return set(self.state.get("ready_models", []))

    @property
    def paused(self) -> bool:
        return bool(self.state.get("paused"))

    @property
    def free_slots(self) -> int:
        return self.slots - len(self.active)

    def can_run(self, model_id: str) -> bool:
        m = catalog.MODELS.get(model_id)
        if not m or not set(m["sources"]) & set(self.info.get("backends") or []):
            return False
        return (self.info.get("usable_mem_gb") or 0) >= m["mem_gb"] * catalog.MEM_HEADROOM

    def speed(self, model_id: str) -> float:
        bench = self.info.get("benchmark") or {}
        return float(bench.get(model_id, {}).get("gen_tps") or bench.get("_default", {}).get("gen_tps") or 10.0)


class Hub:
    def __init__(self, db: DB):
        self.db = db
        self.nodes: dict[str, NodeConn] = {}
        self.streams: dict[str, asyncio.Queue] = {}
        self.wakeup = asyncio.Event()
        self._bg: list[asyncio.Task] = []
        # Leases from a previous run are void: put that work back in the queue.
        db.run("UPDATE tasks SET status='queued', node_id=NULL, lease_expires=NULL WHERE status='leased' AND kind != 'canary'")
        db.run("UPDATE tasks SET status='failed' WHERE status='leased' AND kind = 'canary'")

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        self._bg = [asyncio.create_task(self._dispatch_loop()), asyncio.create_task(self._reaper_loop())]

    async def stop(self):
        for t in self._bg:
            t.cancel()

    def kick(self):
        self.wakeup.set()

    # ------------------------------------------------------------------ node connections
    async def node_connected(self, node_id: str, ws) -> NodeConn:
        old = self.nodes.get(node_id)
        if old:  # a node reconnecting replaces its stale socket
            await self.node_disconnected(old)
        info = self.db.one("SELECT * FROM nodes WHERE id = ?", node_id)
        conn = NodeConn(node_id, ws, info)
        self.nodes[node_id] = conn
        self.db.update("nodes", node_id, last_seen=now())
        log.info("node %s connected (%s, %s GB usable)", node_id, info.get("chip"), info.get("usable_mem_gb"))
        return conn

    async def node_disconnected(self, conn: NodeConn):
        if self.nodes.get(conn.id) is conn:
            del self.nodes[conn.id]
        for rid, kind in list(conn.active.items()):
            if kind == "live":
                q = self.streams.get(rid)
                if q:
                    q.put_nowait({"type": "node_lost"})
            else:
                self._requeue(rid, reason="node disconnected")
        conn.active.clear()
        log.info("node %s disconnected", conn.id)
        self.kick()

    async def on_message(self, conn: NodeConn, msg: dict):
        conn.last_seen = time.time()
        t = msg.get("type")
        if t == "heartbeat":
            conn.state = msg.get("state", {})
            conn.slots = max(1, int(conn.state.get("slots", 1)))
            conn.preparing -= conn.ready_models
            self.db.update("nodes", conn.id, last_seen=now(),
                           cached_models=sorted(conn.ready_models))
            if conn.paused and conn.active:
                for rid, kind in list(conn.active.items()):
                    if kind == "task":
                        self._requeue(rid, reason="owner paused")
                        conn.active.pop(rid, None)
            self.kick()
        elif t == "chunk":
            q = self.streams.get(msg["id"])
            if q:
                q.put_nowait(msg)
        elif t == "done":
            await self._on_done(conn, msg)
        elif t == "error":
            await self._on_error(conn, msg)
        elif t == "release":
            if conn.active.pop(msg["id"], None) == "task":
                self._requeue(msg["id"], reason="released by node")
            self.kick()
        elif t == "prepared":
            conn.preparing.discard(msg.get("model"))
            self.kick()

    # ------------------------------------------------------------------ scheduling
    def eligible(self, conn: NodeConn, model_id: str, need_ready=True) -> bool:
        return (not conn.paused and conn.info.get("status") != "banned" and conn.can_run(model_id)
                and (model_id in conn.ready_models or not need_ready))

    def score(self, conn: NodeConn, model_id: str) -> float:
        fastest = max([c.speed(model_id) for c in self.nodes.values()] or [1.0])
        return (3.0 * (model_id in conn.ready_models)
                + 2.0 * float(conn.info.get("reputation", 0.5))
                + 0.5 * conn.speed(model_id) / fastest
                - 0.5 * len(conn.active))

    def pick_node_for_live(self, model_id: str, exclude=()) -> NodeConn | None:
        cands = [c for c in self.nodes.values()
                 if c.id not in exclude and c.free_slots > 0 and self.eligible(c, model_id)
                 and c.info.get("status") == "active"]
        return max(cands, key=lambda c: self.score(c, model_id), default=None)

    def _lease_seconds(self, conn: NodeConn, model_id: str, max_tokens: int) -> float:
        return 30 + 2.0 * max_tokens / max(conn.speed(model_id), 1.0)

    async def _dispatch_loop(self):
        while True:
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self.wakeup.clear()
            try:
                await self.dispatch_once()
            except Exception:  # keep the scheduler alive no matter what
                log.exception("dispatch failed")

    async def dispatch_once(self):
        self._prewarm()
        for conn in list(self.nodes.values()):
            while conn.free_slots > 0 and not conn.paused and conn.info.get("status") != "banned":
                if conn.info.get("status") == "probation" or random.random() < CANARY_RATE:
                    if await self._send_canary(conn):
                        continue
                    if conn.info.get("status") == "probation":
                        break
                task = self._pick_task(conn)
                if not task:
                    break
                await self._assign(conn, task)

    def _pick_task(self, conn: NodeConn) -> dict | None:
        runnable = [m for m in conn.ready_models if self.eligible(conn, m)]
        if not runnable:
            return None
        qs = ",".join("?" for _ in runnable)
        rows = self.db.all(
            f"SELECT * FROM tasks WHERE status='queued' AND model IN ({qs}) ORDER BY created_at LIMIT 200", *runnable)
        rows = [r for r in rows if conn.id not in (r["excluded_nodes"] or [])]
        if not rows:
            return None
        prio = {"verify": 0, "batch": 1}
        return min(rows, key=lambda r: (prio.get(r["kind"], 2), r["created_at"]))

    async def _assign(self, conn: NodeConn, task: dict):
        p = task["payload"]
        lease = self._lease_seconds(conn, task["model"], p.get("max_tokens", 256))
        with self.db.tx():
            cur = self.db.run("UPDATE tasks SET status='leased', node_id=?, lease_expires=?, attempts=attempts+1 "
                              "WHERE id=? AND status='queued'", conn.id, now() + lease, task["id"])
            if cur.rowcount != 1:
                return
        conn.active[task["id"]] = "task"
        try:
            await conn.send({"type": "run", "id": task["id"], "model": task["model"], "stream": False,
                             "messages": p["messages"], "max_tokens": p.get("max_tokens", 256),
                             "temperature": p.get("temperature", 0.0)})
        except Exception:
            conn.active.pop(task["id"], None)
            self._requeue(task["id"], reason="send failed")

    def _prewarm(self):
        """If work is waiting for a model no connected node has ready, ask one capable
        idle node to download it ahead of time."""
        waiting = [r["model"] for r in self.db.all("SELECT DISTINCT model FROM tasks WHERE status='queued'")]
        for model_id in waiting:
            if any(self.eligible(c, model_id) for c in self.nodes.values()):
                continue
            if any(model_id in c.preparing for c in self.nodes.values()):
                continue
            cands = [c for c in self.nodes.values()
                     if self.eligible(c, model_id, need_ready=False) and c.state.get("auto_download", True)]
            if cands:
                conn = max(cands, key=lambda c: c.info.get("usable_mem_gb") or 0)
                conn.preparing.add(model_id)
                asyncio.create_task(conn.send({"type": "prepare", "model": model_id}))
                log.info("asked node %s to prepare %s", conn.id, model_id)

    # ------------------------------------------------------------------ canaries
    async def _send_canary(self, conn: NodeConn) -> bool:
        runnable = [m for m in conn.ready_models if self.eligible(conn, m)]
        if not runnable:
            return False
        model_id = random.choice(runnable)
        code = f"{secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}{secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}-{random.randint(1000, 9999)}"
        payload = {"messages": [{"role": "user", "content": f"Repeat exactly this code and nothing else: {code}"}],
                   "max_tokens": 16, "temperature": 0.0, "expected": code}
        tid = new_id("t")
        self.db.insert("tasks", id=tid, kind="canary", model=model_id, payload=payload, status="leased",
                       node_id=conn.id, lease_expires=now() + 60, attempts=1, created_at=now())
        conn.active[tid] = "task"
        await conn.send({"type": "run", "id": tid, "model": model_id, "stream": False,
                         "messages": payload["messages"], "max_tokens": 16, "temperature": 0.0})
        return True

    def _canary_result(self, conn: NodeConn, task: dict, output: str | None):
        passed = output is not None and task["payload"]["expected"] in output
        info = self.db.one("SELECT * FROM nodes WHERE id = ?", conn.id)
        if passed:
            rep = min(1.0, info["reputation"] + 0.05)
            n_pass = info["canary_pass"] + 1
            status = "active" if info["status"] == "probation" and n_pass >= PROBATION_CANARIES else info["status"]
            self.db.update("nodes", conn.id, reputation=rep, canary_pass=n_pass, status=status)
        else:
            rep = max(0.0, info["reputation"] - 0.3)
            status = "banned" if rep < 0.1 else info["status"]
            self.db.update("nodes", conn.id, reputation=rep, canary_fail=info["canary_fail"] + 1, status=status)
        self.db.update("tasks", task["id"], status="done" if passed else "failed",
                       result={"output": output, "passed": passed}, finished_at=now())
        conn.info = self.db.one("SELECT * FROM nodes WHERE id = ?", conn.id)
        log.info("canary on %s %s (rep %.2f, %s)", conn.id, "passed" if passed else "FAILED", rep, conn.info["status"])

    # ------------------------------------------------------------------ results
    def _learn_speed(self, conn: NodeConn, model_id: str, usage: dict):
        """Real throughput from real work (moving average) feeds the scheduler's scoring."""
        tps = float(usage.get("gen_tps") or 0)
        if tps <= 0 or usage.get("completion_tokens", 0) < 8:
            return
        bench = dict(conn.info.get("benchmark") or {})
        old = float(bench.get(model_id, {}).get("gen_tps") or tps)
        bench[model_id] = {"gen_tps": round(0.7 * old + 0.3 * tps, 1)}
        self.db.update("nodes", conn.id, benchmark=bench)
        conn.info["benchmark"] = bench

    async def _on_done(self, conn: NodeConn, msg: dict):
        rid = msg["id"]
        kind = conn.active.pop(rid, None)
        model_id = msg.get("model") or (self.db.one("SELECT model FROM tasks WHERE id = ?", rid) or {}).get("model")
        if model_id:
            self._learn_speed(conn, model_id, msg.get("usage") or {})
        if kind == "live":
            q = self.streams.get(rid)
            if q:
                q.put_nowait(msg)
            self.kick()
            return
        task = self.db.one("SELECT * FROM tasks WHERE id = ?", rid)
        if not task or task["status"] != "leased" or task["node_id"] != conn.id:
            self.kick()
            return  # lease expired and the task moved elsewhere; ignore the late result
        output = msg.get("output", "")
        usage = msg.get("usage", {})
        tokens = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
        if task["kind"] == "canary":
            self._canary_result(conn, task, output)
            self._pay_node(conn.id, task["model"], tokens, None, "canary_pay", rid)
        elif task["kind"] == "verify":
            self._pay_node(conn.id, task["model"], tokens, None, "verify_pay", rid)
            self._verify_result(conn, task, output)
        else:
            self._batch_result(conn, task, output, usage, tokens)
        self.kick()

    async def _on_error(self, conn: NodeConn, msg: dict):
        rid = msg["id"]
        kind = conn.active.pop(rid, None)
        if kind == "live":
            q = self.streams.get(rid)
            if q:
                q.put_nowait(msg)
        elif kind == "task":
            task = self.db.one("SELECT * FROM tasks WHERE id = ?", rid)
            if task and task["kind"] == "canary":
                self._canary_result(conn, task, None)
            else:
                self._requeue(rid, reason=msg.get("error", "node error"), exclude=conn.id)
        self.kick()

    def _batch_result(self, conn, task, output, usage, tokens):
        job = self.db.one("SELECT * FROM jobs WHERE id = ?", task["job_id"])
        cost = catalog.cost_usd(task["model"], tokens)
        with self.db.tx():
            self.db.update("tasks", task["id"], status="done", tokens=tokens, finished_at=now(),
                           result={"output": output, "usage": usage, "node_id": conn.id})
            self.db.run("UPDATE jobs SET done = done + 1, tokens = tokens + ?, cost = cost + ? WHERE id = ?",
                        tokens, cost, job["id"])
            self.db.run("UPDATE users SET credits = credits - ? WHERE id = ?", cost, job["user_id"])
            self.db.insert("ledger", ts=now(), user_id=job["user_id"], amount=-cost, kind="batch_charge", ref=task["id"])
            self.db.run("UPDATE nodes SET tasks_done = tasks_done + 1, reputation = MIN(1.0, reputation + 0.01) "
                        "WHERE id = ?", conn.id)
        self._pay_node(conn.id, task["model"], tokens, cost, "batch_pay", task["id"])
        conn.info = self.db.one("SELECT * FROM nodes WHERE id = ?", conn.id)
        if task["payload"].get("temperature", 0) == 0 and random.random() < VERIFY_RATE:
            self._create_verify(task, conn.id, output)
        self._maybe_finish_job(job["id"])

    def _pay_node(self, node_id, model_id, tokens, cost, kind, ref):
        cost = catalog.cost_usd(model_id, tokens) if cost is None else cost
        pay = cost * catalog.NODE_SHARE
        node = self.db.one("SELECT user_id FROM nodes WHERE id = ?", node_id)
        with self.db.tx():
            self.db.run("UPDATE nodes SET earnings = earnings + ?, tokens_served = tokens_served + ? WHERE id = ?",
                        pay, tokens, node_id)
            self.db.run("UPDATE users SET earnings = earnings + ? WHERE id = ?", pay, node["user_id"])
            self.db.insert("ledger", ts=now(), user_id=node["user_id"], node_id=node_id, amount=pay, kind=kind, ref=ref)

    def _maybe_finish_job(self, job_id):
        job = self.db.one("SELECT * FROM jobs WHERE id = ?", job_id)
        if job and job["status"] == "running" and job["done"] + job["failed"] >= job["total"]:
            self.db.update("jobs", job_id, status="completed" if job["failed"] == 0 else "completed_with_errors",
                           finished_at=now())
            log.info("job %s finished: %s done, %s failed", job_id, job["done"], job["failed"])

    # ------------------------------------------------------------------ verification by duplicates
    def _create_verify(self, task, first_node, first_output, tiebreak=None):
        payload = dict(task["payload"])
        payload.update({"first_node": first_node, "first_output": first_output})
        if tiebreak:
            payload["tiebreak"] = tiebreak
        excluded = [first_node] + ([tiebreak["node"]] if tiebreak else [])
        self.db.insert("tasks", id=new_id("t"), job_id=task["job_id"], kind="verify", model=task["model"],
                       payload=payload, status="queued", verify_of=task["verify_of"] or task["id"],
                       excluded_nodes=excluded, created_at=now())

    @staticmethod
    def _agree(a: str, b: str) -> bool:
        return difflib.SequenceMatcher(None, a or "", b or "").ratio() >= MATCH_THRESHOLD

    def _adjust(self, node_id, delta, mismatch=False):
        self.db.run("UPDATE nodes SET reputation = MAX(0.0, MIN(1.0, reputation + ?)), "
                    "verify_mismatch = verify_mismatch + ? WHERE id = ?", delta, int(mismatch), node_id)
        self.db.run("UPDATE nodes SET status = 'banned' WHERE id = ? AND reputation < 0.1", node_id)
        if node_id in self.nodes:
            self.nodes[node_id].info = self.db.one("SELECT * FROM nodes WHERE id = ?", node_id)

    def _verify_result(self, conn, vtask, output):
        p = vtask["payload"]
        self.db.update("tasks", vtask["id"], status="done", result={"output": output, "node_id": conn.id},
                       finished_at=now())
        first, first_out = p["first_node"], p["first_output"]
        tb = p.get("tiebreak")
        if not tb:
            if self._agree(first_out, output):
                self._adjust(first, +0.02)
                self._adjust(conn.id, +0.02)
                log.info("verify %s: %s and %s agree", vtask["verify_of"], first, conn.id)
            else:  # disagreement: a third node breaks the tie
                log.warning("verify %s: %s and %s DISAGREE, scheduling tiebreak", vtask["verify_of"], first, conn.id)
                orig = self.db.one("SELECT * FROM tasks WHERE id = ?", vtask["verify_of"])
                self._create_verify(orig, first, first_out, tiebreak={"node": conn.id, "output": output})
            return
        second, second_out = tb["node"], tb["output"]
        a, b = self._agree(first_out, output), self._agree(second_out, output)
        if a and not b:
            self._adjust(second, -0.3, mismatch=True)
            self._adjust(first, +0.02)
        elif b and not a:
            self._adjust(first, -0.3, mismatch=True)
            self._adjust(second, +0.02)
            orig = self.db.one("SELECT * FROM tasks WHERE id = ?", vtask["verify_of"])
            res = dict(orig["result"] or {})
            res.update({"output": output, "corrected_by_verification": True})
            self.db.update("tasks", orig["id"], result=res)
        else:
            self._adjust(first, -0.05, mismatch=True)
            self._adjust(second, -0.05, mismatch=True)
        log.info("tiebreak %s: first=%s agree=%s, second=%s agree=%s", vtask["verify_of"], first, a, second, b)

    # ------------------------------------------------------------------ leases
    def _requeue(self, task_id, reason="", exclude=None):
        task = self.db.one("SELECT * FROM tasks WHERE id = ?", task_id)
        if not task or task["status"] != "leased":
            return
        if task["kind"] == "canary":
            self.db.update("tasks", task_id, status="failed", finished_at=now())
            return
        excluded = list(task["excluded_nodes"] or [])
        if exclude and exclude not in excluded:
            excluded.append(exclude)
        if task["attempts"] >= MAX_ATTEMPTS:
            self.db.update("tasks", task_id, status="failed", finished_at=now(),
                           result={"error": f"gave up after {task['attempts']} attempts: {reason}"})
            if task["job_id"] and task["kind"] == "batch":
                self.db.run("UPDATE jobs SET failed = failed + 1 WHERE id = ?", task["job_id"])
                self._maybe_finish_job(task["job_id"])
        else:
            self.db.update("tasks", task_id, status="queued", node_id=None, lease_expires=None,
                           excluded_nodes=excluded)
        log.info("task %s back in queue (%s)", task_id, reason)

    async def _reaper_loop(self):
        while True:
            await asyncio.sleep(2)
            try:
                for t in self.db.all("SELECT id, node_id FROM tasks WHERE status='leased' AND lease_expires < ?", now()):
                    conn = self.nodes.get(t["node_id"])
                    if conn:
                        conn.active.pop(t["id"], None)
                    self._requeue(t["id"], reason="lease expired", exclude=t["node_id"])
                for conn in list(self.nodes.values()):
                    if time.time() - conn.last_seen > HEARTBEAT_TIMEOUT:
                        try:
                            await conn.ws.close()
                        except Exception:
                            pass
                        await self.node_disconnected(conn)
                self.kick()
            except Exception:
                log.exception("reaper failed")

    # ------------------------------------------------------------------ live (streaming) requests
    async def run_live(self, user: dict, model_id: str, messages, max_tokens: int, temperature: float,
                       wait_seconds: float | None = None):
        """Async generator of events for one streaming request. Retries on another node
        if the first one disappears before sending any tokens."""
        wait_seconds = LIVE_WAIT_SECONDS if wait_seconds is None else wait_seconds
        rid = new_id("r")
        q: asyncio.Queue = asyncio.Queue()
        self.streams[rid] = q
        tried: list[str] = []
        sent_any = False
        try:
            for _attempt in range(3):
                deadline = time.time() + wait_seconds
                conn = None
                while time.time() < deadline:
                    conn = self.pick_node_for_live(model_id, exclude=tried)
                    if conn:
                        break
                    await asyncio.sleep(0.25)
                if not conn:
                    yield {"type": "error", "error": f"No node is available for {model_id} right now."}
                    return
                tried.append(conn.id)
                conn.active[rid] = "live"
                await conn.send({"type": "run", "id": rid, "model": model_id, "stream": True,
                                 "messages": messages, "max_tokens": max_tokens, "temperature": temperature})
                yield {"type": "node", "node_id": conn.id}
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=self._lease_seconds(conn, model_id, max_tokens))
                    except asyncio.TimeoutError:
                        ev = {"type": "node_lost"}
                        conn.active.pop(rid, None)
                    if ev["type"] == "chunk":
                        sent_any = True
                        yield ev
                    elif ev["type"] == "done":
                        usage = ev.get("usage", {})
                        tokens = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
                        cost = catalog.cost_usd(model_id, tokens)
                        self.db.run("UPDATE users SET credits = credits - ? WHERE id = ?", cost, user["id"])
                        self.db.insert("ledger", ts=now(), user_id=user["id"], amount=-cost, kind="live_charge", ref=rid)
                        self._pay_node(conn.id, model_id, tokens, cost, "live_pay", rid)
                        ev["node_id"] = conn.id
                        yield ev
                        return
                    elif ev["type"] in ("node_lost", "error"):
                        if sent_any:
                            yield {"type": "error", "error": "The node serving this request went offline."}
                            return
                        break  # nothing streamed yet: retry transparently on another node
            yield {"type": "error", "error": "No node could complete this request."}
        finally:
            self.streams.pop(rid, None)
            for c in self.nodes.values():
                c.active.pop(rid, None)
            self.kick()

    # ------------------------------------------------------------------ batch jobs
    def create_batch_job(self, user: dict, model_id: str, rows: list[list[dict]], max_tokens: int,
                         temperature: float) -> dict:
        jid = new_id("job")
        with self.db.tx():
            self.db.insert("jobs", id=jid, user_id=user["id"], model=model_id, status="running", total=len(rows),
                           params={"max_tokens": max_tokens, "temperature": temperature}, created_at=now())
            for i, messages in enumerate(rows):
                self.db.insert("tasks", id=new_id("t"), job_id=jid, row_index=i, kind="batch", model=model_id,
                               payload={"messages": messages, "max_tokens": max_tokens, "temperature": temperature},
                               status="queued", created_at=now())
        self.kick()
        return self.db.one("SELECT * FROM jobs WHERE id = ?", jid)

    # ------------------------------------------------------------------ views
    def network_stats(self) -> dict:
        online = list(self.nodes.values())
        models = {}
        for mid in catalog.MODELS:
            models[mid] = sum(1 for c in online if self.eligible(c, mid) and c.info.get("status") == "active")
        return {
            "nodes_online": len(online),
            "nodes_paused": sum(1 for c in online if c.paused),
            "busy_slots": sum(len(c.active) for c in online),
            "total_slots": sum(c.slots for c in online),
            "queued_tasks": self.db.one("SELECT COUNT(*) n FROM tasks WHERE status='queued'")["n"],
            "warm_nodes_per_model": models,
        }
