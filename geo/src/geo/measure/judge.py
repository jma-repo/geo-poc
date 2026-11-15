"""Answer accuracy monitoring: when an AI engine talks about your brand, is it right?

Wrong prices, discontinued products or invented features in AI answers cost sales even when you
are "visible". This judge checks each answer that mentions you against the catalog facts.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..catalog import Catalog
from ..llm import Claude


class Inaccuracy(BaseModel):
    claim: str = Field(description="What the answer said about the brand, quoted or closely paraphrased.")
    correction: str = Field(description="What the facts say instead, or 'not in facts' if unverifiable.")
    severity: Literal["high", "medium", "low"]


class Verdict(BaseModel):
    sentiment: Literal["positive", "neutral", "negative"]
    recommended: bool = Field(description="True if the answer recommends the brand rather than only listing it.")
    inaccuracies: list[Inaccuracy]


SYSTEM = """You audit AI assistant answers for factual accuracy about one brand.
You get the brand's verified facts and an answer from an AI engine. Report:
- sentiment toward the brand in the answer,
- whether the answer actually recommends it,
- every statement about the brand or its products that contradicts the facts, or that states a
  specific detail (price, spec, policy, availability, location) not present in the facts.
Statements about other brands are out of scope. Do not flag wording differences that mean the same thing."""


def judge(rows: list[dict], cat: Catalog, claude: Claude | None = None, limit: int = 50) -> list[dict]:
    claude = claude or Claude()
    facts = cat.facts_text()
    us = cat.brand.name
    out = []
    for r in [r for r in rows if us in r.get("mentioned", [])][:limit]:
        v = claude.structured(SYSTEM, f"Brand: {us}\n\nVerified facts:\n{facts}\n\nAI answer ({r['engine']}):\n{r['text']}",
                              Verdict, max_tokens=4000)
        out.append({"prompt_id": r["prompt_id"], "engine": r["engine"], "sample": r["sample"], **v.model_dump()})
    return out
