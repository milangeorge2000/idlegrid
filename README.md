# IdleGrid

Decentralized LLM inference on idle Apple Silicon Macs (and later NVIDIA GPUs).
Buyers call an OpenAI-compatible API or submit batch jobs; volunteer machines do the work.

```
buyer ──HTTPS/SSE──▶ coordinator + gateway ◀──outbound WebSocket── node agents (Macs/GPUs)
                     (accounts, scheduler,                          (MLX runtime, guards,
                      leases, verification,                          model cache)
                      billing, dashboard)
```

## How it works

- **Nodes only connect out.** Each node keeps one WebSocket open to the coordinator, so
  it works behind any home router. Work flows down that socket; tokens stream back up.
- **Registration.** The owner creates a one-time join token in the dashboard, then runs
  `idlegrid-node start --server URL --join-token ...`. The agent detects the hardware,
  checks it is eligible, registers, and stores its node secret in `~/.idlegrid/node.json`.
- **Probation.** New nodes only receive hidden test prompts ("canaries") until they pass 3.
- **Scheduling.** Filter (backend, memory with 15% headroom, model on disk, not paused,
  not banned), then score (model already warm, reputation, measured speed, current load).
- **Leases.** A batch task is leased with a timeout. If the node disconnects, pauses, or
  the lease runs out, the task goes back in the queue automatically (max 3 attempts).
- **Pre-warming.** If work waits for a model no online node has, the coordinator asks one
  capable node to download it ahead of time.
- **Verification.** Canaries catch nodes returning junk. For temperature-0 work, 5% of tasks
  are re-run on another node; a disagreement triggers a third "tiebreak" run, the loser is
  penalised and the buyer's result is corrected. Reputation < 0.1 = banned.
- **Money.** Buyers pay per token (prompt + completion); node owners earn 70%.
- **Owner first.** The agent pauses on battery (`--allow-battery` to override), outside
  `--hours 23-7`, when the Mac is too hot, and with `--pause-when-active` while you use it.
  Paused work is handed back immediately.

## Share your Mac (one line)

Sign up on the coordinator's website, open **My machines → Add a machine**, and paste
the command it shows into Terminal on an Apple Silicon Mac:

```bash
curl -fsSL https://YOUR-COORDINATOR/install.sh | bash -s -- join_XXXX
```

The installer checks the Mac (Apple Silicon, 8 GB+), installs its own Python, the agent
and MLX into `~/.idlegrid` (no sudo), registers the Mac, downloads a small starter model
so it can pass its verification prompts, and runs it as a background service that starts
at every login. Add `--allow-battery`, `--pause-when-active`, `--hours 23-7`,
`--models a,b` or `--no-service` after the token to change that.

```bash
tail -f ~/.idlegrid/node.log                              # watch it work
idlegrid-node prepare recommended                          # download the biggest model that fits
launchctl bootout gui/$(id -u)/ai.idlegrid.node            # stop
rm -rf ~/.idlegrid ~/Library/LaunchAgents/ai.idlegrid.node.plist   # uninstall (after stopping)
```

## Run locally

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[mlx,dev]"
.venv/bin/idlegrid-coordinator --port 8000            # dashboard at http://localhost:8000
```

Sign up in the dashboard, open **My machines → Add a machine**, and run the command it shows:

```bash
.venv/bin/idlegrid-node start --server http://localhost:8000 --join-token join_...
```

Options: `--models qwen2.5-7b`, `--allow-battery`, `--pause-when-active`, `--hours 23-7`,
`--no-auto-download`, `--state PATH` (to run several nodes on one machine).
`idlegrid-node info` shows what this machine can contribute.

## API

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="ig_...")   # key from the API page
for chunk in client.chat.completions.create(model="qwen2.5-7b", stream=True,
        messages=[{"role": "user", "content": "Hello!"}]):
    print(chunk.choices[0].delta.content or "", end="")
```

Batch: `POST /api/jobs` (form fields `model`, `max_tokens`, `temperature`, and `prompts`
text or a `.jsonl`/`.csv` `file`), then `GET /api/jobs/{id}` and `/api/jobs/{id}/results`.

## Models

| id | needs (GB GPU memory) | price / 1M tokens |
|---|---|---|
| qwen2.5-0.5b | 1.0 | $0.02 |
| qwen2.5-7b | 5.5 | $0.10 |
| llama-3.1-8b | 6.0 | $0.10 |
| qwen2.5-14b | 10.5 | $0.20 |

A 16 GB Mac reports ~11.8 GB usable, so it serves up to 8B; 14B needs a 24 GB+ Mac
(or pipeline mode, planned).

## Tests

```bash
.venv/bin/python -m pytest -q
```

End-to-end with a real coordinator and real WebSocket agents (mock backend): streaming,
batch spread across nodes, billing, junk-node banning, "smart cheater" caught by
duplicates, dead-node failover, on-demand model download, auth.

## Not built yet

Pipeline mode (layer-split models across nodes), NVIDIA/llama.cpp backend, payments
and payouts, model store mirror (nodes currently download pinned builds from Hugging Face),
Postgres, Terraform deployment.
