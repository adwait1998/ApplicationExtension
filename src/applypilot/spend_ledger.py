"""Append-only spend ledger + a metering wrapper around the LLM client.
chat() returns only text (usage is discarded upstream), so cost is estimated
from token counts (char/4 heuristic when exact counts unavailable) x per-model
rates. One enforcement point (wired separately): callers check over_cap()."""
from __future__ import annotations

import json
import time
from pathlib import Path

# USD per 1M tokens (input, output). Extend as providers are added.
_RATES = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    "gemini-2.0-flash": (0.10, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
}
_DEFAULT_RATE = (1.0, 5.0)


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    ri, ro = _RATES.get((model or "").strip(), _DEFAULT_RATE)
    return (tokens_in / 1_000_000) * ri + (tokens_out / 1_000_000) * ro


def _est_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


class SpendLedger:
    def __init__(self, path, *, daily_cap_usd: float | None = None, monthly_cap_usd: float | None = None):
        self.path = Path(path)
        self.daily_cap_usd = daily_cap_usd
        self.monthly_cap_usd = monthly_cap_usd

    def record(self, *, stage: str, model: str, tokens_in: int, tokens_out: int,
               identity_id: str | None = None) -> float:
        cost = estimate_cost(model, tokens_in, tokens_out)
        row = {"ts": time.time(), "stage": stage, "model": model, "identity_id": identity_id,
               "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": round(cost, 6)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return cost

    def entries(self):
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    yield json.loads(line)
                except Exception:
                    continue

    def _spent_since(self, cutoff: float) -> float:
        return round(sum(e["cost_usd"] for e in self.entries() if e.get("ts", 0) >= cutoff), 6)

    def spent_today(self) -> float:
        return self._spent_since(time.time() - 86400)

    def spent_month(self) -> float:
        return self._spent_since(time.time() - 30 * 86400)

    def over_cap(self) -> bool:
        d = self.daily_cap_usd is not None and self.spent_today() >= self.daily_cap_usd
        m = self.monthly_cap_usd is not None and self.spent_month() >= self.monthly_cap_usd
        return bool(d or m)


class MeteredClient:
    """Delegates chat/ask/close to the wrapped provider client, recording spend
    per logical call. Duck-typed — works for both LLMClient and ClaudeCodeClient."""
    def __init__(self, inner, ledger: SpendLedger, *, model: str, stage: str = "llm"):
        self._inner = inner
        self._ledger = ledger
        self._model = model
        self._stage = stage

    def chat(self, messages, temperature: float = 0.0, max_tokens: int = 4096) -> str:
        out = self._inner.chat(messages, temperature=temperature, max_tokens=max_tokens)
        tin = sum(_est_tokens(m.get("content", "")) for m in messages)
        self._ledger.record(stage=self._stage, model=self._model,
                            tokens_in=tin, tokens_out=_est_tokens(out))
        return out

    def ask(self, prompt: str, **kwargs) -> str:
        return self.chat([{"role": "user", "content": prompt}], **{
            k: v for k, v in kwargs.items() if k in ("temperature", "max_tokens")})

    def close(self):
        if hasattr(self._inner, "close"):
            self._inner.close()
