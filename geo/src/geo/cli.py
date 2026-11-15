"""geo: command-line entry point.

  geo build     catalog -> static site, JSON-LD, feeds, robots.txt, sitemap, llms.txt
  geo panel     generate the prompt panel (questions people ask AI assistants)
  geo probe     ask every configured engine, N samples per prompt
  geo report    metrics, gaps, optional accuracy audit -> markdown + JSON
  geo cards     draft grounded answer cards for the biggest gaps
  geo indexnow  notify Bing/IndexNow of changed URLs
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import catalog as catalog_mod


def _cat(args):
    return catalog_mod.load(args.catalog)


def cmd_build(args):
    from .publish import site
    paths = site.build(_cat(args), Path(args.out), Path(args.cards), enable_checkout=args.checkout)
    print(f"wrote {len(paths)} files to {args.out}")


def cmd_panel(args):
    from .measure import panel
    out = Path(args.out)
    if out.exists() and not args.force:
        sys.exit(f"{out} exists. Panels must stay stable between runs; use --out for a new version or --force.")
    prompts = panel.generate(_cat(args), args.n, Path(args.demand_csv) if args.demand_csv else None)
    panel.save(prompts, out)
    print(f"wrote {len(prompts)} prompts to {out}")


def cmd_probe(args):
    from .measure import engines, panel, probe
    prompts = panel.load(Path(args.panel))[: args.limit or None]
    selected = [e for e in engines.from_env() if not args.engines or e.name in args.engines]
    print(f"{len(prompts)} prompts × {len(selected)} engines ({', '.join(e.name for e in selected)}) × {args.samples} samples")
    path = probe.run(prompts, selected, args.samples, Path(args.out), args.workers)
    rows = probe.load(path)
    errors = sum(1 for r in rows if r.get("error"))
    print(f"wrote {path} ({len(rows)} answers, {errors} errors)")


def cmd_report(args):
    from .measure import analyze, panel, probe, report
    from .publish import site
    cat = _cat(args)
    path = Path(args.probe)
    rows = probe.load(path)
    ann = analyze.annotate(rows, cat)
    m = analyze.metrics(ann, cat)
    cards = [c for cs in site.load_cards(Path(args.cards)).values() for c in cs]
    coverage = cat.facts_text() + "\n" + "\n".join(c["question"] + " " + c["answer"] for c in cards)
    gap_list = analyze.gaps(ann, panel.load(Path(args.panel)), cat, coverage)
    verdicts = None
    if args.judge:
        from .measure import judge
        verdicts = judge.judge(ann, cat, limit=args.judge_limit)
    run_id = rows[0]["run_id"] if rows else ""
    md = report.render(cat, m, gap_list, verdicts, run_id)
    out = path.with_suffix(".report.md")
    out.write_text(md, encoding="utf-8")
    path.with_suffix(".report.json").write_text(
        json.dumps({"metrics": m, "gaps": gap_list, "verdicts": verdicts}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}")


def cmd_cards(args):
    from .content import cards
    gap_list = json.loads(Path(args.report_json).read_text(encoding="utf-8"))["gaps"]
    paths = cards.draft(_cat(args), gap_list, Path(args.out), args.limit)
    print(f"wrote {len(paths)} drafts to {args.out}; review them, then move good ones to content/approved/")


def cmd_indexnow(args):
    from .publish import indexnow
    print("IndexNow status", indexnow.submit(_cat(args), args.key, args.url or None))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="geo", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", default="catalog.yaml")
    sub = ap.add_subparsers(required=True)

    p = sub.add_parser("build"); p.set_defaults(fn=cmd_build)
    p.add_argument("--out", default="site"); p.add_argument("--cards", default="content/approved")
    p.add_argument("--checkout", action="store_true", help="mark products eligible for ChatGPT checkout")

    p = sub.add_parser("panel"); p.set_defaults(fn=cmd_panel)
    p.add_argument("--n", type=int, default=40); p.add_argument("--out", default="panels/v1.jsonl")
    p.add_argument("--demand-csv", help="demand-radar opportunities export (title, brief_md)")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("probe"); p.set_defaults(fn=cmd_probe)
    p.add_argument("--panel", default="panels/v1.jsonl"); p.add_argument("--samples", type=int, default=3)
    p.add_argument("--engines", nargs="*"); p.add_argument("--limit", type=int, default=0)
    p.add_argument("--workers", type=int, default=4); p.add_argument("--out", default="runs")

    p = sub.add_parser("report"); p.set_defaults(fn=cmd_report)
    p.add_argument("probe"); p.add_argument("--panel", default="panels/v1.jsonl")
    p.add_argument("--cards", default="content/approved")
    p.add_argument("--judge", action="store_true", help="audit answers that mention us for factual errors")
    p.add_argument("--judge-limit", type=int, default=50)

    p = sub.add_parser("cards"); p.set_defaults(fn=cmd_cards)
    p.add_argument("report_json"); p.add_argument("--out", default="content/drafts"); p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("indexnow"); p.set_defaults(fn=cmd_indexnow)
    p.add_argument("--key", required=True); p.add_argument("--url", nargs="*")

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
