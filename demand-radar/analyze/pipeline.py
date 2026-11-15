#!/usr/bin/env python3
"""Signals -> pain points -> merged clusters -> scored opportunities, without n8n.

  python3 -m analyze.pipeline signals.jsonl --out results/

Same prompts and weights as workflows 03 and 04, plus a cluster-merging step: each extraction batch
names clusters independently, so synonyms ("car-hail-cover", "hail-protection-car") are merged
before scoring. Writes pain_points.jsonl, opportunities.json and report.md to --out.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .llm import LLM, from_env

ROOT = Path(__file__).resolve().parent.parent
WEIGHTS = {"demand": 0.30, "margin": 0.25, "gap": 0.20, "logistics": 0.15, "compliance": 0.10}


def prompt(name: str) -> str:
    return (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8").strip()


# ------------------------------------------------------------------ schemas

class PainItem(BaseModel):
    signal_id: int
    is_demand: bool
    cluster_key: str
    pain: str
    context: str
    audience: str
    quote: str
    intent_level: int = Field(ge=1, le=5)
    product_hint: str
    geo: str


class PainBatch(BaseModel):
    items: list[PainItem]


class Merge(BaseModel):
    key: str
    canonical: str = Field(description="Shared lowercase hyphenated slug for this need.")


class MergeMap(BaseModel):
    merges: list[Merge]


class Scored(BaseModel):
    cluster_key: str
    title: str
    demand: int = Field(ge=0, le=10)
    margin: int = Field(ge=0, le=10)
    gap: int = Field(ge=0, le=10)
    logistics: int = Field(ge=0, le=10)
    compliance: int = Field(ge=0, le=10)
    target_price_cad: str
    brief_md: str


class ScoredList(BaseModel):
    opportunities: list[Scored]


MERGE_SYSTEM = """You normalize product-need categories. You get category slugs with a count and an example pain.
Find slugs that describe the same purchasable need and map them to one canonical slug. Do not merge different
needs just because they share a word (dog boots and dog toys stay separate). Use the clearest existing slug
as the canonical one. Return entries ONLY for slugs that change; omit every slug that stays as it is."""


# ------------------------------------------------------------------ steps

def load_signals(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    signals, suggestions = [], defaultdict(list)
    for r in rows:
        if r["source"] == "autocomplete":
            # Autocomplete rows are search phrases, not posts: fold them into one signal per seed.
            seed = r["body"].removeprefix("Search suggestion for '").rstrip("'")
            suggestions[seed].append(r["title"])
        elif "[removed]" not in (r.get("body") or "") or r.get("title"):
            signals.append(r)
    for seed, items in suggestions.items():
        signals.append({"source": "autocomplete", "external_id": f"seed:{seed}", "url": None, "geo_hint": "canada",
                        "title": f"What people in Canada search after typing '{seed}'",
                        "body": "; ".join(sorted(set(items)))})
    for i, s in enumerate(signals):
        s["signal_id"] = i
    return signals


def extract(llm: LLM, signals: list[dict], batch: int = 20, workers: int = 8) -> tuple[list[dict], list[str]]:
    system = prompt("extract_pain_points")
    chunks = [signals[i:i + batch] for i in range(0, len(signals), batch)]

    def one(chunk):
        payload = [{"signal_id": s["signal_id"], "source": s["source"], "geo_hint": s.get("geo_hint"),
                    "title": s.get("title") or "", "body": (s.get("body") or "")[:1500]} for s in chunk]
        ids = {s["signal_id"] for s in chunk}
        try:
            out = llm.json(system, "Signals:\n" + json.dumps(payload, ensure_ascii=False), PainBatch)
        except Exception as e:  # keep going; report the failed batch
            return [], f"batch {min(ids)}-{max(ids)}: {type(e).__name__}: {str(e)[:200]}"
        return [p.model_dump() for p in out.items if p.signal_id in ids and p.is_demand and p.pain.strip()], None

    pains, errors = [], []
    with ThreadPoolExecutor(workers) as pool:
        for got, err in pool.map(one, chunks):
            pains += got
            if err:
                errors.append(err)
    for p in pains:
        p["cluster_key"] = re.sub(r"[^a-z0-9]+", "-", p["cluster_key"].lower()).strip("-")
    return [p for p in pains if p["cluster_key"]], errors


def merge_clusters(llm: LLM, pains: list[dict]) -> dict[str, str]:
    by_key = defaultdict(list)
    for p in pains:
        by_key[p["cluster_key"]].append(p["pain"])
    listing = "\n".join(f"{k} ({len(v)}): {v[0][:120]}" for k, v in sorted(by_key.items()))
    try:
        out = llm.json(MERGE_SYSTEM, listing, MergeMap, max_tokens=64000)
    except Exception as e:  # merging improves the ranking but is not essential
        print(f"warning: cluster merge skipped: {type(e).__name__}: {str(e)[:200]}")
        return {k: k for k in by_key}
    mapping = {m.key: re.sub(r"[^a-z0-9]+", "-", m.canonical.lower()).strip("-") for m in out.merges if m.key in by_key}
    return {k: mapping.get(k) or k for k in by_key}  # keys the model left out stay as they are


def aggregate(pains: list[dict], signals: list[dict], min_mentions: int = 2, top: int = 25) -> list[dict]:
    sig = {s["signal_id"]: s for s in signals}
    groups = defaultdict(list)
    for p in pains:
        groups[p["cluster_key"]].append(p)
    clusters = []
    for key, ps in groups.items():
        if len(ps) < min_mentions:
            continue
        ps.sort(key=lambda p: -p["intent_level"])
        clusters.append({
            "cluster_key": key, "mentions": len(ps),
            "sources": len({sig[p["signal_id"]]["source"].split(":")[0] for p in ps}),
            "avg_intent": round(sum(p["intent_level"] for p in ps) / len(ps), 2),
            "local_mentions": sum(p["geo"] in ("calgary", "alberta") for p in ps),
            "top_pains": [p["pain"] for p in ps[:5]], "top_quotes": [p["quote"] for p in ps[:5]],
            "product_hints": sorted({p["product_hint"] for p in ps})[:5],
            "evidence_urls": [u for u in (sig[p["signal_id"]].get("url") for p in ps[:5]) if u],
        })
    clusters.sort(key=lambda c: -c["mentions"] * c["avg_intent"])
    return clusters[:top]


def score(llm: LLM, clusters: list[dict]) -> list[dict]:
    if not clusters:
        return []
    brief = [{k: v for k, v in c.items() if k != "evidence_urls"} for c in clusters]
    out = llm.json(prompt("score_opportunities"), "Clusters:\n" + json.dumps(brief, ensure_ascii=False), ScoredList)
    by_key = {c["cluster_key"]: c for c in clusters}
    rows = []
    for o in out.opportunities:
        c = by_key.get(o.cluster_key)
        if not c:
            continue  # the model invented a cluster
        s = {k: getattr(o, k) for k in WEIGHTS}
        rows.append({"cluster_key": o.cluster_key, "title": o.title, "scores": s,
                     "total_score": round(sum(s[k] * w for k, w in WEIGHTS.items()) * 10, 1),
                     "target_price_cad": o.target_price_cad, "brief_md": o.brief_md,
                     "mentions": c["mentions"], "avg_intent": c["avg_intent"],
                     "local_mentions": c["local_mentions"], "evidence_urls": c["evidence_urls"]})
    return sorted(rows, key=lambda r: -r["total_score"])


def report(opps: list[dict], n_signals: int, n_pains: int, n_clusters: int, provider: str, errors: list[str]) -> str:
    lines = [f"# Demand Radar: opportunities", "",
             f"{n_signals} signals → {n_pains} pain points → {n_clusters} merged clusters → "
             f"{len(opps)} scored. Model provider: {provider}.", ""]
    if errors:
        lines += [f"{len(errors)} extraction batches failed (their signals were skipped).", ""]
    lines += ["| # | Opportunity | Score | Mentions | Intent | Local | Price CAD |", "|---|---|---|---|---|---|---|"]
    lines += [f"| {i} | {o['title']} | {o['total_score']} | {o['mentions']} | {o['avg_intent']} | "
              f"{o['local_mentions']} | {o['target_price_cad']} |" for i, o in enumerate(opps, 1)]
    for o in opps[:10]:
        s = o["scores"]
        lines += ["", f"## {o['title']} ({o['total_score']})", "",
                  f"Demand {s['demand']} · margin {s['margin']} · gap {s['gap']} · logistics {s['logistics']} · "
                  f"compliance {s['compliance']}", "", o["brief_md"].strip(), ""]
        lines += [f"- {u}" for u in o["evidence_urls"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("signals", type=Path, nargs="+", help="JSONL files from collectors.run")
    ap.add_argument("--out", type=Path, default=Path("results"))
    ap.add_argument("--min-mentions", type=int, default=2)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--pains", type=Path, help="reuse a pain_points.jsonl from an earlier run instead of extracting")
    args = ap.parse_args(argv)

    llm = from_env()
    tmp = args.out / "_merged_signals.jsonl"
    args.out.mkdir(parents=True, exist_ok=True)
    tmp.write_text("".join(f.read_text(encoding="utf-8") for f in args.signals), encoding="utf-8")
    signals = load_signals(tmp)
    tmp.unlink()
    pains_path = args.out / "pain_points.jsonl"
    if args.pains:
        pains, errors = [json.loads(l) for l in args.pains.read_text(encoding="utf-8").splitlines() if l.strip()], []
        print(f"reusing {len(pains)} pain points from {args.pains}", flush=True)
    else:
        print(f"{len(signals)} signals; extracting with {llm.name} …", flush=True)
        pains, errors = extract(llm, signals, workers=args.workers)
        # checkpoint: extraction is the expensive step, keep it even if a later step fails
        pains_path.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in pains), encoding="utf-8")
        print(f"{len(pains)} pain points ({len(errors)} failed batches) -> {pains_path}", flush=True)
    mapping = merge_clusters(llm, pains) if pains else {}
    for p in pains:
        p.setdefault("raw_cluster_key", p["cluster_key"])
        p["cluster_key"] = mapping.get(p["raw_cluster_key"], mapping.get(p["cluster_key"], p["cluster_key"]))
    print(f"{len(set(mapping))} cluster keys merged into {len(set(mapping.values()))}", flush=True)
    clusters = aggregate(pains, signals, args.min_mentions)
    opps = score(llm, clusters)
    pains_path.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in pains), encoding="utf-8")
    (args.out / "opportunities.json").write_text(json.dumps(opps, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "report.md").write_text(report(opps, len(signals), len(pains), len(set(mapping.values())), llm.name, errors), encoding="utf-8")
    if errors:
        print("failed batches:", *errors[:5], sep="\n  ")
    print(f"wrote {args.out}/report.md")


if __name__ == "__main__":
    main()
