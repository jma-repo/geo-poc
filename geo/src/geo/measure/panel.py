"""The prompt panel: a fixed, versioned set of realistic questions to measure AI visibility against.

Two ideas from current GEO practice:
- Prompts, not keywords. People ask AI assistants long, situational questions; the panel models
  personas and buying stages instead of short head terms.
- Query fan-out. Engines split one prompt into several web searches and answer from the union.
  Each prompt carries the sub-queries an engine is likely to run; those are what your pages must
  actually rank for and answer.

Keep the panel stable between runs so week-over-week numbers are comparable; add prompts as a new
version rather than editing old ones.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..catalog import Catalog
from ..llm import Claude

Intent = Literal["informational", "comparison", "transactional", "local"]


class PanelPrompt(BaseModel):
    text: str = Field(description="The question exactly as a real person would type it into an AI assistant.")
    persona: str = Field(description="Who is asking, in a few words.")
    intent: Intent
    fanout: list[str] = Field(description="3-6 web search queries an AI engine would run to answer this.")


class PanelDraft(BaseModel):
    prompts: list[PanelPrompt]


SYSTEM = """You design measurement panels for generative engine optimization (GEO).
Write questions that real shoppers would ask an AI assistant such as ChatGPT, Claude, Perplexity or Gemini
about the category described. Rules:
- Never mention the brand being measured or any competitor by name. We measure whether the AI brings them up.
- Mix intents: informational (how to solve a problem), comparison (which kind or option is best),
  transactional (where or what to buy, budgets), local (in a named city or region).
- Vary personas, situations, constraints and budgets. Write naturally, including long conversational questions.
- For each question, list the web search queries an AI engine would most likely issue to answer it (query fan-out).
- No duplicates or near-duplicates."""


def _seed_text(cat: Catalog, demand_csv: Path | None) -> str:
    b = cat.brand
    parts = [f"Market: {b.city or ''} {b.region or ''} {b.country}. Ships to: {', '.join(b.ships_to)}."]
    for p in cat.products:
        parts.append(f"- Category: {p.category}. Problems solved: {'; '.join(p.use_cases) or p.summary}. "
                     f"Price point: about {p.price:.0f} {p.currency}.")
    if demand_csv:
        # Export of demand-radar `opportunities` (title, brief_md): real pain points become prompts.
        with demand_csv.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                parts.append(f"- Observed consumer need: {row.get('title', '')}. {row.get('brief_md', '')[:600]}")
    return "\n".join(parts)


def prompt_id(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()[:12]


def generate(cat: Catalog, n: int = 40, demand_csv: Path | None = None, claude: Claude | None = None) -> list[dict]:
    claude = claude or Claude()
    user = f"Write {n} panel questions.\n\nWhat is being measured:\n{_seed_text(cat, demand_csv)}"
    draft = claude.structured(SYSTEM, user, PanelDraft)
    names = [a.lower() for aliases in cat.tracked_brands().values() for a in aliases]
    out, seen = [], set()
    for p in draft.prompts:
        pid = prompt_id(p.text)
        leaked = any(name in p.text.lower() for name in names)
        if pid in seen or leaked or not p.text.strip():
            continue
        seen.add(pid)
        out.append({"id": pid, **p.model_dump()})
    return out


def save(prompts: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in prompts), encoding="utf-8")


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
