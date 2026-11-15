"""Engine adapters: ask a question with live web search enabled, return the answer and its sources.

API answers approximate, but are not identical to, what people see in the consumer apps
(ChatGPT, Claude, Perplexity, Gemini). Treat results as a consistent proxy and watch trends.

Each adapter separates
- cited: sources the answer actually points to (what readers see), and
- retrieved: sources the engine fetched but may not have cited.
Being retrieved but not cited means the page was found but lost on content.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

import anthropic
import httpx

from ..llm import FALLBACK_BETA


@dataclass
class Answer:
    engine: str
    model: str
    text: str = ""
    cited: list[str] = field(default_factory=list)
    retrieved: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def domain_of(url_or_domain: str) -> str:
    s = url_or_domain.strip().lower()
    host = urlsplit(s).hostname if "://" in s else s.split("/")[0]
    return (host or "").removeprefix("www.")


def _dedupe(xs) -> list[str]:
    return list(dict.fromkeys(x for x in xs if x))


@dataclass
class Location:
    city: str = "Calgary"
    region: str = "Alberta"
    country: str = "CA"
    timezone: str = "America/Edmonton"


class Engine:
    name = "base"

    def __init__(self, model: str, location: Location | None = None):
        self.model = model
        self.location = location or Location()

    def ask(self, prompt: str) -> Answer:
        try:
            return self._ask(prompt)
        except Exception as e:  # one failed sample must not stop a run
            return Answer(self.name, self.model, error=f"{type(e).__name__}: {e}"[:500])

    def _ask(self, prompt: str) -> Answer:
        raise NotImplementedError


class ClaudeEngine(Engine):
    name = "claude"

    def __init__(self, model: str, location: Location | None = None, client: anthropic.Anthropic | None = None):
        super().__init__(model, location)
        self.client = client or anthropic.Anthropic()

    def _ask(self, prompt: str) -> Answer:
        loc = self.location
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5,
                  "user_location": {"type": "approximate", "city": loc.city, "region": loc.region,
                                    "country": loc.country, "timezone": loc.timezone}}]
        messages = [{"role": "user", "content": prompt}]
        blocks: list[dict] = []
        for _ in range(5):  # resume server-tool turns that pause
            resp = self.client.beta.messages.create(
                model=self.model, max_tokens=16000, messages=messages, tools=tools,
                output_config={"effort": os.environ.get("GEO_PROBE_EFFORT", "low")},
                betas=[FALLBACK_BETA], fallbacks="default")
            content = [b.model_dump() for b in resp.content]
            blocks += content
            if resp.stop_reason != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": content}]
        if resp.stop_reason == "refusal":
            return Answer(self.name, self.model, error="refusal")
        text, cited, retrieved = [], [], []
        for b in blocks:
            if b.get("type") == "text":
                text.append(b.get("text", ""))
                cited += [c.get("url") for c in (b.get("citations") or []) if c.get("url")]
            elif b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
                retrieved += [r.get("url") for r in b["content"] if isinstance(r, dict)]
        return Answer(self.name, self.model, "".join(text), _dedupe(cited), _dedupe(retrieved))


class OpenAIEngine(Engine):
    """OpenAI Responses API with the hosted web_search tool."""
    name = "openai"
    url = "https://api.openai.com/v1/responses"

    def __init__(self, model: str, location: Location | None = None, http: httpx.Client | None = None):
        super().__init__(model, location)
        self.http = http or httpx.Client(timeout=180)

    def _ask(self, prompt: str) -> Answer:
        loc = self.location
        body = {"model": self.model, "input": prompt,
                "tools": [{"type": "web_search", "user_location": {
                    "type": "approximate", "city": loc.city, "region": loc.region,
                    "country": loc.country, "timezone": loc.timezone}}]}
        r = self.http.post(self.url, json=body,
                           headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
        r.raise_for_status()
        data = r.json()
        text, cited, retrieved = [], [], []
        for item in data.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        text.append(c.get("text", ""))
                        cited += [a.get("url") for a in c.get("annotations", []) if a.get("type") == "url_citation"]
            elif item.get("type") == "web_search_call":
                retrieved += [s.get("url") for s in (item.get("action") or {}).get("sources", []) or []]
        return Answer(self.name, self.model, "".join(text), _dedupe(cited), _dedupe(retrieved))


class PerplexityEngine(Engine):
    name = "perplexity"
    url = "https://api.perplexity.ai/chat/completions"

    def __init__(self, model: str, location: Location | None = None, http: httpx.Client | None = None):
        super().__init__(model, location)
        self.http = http or httpx.Client(timeout=180)

    def _ask(self, prompt: str) -> Answer:
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}],
                "web_search_options": {"user_location": {"country": self.location.country}}}
        r = self.http.post(self.url, json=body,
                           headers={"Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}"})
        r.raise_for_status()
        data = r.json()
        text = data["choices"][0]["message"].get("content", "")
        results = [s.get("url") for s in data.get("search_results") or []]
        cited = data.get("citations") or results
        return Answer(self.name, self.model, text, _dedupe(cited), _dedupe(results))


class GeminiEngine(Engine):
    """Gemini API with Grounding with Google Search (closest API proxy for AI Overviews / AI Mode)."""
    name = "gemini"
    base = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, model: str, location: Location | None = None, http: httpx.Client | None = None):
        super().__init__(model, location)
        self.http = http or httpx.Client(timeout=180)

    def _ask(self, prompt: str) -> Answer:
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]}
        r = self.http.post(f"{self.base}/{self.model}:generateContent", json=body,
                           headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
        r.raise_for_status()
        cand = (r.json().get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
        cited = []
        for ch in (cand.get("groundingMetadata") or {}).get("groundingChunks", []):
            web = ch.get("web") or {}
            uri, title = web.get("uri", ""), web.get("title", "")
            # Grounding URIs are Google redirect links; the title carries the source domain.
            cited.append(title if "vertexaisearch" in domain_of(uri) and title else uri)
        return Answer(self.name, self.model, text, _dedupe(cited), [])


def from_env() -> list[Engine]:
    """Enable each engine whose API key and model are configured."""
    engines: list[Engine] = []
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GEO_ENABLE_CLAUDE"):
        engines.append(ClaudeEngine(os.environ.get("GEO_PROBE_CLAUDE_MODEL", "claude-opus-5-5")))
    if os.environ.get("OPENAI_API_KEY") and os.environ.get("GEO_OPENAI_MODEL"):
        engines.append(OpenAIEngine(os.environ["GEO_OPENAI_MODEL"]))
    if os.environ.get("PERPLEXITY_API_KEY"):
        engines.append(PerplexityEngine(os.environ.get("GEO_PERPLEXITY_MODEL", "sonar")))
    if os.environ.get("GEMINI_API_KEY") and os.environ.get("GEO_GEMINI_MODEL"):
        engines.append(GeminiEngine(os.environ["GEO_GEMINI_MODEL"]))
    return engines
