"""Turn raw probe answers into visibility metrics and a prioritized action list."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from ..catalog import Catalog
from .engines import domain_of


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion; stable for small samples."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _patterns(cat: Catalog) -> dict[str, re.Pattern]:
    return {brand: re.compile(r"(?<!\w)(" + "|".join(re.escape(a) for a in sorted(set(aliases), key=len, reverse=True))
                              + r")(?!\w)", re.IGNORECASE)
            for brand, aliases in cat.tracked_brands().items()}


def mentions(text: str, patterns: dict[str, re.Pattern]) -> list[str]:
    """Tracked brands mentioned in the answer, in order of first appearance."""
    pos = {}
    for brand, pat in patterns.items():
        m = pat.search(text or "")
        if m:
            pos[brand] = m.start()
    return sorted(pos, key=pos.get)


def annotate(rows: list[dict], cat: Catalog) -> list[dict]:
    pats = _patterns(cat)
    ours = cat.domain
    comp_domains = {c.domain.removeprefix("www.") for c in cat.competitors if c.domain}
    out = []
    for r in rows:
        if r.get("error"):
            continue
        order = mentions(r.get("text", ""), pats)
        cited = [domain_of(u) for u in r.get("cited", [])]
        retrieved = [domain_of(u) for u in r.get("retrieved", [])]
        out.append({**r, "mentioned": order,
                    "cited_domains": cited,
                    "we_cited": any(d == ours or d.endswith("." + ours) for d in cited),
                    "we_retrieved": any(d == ours or d.endswith("." + ours) for d in retrieved),
                    "third_party": [d for d in cited if d != ours and not d.endswith("." + ours)
                                    and d not in comp_domains]})
    return out


def metrics(ann: list[dict], cat: Catalog) -> dict:
    us = cat.brand.name
    brands = list(cat.tracked_brands())

    def block(rows):
        n = len(rows)
        k = sum(us in r["mentioned"] for r in rows)
        all_mentions = sum(len(r["mentioned"]) for r in rows)
        ranks = [r["mentioned"].index(us) + 1 for r in rows if us in r["mentioned"]]
        return {
            "answers": n,
            "mention_rate": k / n if n else 0.0,
            "mention_ci95": wilson(k, n),
            "citation_rate": (sum(r["we_cited"] for r in rows) / n) if n else 0.0,
            "retrieved_not_cited": sum(r["we_retrieved"] and not r["we_cited"] for r in rows),
            "share_of_voice": (sum(r["mentioned"].count(us) for r in rows) / all_mentions) if all_mentions else 0.0,
            "avg_rank_when_mentioned": (sum(ranks) / len(ranks)) if ranks else None,
            "brand_mention_rates": {b: (sum(b in r["mentioned"] for r in rows) / n if n else 0.0) for b in brands},
        }

    by_engine = defaultdict(list)
    by_intent = defaultdict(list)
    for r in ann:
        by_engine[r["engine"]].append(r)
        by_intent[r.get("intent") or "unknown"].append(r)
    domains = Counter(d for r in ann for d in set(r["third_party"]))
    return {
        "overall": block(ann),
        "by_engine": {e: block(rs) for e, rs in sorted(by_engine.items())},
        "by_intent": {i: block(rs) for i, rs in sorted(by_intent.items())},
        "top_third_party_domains": domains.most_common(15),
    }


def _tokens(s: str) -> set[str]:
    stop = {"the", "a", "an", "for", "in", "of", "to", "and", "or", "best", "how", "what", "is", "my",
            "with", "on", "do", "i", "you", "vs", "near", "me", "2026"}
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in stop and len(w) > 2}


def gaps(ann: list[dict], prompts: list[dict], cat: Catalog, coverage_text: str,
         competitor_min: float = 0.5, ours_max: float = 0.2) -> list[dict]:
    """Prompts where competitors show up and we do not, with the fan-out queries we fail to cover."""
    us = cat.brand.name
    rows_by_prompt = defaultdict(list)
    for r in ann:
        rows_by_prompt[r["prompt_id"]].append(r)
    covered = _tokens(coverage_text)
    out = []
    for p in prompts:
        rows = rows_by_prompt.get(p["id"])
        if not rows:
            continue
        n = len(rows)
        ours = sum(us in r["mentioned"] for r in rows) / n
        comp = {b: sum(b in r["mentioned"] for r in rows) / n for b in cat.tracked_brands() if b != us}
        leader, lead_rate = max(comp.items(), key=lambda kv: kv[1], default=(None, 0.0))
        if lead_rate >= competitor_min and ours <= ours_max:
            # A sub-query counts as covered when at least 60% of its terms appear in our content.
            # Lexical and rough on purpose: it flags candidates for a person to check.
            uncovered = [q for q in p.get("fanout", [])
                         if _tokens(q) and len(_tokens(q) & covered) / len(_tokens(q)) < 0.6]
            sources = Counter(d for r in rows for d in set(r["third_party"])).most_common(5)
            out.append({"prompt_id": p["id"], "prompt": p["text"], "intent": p.get("intent"),
                        "our_rate": ours, "leader": leader, "leader_rate": lead_rate,
                        "uncovered_fanout": uncovered, "sources_engines_used": sources})
    return sorted(out, key=lambda g: (g["leader_rate"] - g["our_rate"]), reverse=True)
