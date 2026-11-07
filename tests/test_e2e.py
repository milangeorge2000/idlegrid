"""End-to-end tests: a real coordinator (uvicorn) and real node agents over real
WebSockets, using the mock backend so no GPU or model download is needed."""

import asyncio
import contextlib
import json
import socket
import time

import httpx
import pytest
import uvicorn

from idlegrid.agent.agent import Agent, register
from idlegrid.agent.backends import MockBackend
from idlegrid.agent.guards import Rules
from idlegrid.coordinator import hub as hubmod
from idlegrid.coordinator.app import create_app

MOCK_HW = {"name": "m", "platform": "mock", "chip": "Mock GPU", "memory_gb": 24, "usable_mem_gb": 24, "backends": ["mlx"]}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Net:
    """One coordinator plus helpers to add users and nodes."""

    def __init__(self, tmp_path):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.app = create_app(str(tmp_path / "t.db"))
        self.server = uvicorn.Server(uvicorn.Config(self.app, port=self.port, log_level="warning"))
        self.tmp = tmp_path
        self.agents = {}

    async def __aenter__(self):
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.05)
        self.http = httpx.AsyncClient(base_url=self.base, timeout=30)
        r = await self.http.post("/api/auth/register", json={"email": "buyer@test.io", "password": "password123"})
        self.http.headers["Authorization"] = "Bearer " + r.json()["token"]
        return self

    async def __aexit__(self, *_):
        for t in self.agents.values():
            t.cancel()
        await self.http.aclose()
        self.server.should_exit = True
        await self.task

    async def add_node(self, name, **mock):
        jt = (await self.http.post("/api/nodes/join-token")).json()["join_token"]
        backend = MockBackend(**mock)
        creds = await asyncio.to_thread(register, self.base, jt, {**MOCK_HW, "name": name}, backend.benchmark(),
                                        self.tmp / f"{name}.json")
        agent = Agent(self.base, creds, backend, Rules(require_ac=False), MOCK_HW)
        self.agents[name] = asyncio.create_task(agent.run_forever())
        return creds["node_id"]

    async def kill(self, name):
        self.agents.pop(name).cancel()

    async def nodes(self):
        return {n["id"]: n for n in (await self.http.get("/api/nodes")).json()}

    async def wait(self, cond, timeout=20, what="condition"):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if await cond():
                return
            await asyncio.sleep(0.2)
        raise AssertionError(f"timed out waiting for {what}")

    async def wait_active(self, *node_ids):
        async def ok():
            ns = await self.nodes()
            return all(ns.get(i, {}).get("status") == "active" for i in node_ids)
        await self.wait(ok, what="nodes to pass probation")

    async def run_job(self, n_rows, model="qwen2.5-7b", timeout=30, **extra):
        prompts = "\n".join(f"Question {i}: what is {i} squared?" for i in range(n_rows))
        job = (await self.http.post("/api/jobs", data={"model": model, "prompts": prompts, "max_tokens": 32, **extra})).json()

        async def done():
            j = (await self.http.get(f"/api/jobs/{job['id']}")).json()
            return j["status"] != "running"
        await self.wait(done, timeout=timeout, what="job to finish")
        return (await self.http.get(f"/api/jobs/{job['id']}")).json()


def run(coro):
    return asyncio.run(coro)


def test_register_stream_and_batch(tmp_path):
    async def main():
        async with Net(tmp_path) as net:
            a = await net.add_node("alpha", models=["qwen2.5-7b"], tps=200)
            b = await net.add_node("beta", models=["qwen2.5-7b"], tps=200)
            await net.wait_active(a, b)

            # Live streaming through the gateway (OpenAI-compatible SSE).
            chunks, final = [], None
            async with net.http.stream("POST", "/v1/chat/completions", json={
                    "model": "qwen2.5-7b", "stream": True, "max_tokens": 64,
                    "messages": [{"role": "user", "content": "hello there network"}]}) as r:
                assert r.status_code == 200
                async for line in r.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        ev = json.loads(line[6:])
                        if ev["choices"][0]["delta"].get("content"):
                            chunks.append(ev["choices"][0]["delta"]["content"])
                        if ev["choices"][0]["finish_reason"]:
                            final = ev
            assert len(chunks) > 3, "tokens should arrive as separate chunks"
            assert "hello there network" in "".join(chunks)
            assert final["idlegrid"]["node_id"] in (a, b)

            # Batch: 24 rows spread over both nodes, results downloadable, money moves.
            before = (await net.http.get("/api/me")).json()
            job = await net.run_job(24)
            assert job["status"] == "completed" and job["done"] == 24
            assert {n["node_id"] for n in job["by_node"]} == {a, b}, "work should spread across nodes"
            rows = [json.loads(l) for l in (await net.http.get(f"/api/jobs/{job['id']}/results")).text.splitlines()]
            assert len(rows) == 24 and all("squared" in r["output"] for r in rows)
            after = (await net.http.get("/api/me")).json()
            assert after["credits"] < before["credits"]
            assert after["earnings"] > before["earnings"]  # the buyer also owns the nodes here
    run(main())


def test_cheaters_are_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(hubmod, "VERIFY_RATE", 1.0)   # duplicate every task for this test
    async def main():
        async with Net(tmp_path) as net:
            dumb = await net.add_node("dumb", models=["qwen2.5-7b"], cheat="dumb", tps=200)
            honest1 = await net.add_node("honest1", models=["qwen2.5-7b"], tps=200)
            honest2 = await net.add_node("honest2", models=["qwen2.5-7b"], tps=200)
            smart = await net.add_node("smart", models=["qwen2.5-7b"], cheat="smart", tps=200)

            async def dumb_banned():
                return (await net.nodes())[dumb]["status"] == "banned"
            await net.wait(dumb_banned, what="the junk node to be banned by canaries")
            await net.wait_active(honest1, honest2, smart)   # the smart cheater passes canaries...

            await net.run_job(30)

            async def smart_punished():
                ns = await net.nodes()
                return ns[smart]["verify_mismatch"] > 0 and ns[smart]["reputation"] < ns[honest1]["reputation"]
            await net.wait(smart_punished, what="duplicate checks to catch the smart cheater")  # ...but not duplicates
            ns = await net.nodes()
            assert ns[honest1]["verify_mismatch"] == 0 and ns[honest2]["verify_mismatch"] == 0
    run(main())


def test_dead_node_work_is_reassigned(tmp_path):
    async def main():
        async with Net(tmp_path) as net:
            slow = await net.add_node("slow", models=["qwen2.5-7b"], tps=4)
            await net.wait_active(slow)
            job = (await net.http.post("/api/jobs", data={"model": "qwen2.5-7b", "max_tokens": 32,
                                                          "prompts": "a long question that takes a while to answer"})).json()

            async def leased():
                return (await net.http.get(f"/api/jobs/{job['id']}")).json()["by_status"].get("leased") == 1
            await net.wait(leased, what="the slow node to take the task")
            await net.kill("slow")                       # laptop lid closes mid-task
            fast = await net.add_node("fast", models=["qwen2.5-7b"], tps=200)

            async def finished():
                return (await net.http.get(f"/api/jobs/{job['id']}")).json()["status"] == "completed"
            await net.wait(finished, timeout=30, what="the task to be re-run elsewhere")
            rows = [json.loads(l) for l in (await net.http.get(f"/api/jobs/{job['id']}/results")).text.splitlines()]
            assert rows[0]["node_id"] == fast
    run(main())


def test_prewarm_downloads_model_on_demand(tmp_path):
    async def main():
        async with Net(tmp_path) as net:
            node = await net.add_node("fresh", models=["qwen2.5-0.5b"], tps=200)   # has only the small model
            await net.wait_active(node)
            job = await net.run_job(3, model="qwen2.5-7b")                          # nobody has 7B yet
            assert job["status"] == "completed"
            assert "qwen2.5-7b" in (await net.nodes())[node]["cached_models"]
    run(main())


def test_no_node_means_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(hubmod, "LIVE_WAIT_SECONDS", 0.5)
    async def main():
        async with Net(tmp_path) as net:
            r = await net.http.post("/v1/chat/completions", json={
                "model": "qwen2.5-14b", "messages": [{"role": "user", "content": "hi"}]}, timeout=40)
            assert r.status_code == 503 and "No node" in r.json()["error"]["message"]
    run(main())


def test_auth_required_and_join_token_single_use(tmp_path):
    async def main():
        async with Net(tmp_path) as net:
            r = await net.http.post("/v1/chat/completions", headers={"Authorization": "Bearer nope"},
                                    json={"model": "qwen2.5-7b", "messages": []})
            assert r.status_code == 401
            jt = (await net.http.post("/api/nodes/join-token")).json()["join_token"]
            ok = await net.http.post("/api/nodes/register", json={"join_token": jt, **MOCK_HW})
            again = await net.http.post("/api/nodes/register", json={"join_token": jt, **MOCK_HW})
            assert ok.status_code == 200 and again.status_code == 401
    run(main())


def test_installer_is_served_with_server_address(tmp_path):
    async def main():
        async with Net(tmp_path) as net:
            r = await net.http.get("/install.sh")
            assert r.status_code == 200 and r.text.startswith("#!/bin/bash")
            assert f'SERVER="${{IDLEGRID_SERVER:-{net.base}}}"' in r.text
            assert "__IDLEGRID_" not in r.text
            proxied = await net.http.get("/install.sh", headers={"x-forwarded-proto": "https", "host": "grid.example.com"})
            assert 'IDLEGRID_SERVER:-https://grid.example.com}' in proxied.text
            evil = await net.http.get("/install.sh", headers={"host": "x$(rm -rf ~)"})
            assert evil.status_code == 400
    run(main())


def test_idle_nodes_are_not_spammed_with_test_prompts(tmp_path, monkeypatch):
    monkeypatch.setattr(hubmod, "CANARY_RATE", 1.0)   # the old code sent one every second to idle nodes
    async def main():
        async with Net(tmp_path) as net:
            node = await net.add_node("idle", models=["qwen2.5-7b"], tps=200)
            await net.wait_active(node)
            db = net.app.state.db
            count = lambda: db.one("SELECT COUNT(*) n FROM tasks WHERE kind='canary' AND node_id=?", node)["n"]
            after_probation = count()
            await asyncio.sleep(4)
            assert count() == after_probation, "an idle node should get no test prompts"
    run(main())
