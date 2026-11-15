#!/usr/bin/env python3
"""Run the free collectors and write raw_signals rows.

  python3 -m collectors.run --check                 # one small request per source: does it work from here?
  python3 -m collectors.run --days 7 --out signals.jsonl
  python3 -m collectors.run --days 7 --database-url postgresql://radar:...@localhost:5432/radar

Run from the demand-radar directory. Each source is independent: one failing source never stops the others.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from .sources import COLLECTORS, NEEDS_KEY, Http

HERE = Path(__file__).resolve().parent
INSERT_SQL = """
WITH ins AS (
  INSERT INTO raw_signals (source, external_id, url, title, body, author_hash, geo_hint, posted_at)
  SELECT r.source, r.external_id, r.url, r.title, r.body, r.author_hash, r.geo_hint, r.posted_at
  FROM jsonb_to_recordset(__ROWS__::jsonb) AS r(
    source text, external_id text, url text, title text, body text,
    author_hash text, geo_hint text, posted_at timestamptz)
  ON CONFLICT (source, external_id) DO NOTHING
  RETURNING id
)
SELECT count(*) FROM ins;
"""


def check_config(cfg: dict) -> dict:
    """Shrink the config to one query per source so --check costs a handful of requests."""
    small = {k: (v[:1] if isinstance(v, list) else v) for k, v in cfg.items()}
    return small


def collect(names: list[str], cfg: dict, days: int, http: Http, limit_per_source: int | None = None):
    rows, report = {}, {}
    for name in names:
        if name in NEEDS_KEY and not os.environ.get(NEEDS_KEY[name]):
            report[name] = f"skipped: set {NEEDS_KEY[name]}"
            continue
        n = 0
        try:
            for r in COLLECTORS[name](http, cfg, days):
                rows[(r["source"], r["external_id"])] = r
                n += 1
                if limit_per_source and n >= limit_per_source:
                    break
            report[name] = f"ok: {n} rows"
        except Exception as e:  # report and continue with the next source
            report[name] = f"error: {type(e).__name__}: {str(e)[:200]}"
    return list(rows.values()), report


def load_postgres(rows: list[dict], url: str, batch: int = 500) -> int:
    """Insert through psql's stdin (no size limit), as a dollar-quoted literal, in batches."""
    total = 0
    for i in range(0, len(rows), batch):
        data = json.dumps(rows[i:i + batch], ensure_ascii=False)
        tag = "$rows$"
        while tag in data:
            tag = tag[:-1] + "x$"
        sql = INSERT_SQL.replace("__ROWS__", f"{tag}{data}{tag}")
        r = subprocess.run(["psql", url, "-v", "ON_ERROR_STOP=1", "-tA", "-q"], input=sql, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stderr.strip())
        total += int(r.stdout.strip() or 0)
    return total


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--sources", nargs="*", default=list(COLLECTORS), choices=list(COLLECTORS))
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--check", action="store_true", help="tiny request per source and print what works")
    ap.add_argument("--out", help="write rows as JSONL")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if args.check:
        cfg = check_config(cfg)
    rows, report = collect(args.sources, cfg, args.days, Http(args.delay), limit_per_source=5 if args.check else None)
    for name, status in report.items():
        print(f"{name:14s} {status}")
    if args.check:
        for r in rows[:5]:
            print("  sample:", r["source"], "|", (r["title"] or r["body"])[:100].replace("\n", " "))
        sys.exit(0 if any(s.startswith("ok: ") and not s.startswith("ok: 0") for s in report.values()) else 1)
    if args.out:
        Path(args.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        print(f"wrote {len(rows)} rows to {args.out}")
    if args.database_url:
        print(f"inserted {load_postgres(rows, args.database_url)} new rows into raw_signals")


if __name__ == "__main__":
    main()
