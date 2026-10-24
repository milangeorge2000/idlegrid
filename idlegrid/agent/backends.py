"""Inference backends. A backend knows which catalog models are on disk, can download
one, and can generate text. generate() is blocking and runs in a worker thread."""

import random
import re
import string
import threading
import time


class Cancelled(Exception):
    pass


class Backend:
    name = "base"

    def __init__(self, catalog: dict | None = None):
        self.catalog = catalog or {}          # model_id -> {backend: repo}

    def set_catalog(self, catalog: dict):
        self.catalog = catalog

    def ready_models(self) -> list[str]:
        raise NotImplementedError

    def prepare(self, model_id: str):
        raise NotImplementedError

    def generate(self, model_id, messages, max_tokens, temperature, on_text, cancel: threading.Event):
        """Returns (output_text, usage_dict, finish_reason)."""
        raise NotImplementedError

    def benchmark(self) -> dict:
        return {}


class MLXBackend(Backend):
    name = "mlx"

    def __init__(self, catalog=None, allowed: set[str] | None = None):
        super().__init__(catalog)
        self.allowed = allowed
        self._loaded: tuple[str, object, object] | None = None
        self._lock = threading.Lock()

    def _repo(self, model_id):
        return (self.catalog.get(model_id) or {}).get("mlx")

    def ready_models(self):
        from huggingface_hub import snapshot_download
        ready = []
        for mid in self.catalog:
            repo = self._repo(mid)
            if not repo or (self.allowed and mid not in self.allowed):
                continue
            try:
                snapshot_download(repo, local_files_only=True)
                ready.append(mid)
            except Exception:
                pass
        return ready

    def prepare(self, model_id):
        from huggingface_hub import snapshot_download
        repo = self._repo(model_id)
        if not repo:
            raise ValueError(f"{model_id} has no MLX build")
        snapshot_download(repo)

    def _model(self, model_id):
        """Keep one model in memory at a time; switching models frees the old one."""
        if self._loaded and self._loaded[0] == model_id:
            return self._loaded[1], self._loaded[2]
        from mlx_lm import load
        self._loaded = None
        import gc
        gc.collect()
        model, tok = load(self._repo(model_id))
        self._loaded = (model_id, model, tok)
        return model, tok

    def generate(self, model_id, messages, max_tokens, temperature, on_text, cancel):
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler
        with self._lock:
            model, tok = self._model(model_id)
            prompt = tok.apply_chat_template(messages, add_generation_prompt=True)
            parts, last = [], None
            for resp in stream_generate(model, tok, prompt, max_tokens=max_tokens,
                                        sampler=make_sampler(temp=float(temperature))):
                if cancel.is_set():
                    raise Cancelled()
                if resp.text:
                    parts.append(resp.text)
                    on_text(resp.text)
                last = resp
            usage = {"prompt_tokens": getattr(last, "prompt_tokens", 0),
                     "completion_tokens": getattr(last, "generation_tokens", 0),
                     "gen_tps": round(getattr(last, "generation_tps", 0.0), 1)}
            return "".join(parts), usage, getattr(last, "finish_reason", None) or "stop"

    def benchmark(self):
        out = {}
        for mid in self.ready_models():
            try:
                _, usage, _ = self.generate(mid, [{"role": "user", "content": "Count from 1 to 40."}], 64, 0.0,
                                            lambda _t: None, threading.Event())
                out[mid] = {"gen_tps": usage["gen_tps"]}
            except Exception as e:  # a broken download should not stop the node joining
                out[mid] = {"error": str(e)[:200]}
        return out


class MockBackend(Backend):
    """Fake backend for tests and demos. It pretends to have every catalog model it is
    told about. cheat="dumb" returns junk for everything; cheat="smart" passes the
    hidden test prompts but returns junk for real work (only duplicates catch it)."""
    name = "mock"
    CANARY = re.compile(r"Repeat exactly this code and nothing else: (\S+)")

    def __init__(self, catalog=None, models=None, tps=40.0, cheat=False, prepare_seconds=1.0):
        super().__init__(catalog)
        self.models = set(models) if models else None
        self.downloaded = set(self.models or [])
        self.tps, self.cheat, self.prepare_seconds = tps, cheat, prepare_seconds

    def ready_models(self):
        return sorted(m for m in self.catalog if m in self.downloaded) if self.models else sorted(self.catalog)

    def prepare(self, model_id):
        time.sleep(self.prepare_seconds)
        if self.models is not None:
            self.downloaded.add(model_id)

    def generate(self, model_id, messages, max_tokens, temperature, on_text, cancel):
        prompt = messages[-1]["content"] if messages else ""
        canary = self.CANARY.search(prompt)
        if canary and self.cheat != "dumb":   # a "smart" cheater spots and passes test prompts
            text = canary.group(1)
        elif self.cheat:
            text = " ".join("".join(random.choices(string.ascii_lowercase, k=5)) for _ in range(8))
        else:
            text = f"[{model_id}] Answer to: {prompt[:120]}"
        words = text.split(" ")[:max_tokens]
        out = []
        for i, w in enumerate(words):
            if cancel.is_set():
                raise Cancelled()
            piece = w if i == 0 else " " + w
            out.append(piece)
            on_text(piece)
            time.sleep(1.0 / self.tps)
        usage = {"prompt_tokens": max(1, len(prompt.split())), "completion_tokens": len(words), "gen_tps": self.tps}
        return "".join(out), usage, "stop" if len(words) < max_tokens else "length"

    def benchmark(self):
        return {"_default": {"gen_tps": self.tps}}
