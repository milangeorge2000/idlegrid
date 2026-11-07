const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => "$" + Number(n || 0).toFixed(n && Math.abs(n) < 0.01 ? 5 : 2);
const ago = (ts) => { if (!ts) return "never"; const s = Date.now() / 1000 - ts; return s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`; };
let token = null, me = null, timer = null;
try { token = localStorage.getItem("ig_token"); } catch (e) {}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (token) headers.Authorization = "Bearer " + token;
  if (opts.json) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401 && !path.startsWith("/api/auth")) { logout(); throw new Error("Signed out"); }
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("application/json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(body?.error?.message || body?.detail || r.statusText);
  return body;
}

/* ---------------- auth ---------------- */
let mode = "login";
document.querySelectorAll(".tabs button").forEach((b) => b.onclick = () => {
  mode = b.dataset.mode;
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
  $("#auth-submit").textContent = mode === "login" ? "Sign in" : "Create account";
});
$("#auth-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  $("#auth-error").textContent = "";
  try {
    const r = await api(`/api/auth/${mode}`, { method: "POST", json: { email: f.get("email"), password: f.get("password") } });
    token = r.token; try { localStorage.setItem("ig_token", token); } catch (e) {}
    boot();
  } catch (err) { $("#auth-error").textContent = err.message; }
};
function logout() { token = null; try { localStorage.removeItem("ig_token"); } catch (e) {} showAuth(); }
$("#logout").onclick = logout;
function showAuth() { $("#shell").classList.add("hidden"); $("#auth").classList.remove("hidden"); }

async function boot() {
  if (!token) return showAuth();
  try { me = await api("/api/me"); } catch (e) { return showAuth(); }
  $("#auth").classList.add("hidden"); $("#shell").classList.remove("hidden");
  $("#who").textContent = me.email;
  route();
}
window.addEventListener("hashchange", route);

/* ---------------- router ---------------- */
const views = { overview, playground, batch, api: apiView, machines, job };
function route() {
  clearInterval(timer);
  const [name, arg] = (location.hash.slice(1) || "overview").split("/");
  document.querySelectorAll(".side a").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === "#" + (name === "job" ? "batch" : name)));
  (views[name] || overview)(arg);
}
const view = (html) => { $("#view").innerHTML = html; };
const poll = (fn, ms = 2000) => { fn(); timer = setInterval(fn, ms); };
async function modelOptions(selected) {
  const ms = await api("/api/models");
  return ms.map((m) => `<option value="${esc(m.id)}" ${m.id === selected ? "selected" : ""}>${esc(m.name)} · ${m.warm_nodes} warm node${m.warm_nodes === 1 ? "" : "s"} · ${money(m.price_per_m)}/1M tok</option>`).join("");
}

/* ---------------- overview ---------------- */
function overview() {
  view(`<h1>Overview</h1><p class="sub">Your account and the live state of the network.</p>
    <div class="cards" id="cards"></div><h2>Models on the network</h2><div class="scroll" id="models"></div>`);
  poll(async () => {
    const [u, n, ms] = await Promise.all([api("/api/me"), api("/api/network"), api("/api/models")]);
    $("#cards").innerHTML = [["Credits", money(u.credits)], ["Earned by your machines", money(u.earnings)],
      ["Nodes online", n.nodes_online], ["Busy / total slots", `${n.busy_slots} / ${n.total_slots}`], ["Queued tasks", n.queued_tasks]]
      .map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
    $("#models").innerHTML = `<table><tr><th>Model</th><th>Size</th><th>Needs</th><th>Price / 1M tokens</th><th>Warm nodes</th></tr>${ms.map((m) => `
      <tr><td>${esc(m.name)}<div class="muted"><code>${esc(m.id)}</code></div></td><td class="num">${m.params_b}B</td>
      <td class="num">${m.mem_gb} GB</td><td class="num">${money(m.price_per_m)}</td>
      <td>${m.warm_nodes ? `<span class="pill ok">${m.warm_nodes} ready</span>` : `<span class="pill">none yet</span>`}</td></tr>`).join("")}</table>`;
  }, 3000);
}

/* ---------------- playground (live streaming) ---------------- */
async function playground() {
  view(`<h1>Playground</h1><p class="sub">Streams from a volunteer node through the gateway, exactly like the API does.</p>
    <div class="panel"><form id="pg">
      <label>Model<select name="model">${await modelOptions("qwen2.5-7b")}</select></label>
      <label>Prompt<textarea name="prompt" rows="4" required>Explain in three sentences why idle laptops could be useful for AI.</textarea></label>
      <div class="row"><label>Max tokens<input name="max_tokens" type="number" value="256" min="1" max="4096"></label>
      <label>Temperature<input name="temperature" type="number" value="0.7" step="0.1" min="0" max="2"></label></div>
      <div class="actions"><button class="primary" id="go">Run</button><span class="error" id="pg-err"></span></div>
    </form></div>
    <h2>Response</h2><div class="panel"><div class="out" id="out"><span class="muted">Nothing yet.</span></div><div class="meta" id="meta"></div></div>`);
  $("#pg").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    $("#go").disabled = true; $("#pg-err").textContent = ""; $("#out").textContent = ""; $("#meta").textContent = "Finding a node…";
    const t0 = performance.now(); let first = null, n = 0;
    try {
      const r = await fetch("/v1/chat/completions", {
        method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
        body: JSON.stringify({ model: f.get("model"), stream: true, max_tokens: +f.get("max_tokens"), temperature: +f.get("temperature"),
          messages: [{ role: "user", content: f.get("prompt") }] }) });
      if (!r.ok) throw new Error((await r.json()).error?.message || r.statusText);
      const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { value, done } = await reader.read(); if (done) break;
        buf += dec.decode(value, { stream: true });
        let i; while ((i = buf.indexOf("\n\n")) >= 0) {
          const line = buf.slice(0, i).trim(); buf = buf.slice(i + 2);
          if (!line.startsWith("data:")) continue; const data = line.slice(5).trim(); if (data === "[DONE]") continue;
          const ev = JSON.parse(data);
          if (ev.error) throw new Error(ev.error.message);
          const d = ev.choices?.[0]?.delta?.content;
          if (d) { if (first === null) first = performance.now(); n++; $("#out").textContent += d; $("#meta").textContent = `streaming… ${n} chunks`; }
          if (ev.choices?.[0]?.finish_reason) {
            const secs = (performance.now() - (first ?? t0)) / 1000, u = ev.usage || {};
            $("#meta").textContent = `node ${ev.idlegrid?.node_id} · first token ${((first - t0) / 1000).toFixed(2)}s · ${u.completion_tokens} tokens · ${(u.completion_tokens / secs).toFixed(1)} tok/s · finish: ${ev.choices[0].finish_reason}`;
          }
        }
      }
    } catch (err) { $("#pg-err").textContent = err.message; $("#meta").textContent = ""; }
    $("#go").disabled = false;
  };
}

/* ---------------- batch jobs ---------------- */
async function batch() {
  view(`<h1>Batch jobs</h1><p class="sub">Each row becomes a task. Tasks spread across every node that has the model ready.</p>
    <div class="panel"><form id="bf">
      <label>Model<select name="model">${await modelOptions("qwen2.5-7b")}</select></label>
      <label>Prompts — one per line, or upload .jsonl ({"prompt": ...} or {"messages": [...]}) / .csv (prompt column)
        <textarea name="prompts" rows="6">Write a haiku about rain.\nName three uses of copper.\nTranslate 'good morning' into French.\nWhat is the capital of Japan?\nGive one tip for writing clean code.\nSummarise photosynthesis in one sentence.</textarea></label>
      <label>…or file<input type="file" name="file" accept=".jsonl,.csv,.txt"></label>
      <div class="row"><label>Max tokens per row<input name="max_tokens" type="number" value="128" min="1" max="4096"></label>
      <label>Temperature (0 = verifiable)<input name="temperature" type="number" value="0" step="0.1" min="0" max="2"></label></div>
      <div class="actions"><button class="primary" id="submit">Start job</button><span class="error" id="bf-err"></span></div>
    </form></div>
    <h2>Your jobs</h2><div class="scroll" id="jobs"></div>`);
  $("#bf").onsubmit = async (e) => {
    e.preventDefault(); $("#bf-err").textContent = ""; $("#submit").disabled = true;
    try { const j = await api("/api/jobs", { method: "POST", body: new FormData(e.target) }); location.hash = "job/" + j.id; }
    catch (err) { $("#bf-err").textContent = err.message; }
    $("#submit").disabled = false;
  };
  poll(async () => {
    const js = await api("/api/jobs");
    $("#jobs").innerHTML = js.length ? `<table><tr><th>Job</th><th>Model</th><th>Progress</th><th>Status</th><th>Cost</th><th>Created</th></tr>${js.map((j) => `
      <tr><td><a href="#job/${esc(j.id)}"><code>${esc(j.id)}</code></a></td><td>${esc(j.model)}</td>
      <td><div class="bar"><div style="width:${(100 * (j.done + j.failed)) / j.total}%"></div></div><span class="muted">${j.done}/${j.total}</span></td>
      <td>${statusPill(j.status)}</td><td class="num">${money(j.cost)}</td><td class="muted">${ago(j.created_at)}</td></tr>`).join("")}</table>`
      : `<p class="muted">No jobs yet.</p>`;
  });
}
const statusPill = (s) => `<span class="pill ${s === "completed" ? "ok" : s === "running" ? "warn" : s.includes("error") ? "bad" : ""}">${esc(s.replace(/_/g, " "))}</span>`;

async function job(id) {
  view(`<p><a href="#batch">← Batch jobs</a></p><h1>Job <code>${esc(id)}</code></h1><p class="sub" id="jsub"></p>
    <div class="cards" id="jcards"></div><h2>Work spread across nodes</h2><div class="scroll" id="spread"></div>
    <h2>Results</h2><div class="actions"><button id="dl">Download results (.jsonl)</button></div><div class="scroll" id="preview" style="margin-top:12px"></div>`);
  $("#dl").onclick = async () => {
    const txt = await api(`/api/jobs/${id}/results`);
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([txt], { type: "application/x-ndjson" })); a.download = id + ".jsonl"; a.click();
  };
  poll(async () => {
    const j = await api(`/api/jobs/${id}`);
    $("#jsub").innerHTML = `${esc(j.model)} · ${statusPill(j.status)}`;
    $("#jcards").innerHTML = [["Done", `${j.done} / ${j.total}`], ["Failed", j.failed], ["In progress", j.by_status.leased || 0],
      ["Waiting", j.by_status.queued || 0], ["Tokens", j.tokens], ["Cost", money(j.cost)]]
      .map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
    $("#spread").innerHTML = j.by_node.length ? `<table><tr><th>Node</th><th>Tasks</th></tr>${j.by_node.map((n) => `<tr><td><code>${esc(n.node_id)}</code></td><td class="num">${n.n}</td></tr>`).join("")}</table>` : `<p class="muted">Waiting for a node…</p>`;
    if (j.done + j.failed > 0) {
      const lines = (await api(`/api/jobs/${id}/results`)).trim().split("\n").map((l) => JSON.parse(l));
      $("#preview").innerHTML = `<table><tr><th>#</th><th>Prompt</th><th>Output</th><th>Node</th></tr>${lines.slice(0, 50).map((r) => `
        <tr><td class="num">${r.row}</td><td>${esc(r.input?.[r.input.length - 1]?.content)}</td>
        <td>${r.output != null ? esc(r.output) : `<span class="muted">${esc(r.error || r.status)}</span>`}</td><td><code>${esc(r.node_id || "")}</code></td></tr>`).join("")}</table>`;
    }
    if (j.status !== "running") clearInterval(timer);
  });
}

/* ---------------- API ---------------- */
function apiView() {
  const base = location.origin;
  view(`<h1>API</h1><p class="sub">OpenAI-compatible. Point any OpenAI SDK at this base URL.</p>
    <div class="panel"><div class="actions"><button class="primary" id="mk">Create API key</button></div><div id="key"></div>
    <h2>Python</h2><pre>from openai import OpenAI
client = OpenAI(base_url="${esc(base)}/v1", api_key="YOUR_KEY")
for chunk in client.chat.completions.create(
        model="qwen2.5-7b", stream=True,
        messages=[{"role": "user", "content": "Hello!"}]):
    print(chunk.choices[0].delta.content or "", end="")</pre>
    <h2>curl</h2><pre>curl -N ${esc(base)}/v1/chat/completions \\
  -H "Authorization: Bearer YOUR_KEY" -H "Content-Type: application/json" \\
  -d '{"model":"qwen2.5-7b","stream":true,"messages":[{"role":"user","content":"Hello!"}]}'</pre></div>`);
  $("#mk").onclick = async () => {
    const r = await api("/api/keys", { method: "POST" });
    $("#key").innerHTML = `<p>Your new key (shown once — store it somewhere safe):</p><pre>${esc(r.api_key)}</pre>`;
  };
}

/* ---------------- machines ---------------- */
function machines() {
  view(`<h1>My machines</h1><p class="sub">Share an idle Apple Silicon Mac or NVIDIA GPU. You earn 70% of what your machine serves.</p>
    <div class="panel"><div class="actions"><button class="primary" id="add">Add a machine</button></div><div id="join"></div></div>
    <h2>Registered machines</h2><div class="scroll" id="nodes"></div>`);
  $("#add").onclick = async () => {
    const r = await api("/api/nodes/join-token", { method: "POST" });
    const cmd = `curl -fsSL ${location.origin}/install.sh | bash -s -- ${r.join_token}`;
    $("#join").innerHTML = `<p>On the Apple Silicon Mac you want to share, open <b>Terminal</b> and paste this (the token works once, for 24 hours):</p>
      <pre id="cmd">${esc(cmd)}</pre>
      <div class="actions"><button id="copy">Copy command</button><span class="muted" id="copied"></span></div>
      <p class="muted">It checks the Mac, installs everything into <code>~/.idlegrid</code> (no admin password), links the Mac to your account,
      downloads a small starter model, and runs in the background at every login. It pauses on battery; add
      <code>--allow-battery</code>, <code>--pause-when-active</code> or <code>--hours 23-7</code> after the token to change when it works.</p>
      <p class="muted">Already installed? <code>idlegrid-node start --server ${esc(location.origin)} --join-token ${esc(r.join_token)}</code></p>`;
    $("#copy").onclick = async () => {
      try { await navigator.clipboard.writeText(cmd); $("#copied").textContent = "Copied"; }
      catch (e) { $("#copied").textContent = "Select the command above and copy it"; }
    };
  };
  poll(async () => {
    const ns = await api("/api/nodes");
    $("#nodes").innerHTML = ns.length ? `<table><tr><th>Machine</th><th>Hardware</th><th>State</th><th>Trust</th><th>Models ready</th><th>Served</th><th>Earned</th></tr>${ns.map((n) => {
      const state = !n.online ? `<span class="pill">offline · ${ago(n.last_seen)}</span>` : n.paused ? `<span class="pill warn">paused · ${esc(n.pause_reason)}</span>` : n.busy ? `<span class="pill ok">working</span>` : `<span class="pill ok">online · idle</span>`;
      const trust = n.status === "banned" ? `<span class="pill bad">banned</span>` : n.status === "probation" ? `<span class="pill warn">probation</span>` : `<span class="pill ok">active</span>`;
      return `<tr><td>${esc(n.name)}<div class="muted"><code>${esc(n.id)}</code></div></td><td>${esc(n.chip)}<div class="muted">${n.usable_mem_gb} GB usable</div></td>
        <td>${state}${n.preparing.length ? `<div class="muted">downloading ${esc(n.preparing.join(", "))}</div>` : ""}</td>
        <td>${trust}<div class="muted">rep ${n.reputation.toFixed(2)} · canaries ${n.canary_pass}✓ ${n.canary_fail}✗</div></td>
        <td>${(n.cached_models || []).map((m) => `<code>${esc(m)}</code>`).join(" ") || `<span class="muted">none</span>`}</td>
        <td class="num">${n.tasks_done} tasks<div class="muted">${n.tokens_served} tokens</div></td><td class="num">${money(n.earnings)}</td></tr>`;
    }).join("")}</table>` : `<p class="muted">No machines yet. Click “Add a machine”.</p>`;
  }, 3000);
}

boot();
