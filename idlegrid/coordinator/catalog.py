"""Model catalog: which models the network serves, per backend, and what they need.

mem_gb is weights + KV cache at ~8k context + runtime overhead (4-bit weights).
Prices are per 1M tokens (prompt + completion), in USD.
"""

MODELS = {
    "qwen2.5-0.5b": {
        "name": "Qwen2.5 0.5B Instruct",
        "params_b": 0.5,
        "mem_gb": 1.0,
        "price_per_m": 0.02,
        "layers": 24,
        "sources": {"mlx": "mlx-community/Qwen2.5-0.5B-Instruct-4bit"},
    },
    "qwen2.5-7b": {
        "name": "Qwen2.5 7B Instruct",
        "params_b": 7.6,
        "mem_gb": 5.5,
        "price_per_m": 0.10,
        "layers": 28,
        "sources": {"mlx": "mlx-community/Qwen2.5-7B-Instruct-4bit"},
    },
    "llama-3.1-8b": {
        "name": "Llama 3.1 8B Instruct",
        "params_b": 8.0,
        "mem_gb": 6.0,
        "price_per_m": 0.10,
        "layers": 32,
        "sources": {"mlx": "mlx-community/Meta-Llama-3.1-8B-Instruct-4bit"},
    },
    "qwen2.5-14b": {
        "name": "Qwen2.5 14B Instruct",
        "params_b": 14.7,
        "mem_gb": 10.5,
        "price_per_m": 0.20,
        "layers": 48,
        "sources": {"mlx": "mlx-community/Qwen2.5-14B-Instruct-4bit"},
    },
}

# Share of each charge paid to the node owner; the rest is the platform fee.
NODE_SHARE = 0.70

# Headroom so a model is only placed on a node with room to spare.
MEM_HEADROOM = 1.15


def public_catalog():
    return [
        {"id": mid, **{k: v for k, v in m.items() if k != "sources"}, "backends": list(m["sources"])}
        for mid, m in MODELS.items()
    ]


def cost_usd(model_id: str, tokens: int) -> float:
    return MODELS[model_id]["price_per_m"] * tokens / 1_000_000
