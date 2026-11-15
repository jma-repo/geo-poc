"""Answer cards: short, answer-first sections that target the exact questions where we are missing.

Generated copy is only a draft. Two guards keep it honest:
1. Claude is told to use catalog facts only.
2. A deterministic check rejects any card containing a number that does not appear in the catalog,
   which catches invented prices, specs and statistics.
A person reviews every draft and moves it to content/approved/ before it is published.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from ..catalog import Catalog, numbers_in
from ..llm import Claude


class Card(BaseModel):
    product_id: str
    question: str = Field(description="The question as a heading, phrased the way people ask it.")
    answer: str = Field(description="Direct answer in the first sentence, at most 80 words, using facts only.")
    key_facts: list[str] = Field(description="2-5 short, checkable facts from the catalog that support the answer.")


SYSTEM = """You write answer-first web content that AI search engines can quote.
Rules:
- Use only the facts provided. Never invent numbers, specs, prices, test results, awards or reviews.
- Answer the question directly in the first sentence, then give the reason.
- Be specific and neutral; no hype, no superlatives you cannot support from the facts.
- If the facts cannot honestly answer the question, say what the product does and does not do.
- Do not mention competitors."""


def check(card: Card, cat: Catalog) -> list[str]:
    problems = []
    if card.product_id not in {p.id for p in cat.products}:
        problems.append(f"unknown product_id {card.product_id!r}")
    allowed = numbers_in(cat.facts_text())
    text = " ".join([card.answer, *card.key_facts])  # numbers in the question are the asker's, not claims
    invented = sorted(numbers_in(text) - allowed)
    if invented:
        problems.append(f"numbers not in catalog: {invented}")
    if len(card.answer.split()) > 90:
        problems.append("answer longer than 90 words")
    return problems


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def draft(cat: Catalog, gap_list: list[dict], out_dir: Path, limit: int = 10, claude: Claude | None = None) -> list[Path]:
    claude = claude or Claude()
    facts = cat.facts_text()
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for g in gap_list[:limit]:
        targets = [g["prompt"], *g.get("uncovered_fanout", [])]
        user = (f"Facts:\n{facts}\n\nWrite one answer card for the product that best fits this question. "
                f"Also make it answer these related searches where the facts allow:\n- " + "\n- ".join(targets))
        card = claude.structured(SYSTEM, user, Card, max_tokens=4000)
        problems = check(card, cat)
        doc = {**card.model_dump(), "source_prompt_id": g["prompt_id"],
               "status": "rejected" if problems else "needs_review", "problems": problems}
        path = out_dir / f"{g['prompt_id']}-{slugify(card.question)}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written
