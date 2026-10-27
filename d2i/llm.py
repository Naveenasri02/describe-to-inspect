"""Tool-calling chat for the Shift Supervisor: Muse Spark Open (open weights) on Baseten first,
Muse Spark 1.3 on the Meta API when Baseten fails."""
import os
import time
from pathlib import Path

from d2i import trace
from d2i.meta_api import MetaClient

BASETEN_URL = "https://inference.baseten.co/v1/chat/completions"
BASETEN_MODEL = "meta-models/muse-spark-open-1026"
BASETEN_KEY_FILE = Path.home() / ".secrets" / "baseten_meta_hackathon_key.txt"
BASETEN = "Baseten (open weights)"
OPEN_IN, OPEN_OUT = 1.25e-6, 4.25e-6  # USD per token; shown in the trace only, Baseten credit has no card behind it


class ToolChat:
    def __init__(self, budget, http=None, timeout: float = 30.0):
        if http is None:
            import requests
            http = requests
        self.http, self.timeout = http, timeout
        self.meta = MetaClient(budget, http=http)
        self.last_platform = None

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        self.last_platform = None
        msg = self._baseten(messages, tools)
        if msg is not None:
            self.last_platform = BASETEN
            return msg
        msg = self.meta.chat_tools(messages, tools)  # raises MetaError when this fails too
        self.last_platform = "Meta API"
        return msg

    def _baseten(self, messages, tools):
        """Spark Open's assistant message, or None on any failure so the Meta API answers instead."""
        key = os.environ.get("BASETEN_API_KEY") or (
            BASETEN_KEY_FILE.read_text().strip() if BASETEN_KEY_FILE.exists() else "")
        if not key:
            return None
        t0, usd, msg, cause = time.perf_counter(), 0.0, None, ""
        try:
            r = self.http.post(BASETEN_URL, headers={"Authorization": f"Bearer {key}"}, timeout=self.timeout,
                               json={"model": BASETEN_MODEL, "messages": messages, "tools": tools,
                                     "reasoning_effort": "low", "max_tokens": 4000})
            if 200 <= r.status_code < 300:
                body = r.json()
                u = body.get("usage") or {}
                usd = u.get("prompt_tokens", 0) * OPEN_IN + u.get("completion_tokens", 0) * OPEN_OUT
                m = body["choices"][0]["message"]
                msg = m if m.get("content") or m.get("tool_calls") else None
                cause = "" if msg else "no answer and no tool call"
            else:
                cause = f"HTTP {r.status_code}"
        except Exception as e:  # network error, timeout or a malformed body: the Meta API answers instead
            cause = type(e).__name__  # class name only: never the key or headers
        trace.record("muse-spark-open-1026", BASETEN, "chat_tools", (time.perf_counter() - t0) * 1000, usd,
                     msg is not None, error=cause)
        return msg
