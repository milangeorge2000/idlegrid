"""Coordinator + gateway: accounts, node registration, batch jobs, OpenAI-compatible
streaming API, the node WebSocket, and the dashboard."""

import argparse
import asyncio
import csv
import io
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, catalog
from .db import DB, new_id, now
from .hub import Hub

WEB = Path(__file__).parent / "web"
MAX_BATCH_ROWS = 10_000
log = logging.getLogger("idlegrid")


def create_app(db_path: str | None = None) -> FastAPI:
    db = DB(db_path or os.environ.get("IDLEGRID_DB", "data/idlegrid.db"))

    @asynccontextmanager
    async def lifespan(app):
        app.state.hub = Hub(db)
        app.state.hub.start()
        yield
        await app.state.hub.stop()

    app = FastAPI(title="IdleGrid", lifespan=lifespan)
    app.state.db = db

    def hub() -> Hub:
        return app.state.hub

    def current_user(authorization: str | None = Header(default=None)) -> dict:
        token = authorization.split(" ", 1)[1] if authorization and " " in authorization else None
        user = auth.user_from_bearer(db, token)
        if not user:
            raise HTTPException(401, "Sign in or pass a valid API key as 'Authorization: Bearer ...'.")
        return user

    def public_user(u):
        return {"id": u["id"], "email": u["email"], "credits": round(u["credits"], 6),
                "earnings": round(u["earnings"], 6)}

    # ------------------------------------------------------------------ accounts
    class Creds(BaseModel):
        email: str
        password: str

    @app.post("/api/auth/register")
    def register(c: Creds):
        try:
            user = auth.register_user(db, c.email, c.password)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"token": auth.login(db, c.email, c.password), "user": public_user(user)}

    @app.post("/api/auth/login")
    def login(c: Creds):
        try:
            token = auth.login(db, c.email, c.password)
        except ValueError as e:
            raise HTTPException(401, str(e))
        return {"token": token, "user": public_user(auth.user_from_bearer(db, token))}

    @app.get("/api/me")
    def me(user=Depends(current_user)):
        return public_user(user)

    @app.post("/api/keys")
    def new_key(user=Depends(current_user)):
        return {"api_key": auth.create_api_key(db, user["id"])}

    @app.get("/api/ledger")
    def ledger(user=Depends(current_user)):
        return db.all("SELECT ts, node_id, amount, kind, ref FROM ledger WHERE user_id = ? ORDER BY id DESC LIMIT 100",
                      user["id"])

    # ------------------------------------------------------------------ catalog + network
    @app.get("/api/models")
    def models():
        stats = hub().network_stats()["warm_nodes_per_model"]
        return [{**m, "warm_nodes": stats.get(m["id"], 0)} for m in catalog.public_catalog()]

    @app.get("/api/network")
    def network():
        return hub().network_stats()

    # ------------------------------------------------------------------ node registration
    @app.post("/api/nodes/join-token")
    def join_token(user=Depends(current_user)):
        return {"join_token": auth.create_join_token(db, user["id"])}

    class NodeRegistration(BaseModel):
        join_token: str
        name: str
        platform: str
        chip: str
        memory_gb: float
        usable_mem_gb: float
        backends: list[str]
        benchmark: dict = {}

    @app.post("/api/nodes/register")
    def register_node(r: NodeRegistration):
        owner = auth.consume_join_token(db, r.join_token)
        if not owner:
            raise HTTPException(401, "This join token is invalid, already used or expired. Create a new one in the dashboard.")
        node_id = new_id("n")
        secret = "ns_" + os.urandom(24).hex()
        db.insert("nodes", id=node_id, user_id=owner, secret_hash=auth.fast_hash(secret), name=r.name,
                  platform=r.platform, chip=r.chip, memory_gb=r.memory_gb, usable_mem_gb=r.usable_mem_gb,
                  backends=r.backends, benchmark=r.benchmark, created_at=now())
        return {"node_id": node_id, "node_secret": secret, "status": "probation"}

    @app.get("/api/nodes")
    def my_nodes(user=Depends(current_user)):
        rows = db.all("SELECT * FROM nodes WHERE user_id = ? ORDER BY created_at", user["id"])
        live = hub().nodes
        out = []
        for r in rows:
            conn = live.get(r["id"])
            r.pop("secret_hash", None)
            r["online"] = conn is not None
            r["paused"] = bool(conn and conn.paused)
            r["pause_reason"] = conn.state.get("pause_reason") if conn else None
            r["busy"] = len(conn.active) if conn else 0
            r["preparing"] = sorted(conn.preparing) if conn else []
            out.append(r)
        return out

    @app.websocket("/node/ws")
    async def node_ws(ws: WebSocket):
        await ws.accept()
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
        except Exception:
            await ws.close(code=4400)
            return
        node = db.one("SELECT * FROM nodes WHERE id = ?", hello.get("node_id", ""))
        if not node or node["secret_hash"] != auth.fast_hash(hello.get("node_secret", "")):
            await ws.send_text(json.dumps({"type": "rejected", "reason": "unknown node or wrong secret"}))
            await ws.close(code=4401)
            return
        if node["status"] == "banned":
            await ws.send_text(json.dumps({"type": "rejected", "reason": "node is banned after failed verification"}))
            await ws.close(code=4403)
            return
        if hello.get("benchmark"):
            db.update("nodes", node["id"], benchmark=hello["benchmark"])
        conn = await hub().node_connected(node["id"], ws)
        await conn.send({"type": "welcome", "node_id": node["id"], "status": node["status"],
                         "catalog": {k: v["sources"] for k, v in catalog.MODELS.items()}})
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                await hub().on_message(conn, msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await hub().node_disconnected(conn)

    # ------------------------------------------------------------------ batch jobs
    def parse_rows(raw: str, filename: str) -> list[list[dict]]:
        rows = []
        if filename.endswith(".csv"):
            for rec in csv.DictReader(io.StringIO(raw)):
                text = rec.get("prompt") or rec.get("text") or next(iter(rec.values()), "")
                rows.append([{"role": "user", "content": text}])
        else:
            for n, line in enumerate(raw.splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                if filename.endswith(".jsonl") or line.startswith("{"):
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        raise HTTPException(400, f"Line {n} is not valid JSON.")
                    if "messages" in rec:
                        rows.append(rec["messages"])
                    elif "prompt" in rec:
                        rows.append([{"role": "user", "content": str(rec["prompt"])}])
                    else:
                        raise HTTPException(400, f"Line {n} needs a 'messages' or 'prompt' field.")
                else:
                    rows.append([{"role": "user", "content": line}])
        if not rows:
            raise HTTPException(400, "No prompts found.")
        if len(rows) > MAX_BATCH_ROWS:
            raise HTTPException(400, f"Batches are limited to {MAX_BATCH_ROWS} rows.")
        return rows

    @app.post("/api/jobs")
    async def create_job(model: str = Form(...), max_tokens: int = Form(256), temperature: float = Form(0.0),
                         prompts: str = Form(""), file: UploadFile | None = File(None), user=Depends(current_user)):
        if model not in catalog.MODELS:
            raise HTTPException(400, f"Unknown model {model}.")
        max_tokens = max(1, min(max_tokens, 4096))
        raw, name = (prompts, "prompts.txt")
        if file is not None and file.filename:
            raw, name = ((await file.read()).decode("utf-8", "replace"), file.filename.lower())
        rows = parse_rows(raw, name)
        est_tokens = sum(len(json.dumps(r)) // 4 for r in rows) + len(rows) * max_tokens
        est_cost = catalog.cost_usd(model, est_tokens)
        if est_cost > user["credits"]:
            raise HTTPException(402, f"Estimated cost ${est_cost:.4f} exceeds your credits ${user['credits']:.4f}.")
        job = hub().create_batch_job(user, model, rows, max_tokens, temperature)
        return {**job, "estimated_cost": est_cost}

    @app.get("/api/jobs")
    def list_jobs(user=Depends(current_user)):
        return db.all("SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 50", user["id"])

    def own_job(job_id, user):
        job = db.one("SELECT * FROM jobs WHERE id = ? AND user_id = ?", job_id, user["id"])
        if not job:
            raise HTTPException(404, "Job not found.")
        return job

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, user=Depends(current_user)):
        job = own_job(job_id, user)
        nodes = db.all("SELECT node_id, COUNT(*) n FROM tasks WHERE job_id = ? AND kind='batch' AND node_id IS NOT NULL "
                       "GROUP BY node_id", job_id)
        counts = db.all("SELECT status, COUNT(*) n FROM tasks WHERE job_id = ? AND kind='batch' GROUP BY status", job_id)
        return {**job, "by_node": nodes, "by_status": {c["status"]: c["n"] for c in counts}}

    @app.get("/api/jobs/{job_id}/results")
    def job_results(job_id: str, user=Depends(current_user)):
        own_job(job_id, user)
        rows = db.all("SELECT row_index, status, result, payload FROM tasks WHERE job_id = ? AND kind='batch' "
                      "ORDER BY row_index", job_id)
        lines = []
        for r in rows:
            res = r["result"] or {}
            lines.append(json.dumps({"row": r["row_index"], "status": r["status"],
                                     "input": r["payload"]["messages"], "output": res.get("output"),
                                     "usage": res.get("usage"), "node_id": res.get("node_id"),
                                     "error": res.get("error")}))
        return PlainTextResponse("\n".join(lines) + "\n", media_type="application/x-ndjson",
                                 headers={"Content-Disposition": f'attachment; filename="{job_id}.jsonl"'})

    # ------------------------------------------------------------------ OpenAI-compatible API
    @app.get("/v1/models")
    def oai_models():
        return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "idlegrid"} for m in catalog.MODELS]}

    class ChatRequest(BaseModel):
        model: str
        messages: list[dict]
        max_tokens: int | None = 512
        temperature: float | None = 0.7
        stream: bool | None = False

    @app.post("/v1/chat/completions")
    async def chat(req: ChatRequest, user=Depends(current_user)):
        if req.model not in catalog.MODELS:
            raise HTTPException(404, f"Unknown model {req.model}.")
        if user["credits"] <= 0:
            raise HTTPException(402, "You are out of credits.")
        created = int(time.time())
        cid = "chatcmpl-" + new_id("c")[2:]
        events = hub().run_live(user, req.model, req.messages, max(1, min(req.max_tokens or 512, 4096)),
                                req.temperature if req.temperature is not None else 0.7)

        def chunk(delta=None, finish=None, extra=None):
            body = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": req.model,
                    "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
            if extra:
                body.update(extra)
            return f"data: {json.dumps(body)}\n\n"

        if req.stream:
            async def sse():
                first = True
                async for ev in events:
                    if ev["type"] == "node":
                        continue
                    if ev["type"] == "chunk":
                        yield chunk({"role": "assistant", "content": ev["text"]} if first else {"content": ev["text"]})
                        first = False
                    elif ev["type"] == "done":
                        yield chunk(finish=ev.get("finish_reason", "stop"),
                                    extra={"usage": ev.get("usage"), "idlegrid": {"node_id": ev.get("node_id")}})
                    elif ev["type"] == "error":
                        yield f"data: {json.dumps({'error': {'message': ev['error']}})}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(sse(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        text, usage, node_id, finish = [], None, None, "stop"
        async for ev in events:
            if ev["type"] == "node":
                node_id = ev["node_id"]
            elif ev["type"] == "chunk":
                text.append(ev["text"])
            elif ev["type"] == "done":
                usage, finish = ev.get("usage"), ev.get("finish_reason", "stop")
                if not text and ev.get("output"):
                    text.append(ev["output"])
            elif ev["type"] == "error":
                raise HTTPException(503, ev["error"])
        return {"id": cid, "object": "chat.completion", "created": created, "model": req.model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(text)},
                             "finish_reason": finish}],
                "usage": usage, "idlegrid": {"node_id": node_id}}

    # ------------------------------------------------------------------ dashboard
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.exception_handler(HTTPException)
    async def http_error(_req, exc: HTTPException):
        return JSONResponse({"error": {"message": exc.detail}}, status_code=exc.status_code)

    return app


def main():
    import uvicorn
    ap = argparse.ArgumentParser(description="Run the IdleGrid coordinator")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--db", default=os.environ.get("IDLEGRID_DB", "data/idlegrid.db"))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(a.db), host=a.host, port=a.port, ws_ping_interval=20, ws_ping_timeout=20)


if __name__ == "__main__":
    main()
