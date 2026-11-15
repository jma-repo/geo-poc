"""One interface, two providers: DeepSeek (OpenAI-compatible JSON mode) or Claude (structured outputs).

  LLM_PROVIDER=deepseek  DEEPSEEK_API_KEY=...  [DEEPSEEK_MODEL=deepseek-flash]
  LLM_PROVIDER=claude    ANTHROPIC_API_KEY=... [CLAUDE_MODEL=claude-opus-5-5]

DeepSeek has no strict JSON Schema mode, so the schema goes in the prompt and every reply is validated
with pydantic; an invalid reply is retried once with the validation error.
"""
from __future__ import annotations

import json
import os
import time
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLM:
    name = "base"

    def json(self, system: str, user: str, schema: type[T], max_tokens: int = 16000) -> T:
        raise NotImplementedError


class DeepSeek(LLM):
    name = "deepseek"
    url = "https://api.deepseek.com/chat/completions"

    def __init__(self, model: str | None = None, http: httpx.Client | None = None):
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
        self.key = os.environ.get("DEEPSEEK_API_KEY")
        if not self.key:
            raise SystemExit("Set DEEPSEEK_API_KEY")
        self.http = http or httpx.Client(timeout=600)

    def _post(self, messages: list[dict], max_tokens: int) -> str:
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"}}
        for attempt in range(5):
            try:
                r = self.http.post(self.url, json=body, headers={"Authorization": f"Bearer {self.key}"})
            except httpx.TransportError:
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            choice = r.json()["choices"][0]
            if choice.get("finish_reason") == "length":
                raise RuntimeError("DeepSeek output truncated at max_tokens")
            return choice["message"]["content"] or ""
        raise RuntimeError("DeepSeek: gave up after retries")

    def json(self, system, user, schema, max_tokens=16000):
        sys_prompt = (f"{system}\n\nRespond with one JSON object that validates against this JSON Schema. "
                      f"No prose, no code fences.\n{json.dumps(schema.model_json_schema())}")
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]
        text = self._post(messages, max_tokens)
        try:
            return schema.model_validate_json(text)
        except ValidationError as e:
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"That JSON is invalid: {str(e)[:1500]}\nReturn the corrected JSON only."}]
            return schema.model_validate_json(self._post(messages, max_tokens))


class Claude(LLM):
    name = "claude"

    def __init__(self, client=None):
        import anthropic
        self.client = client or anthropic.Anthropic()
        self.model = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")

    def json(self, system, user, schema, max_tokens=16000):
        resp = self.client.beta.messages.parse(
            model=self.model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}], output_format=schema,
            output_config={"effort": os.environ.get("CLAUDE_EFFORT", "low")},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        if resp.stop_reason in ("refusal", "max_tokens") or resp.parsed_output is None:
            raise RuntimeError(f"Claude stop_reason={resp.stop_reason}")
        return resp.parsed_output


def from_env() -> LLM:
    provider = os.environ.get("LLM_PROVIDER", "deepseek").lower()
    if provider == "deepseek":
        return DeepSeek()
    if provider == "claude":
        return Claude()
    raise SystemExit(f"Unknown LLM_PROVIDER {provider!r}; use deepseek or claude")
