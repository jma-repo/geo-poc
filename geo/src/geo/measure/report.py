"""Markdown report a person can read in two minutes."""
from __future__ import annotations

from ..catalog import Catalog


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def render(cat: Catalog, m: dict, gap_list: list[dict], verdicts: list[dict] | None = None, run_id: str = "") -> str:
    o = m["overall"]
    lo, hi = o["mention_ci95"]
    lines = [f"# AI visibility report: {cat.brand.name}", "",
             f"Run `{run_id}` · {o['answers']} answers analyzed.", "",
             "## Headline", "",
             f"- Mentioned in **{_pct(o['mention_rate'])}** of answers (95% CI {_pct(lo)}–{_pct(hi)}).",
             f"- Our site cited as a source in **{_pct(o['citation_rate'])}** of answers.",
             f"- Share of voice among tracked brands: **{_pct(o['share_of_voice'])}**."]
    if o["avg_rank_when_mentioned"]:
        lines.append(f"- Average position when mentioned: **{o['avg_rank_when_mentioned']:.1f}** (1 = named first).")
    if o["retrieved_not_cited"]:
        lines.append(f"- {o['retrieved_not_cited']} answers fetched our page but did not cite it: "
                     "the page was found but another source answered better.")
    brands = list(o["brand_mention_rates"])
    lines += ["", "## By engine", "", "| Engine | Answers | Mentioned | 95% CI | Cited | " + " | ".join(brands) + " |",
              "|---|---|---|---|---|" + "---|" * len(brands)]
    for e, b in m["by_engine"].items():
        lo, hi = b["mention_ci95"]
        lines.append(f"| {e} | {b['answers']} | {_pct(b['mention_rate'])} | {_pct(lo)}–{_pct(hi)} | "
                     f"{_pct(b['citation_rate'])} | " + " | ".join(_pct(b['brand_mention_rates'][x]) for x in brands) + " |")
    lines += ["", "## By intent", "", "| Intent | Answers | Mentioned | Cited |", "|---|---|---|---|"]
    for i, b in m["by_intent"].items():
        lines.append(f"| {i} | {b['answers']} | {_pct(b['mention_rate'])} | {_pct(b['citation_rate'])} |")

    lines += ["", "## Where AI engines get their answers", "",
              "Third-party sites cited most often. Being featured on these (reviews, guides, lists, forums) "
              "is usually the fastest way into AI answers.", ""]
    lines += [f"{n}. `{d}` ({c} answers)" for n, (d, c) in enumerate(m["top_third_party_domains"], 1)] or ["None yet."]

    lines += ["", "## Gaps: competitors appear, we do not", ""]
    if not gap_list:
        lines.append("No gaps at the current thresholds.")
    for g in gap_list[:15]:
        lines += [f"### {g['prompt']}", "",
                  f"- {g['leader']} appears in {_pct(g['leader_rate'])} of answers; we appear in {_pct(g['our_rate'])}."]
        if g["uncovered_fanout"]:
            lines.append("- Searches engines run that our pages do not answer: " + "; ".join(f"`{q}`" for q in g["uncovered_fanout"]))
        if g["sources_engines_used"]:
            lines.append("- Sources used for this question: " + ", ".join(f"`{d}`" for d, _ in g["sources_engines_used"]))
        lines.append("")

    if verdicts is not None:
        bad = [v for v in verdicts if v["inaccuracies"]]
        lines += ["## Accuracy of what AI says about us", "",
                  f"{len(verdicts)} answers judged · {len(bad)} with inaccuracies · "
                  f"{sum(v['recommended'] for v in verdicts)} recommend us.", ""]
        for v in bad[:20]:
            for i in v["inaccuracies"]:
                lines.append(f"- **{i['severity']}** ({v['engine']}): \"{i['claim']}\" → {i['correction']}")
    lines += ["", "## Next actions", "",
              "1. Fix every high-severity inaccuracy at its source: update the catalog, then rebuild pages and feeds.",
              "2. For each gap, draft answer cards (`geo cards`), review them, move approved ones to `content/approved/`.",
              "3. Pitch the top third-party domains with real samples and data; do not pay for undisclosed placements.",
              "4. Re-run the same panel next week and compare."]
    return "\n".join(lines) + "\n"
