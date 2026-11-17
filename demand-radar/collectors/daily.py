#!/usr/bin/env python3
"""Daily collection into a separate (private) data repository.

  python3 -m collectors.daily --data-dir ../demand-data [--commit]

Writes signals/YYYY-MM-DD.jsonl (Calgary date; a second run that day gets -HHMM, never overwriting) and appends one line per source to runs.log. With --commit it commits
and pushes the data repository. A two-day lookback overlaps the previous run so nothing falls between
runs; duplicates across days are removed at analysis time.
Collected text belongs to its authors: keep the data repository private.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import yaml

from .run import HERE, collect
from .sources import Http


def git(data_dir: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(data_dir), *args], check=True, capture_output=True, text=True).stdout


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--config", type=Path, default=HERE / "config.yaml")
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--commit", action="store_true", help="git commit and push the data repository")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    sources = cfg.get("daily_sources") or ["arctic_shift", "stackexchange", "autocomplete"]
    now = datetime.now(timezone.utc)
    rows, report = collect(sources, cfg, args.days, Http(1.0))

    local = now.astimezone(ZoneInfo("America/Edmonton"))
    out = args.data_dir / "signals" / f"{local:%Y-%m-%d}.jsonl"
    if out.exists():  # a second run on the same day must not overwrite the first
        out = out.with_name(f"{local:%Y-%m-%d-%H%M}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    with (args.data_dir / "runs.log").open("a", encoding="utf-8") as log:
        for name, status in report.items():
            log.write(f"{now:%Y-%m-%dT%H:%MZ}\t{name}\t{status}\n")
    for name, status in report.items():
        print(f"{name:14s} {status}")
    print(f"wrote {len(rows)} rows to {out}")

    if args.commit:
        git(args.data_dir, "add", "signals", "runs.log")
        if not git(args.data_dir, "status", "--porcelain"):
            print("nothing to commit")
            return
        git(args.data_dir, "commit", "-q", "-m", f"Signals {out.stem}: {len(rows)} rows")
        for attempt in range(4):
            try:
                git(args.data_dir, "push", "-q", "origin", "HEAD")
                print("pushed")
                return
            except subprocess.CalledProcessError as e:
                if attempt == 3:
                    raise SystemExit(f"push failed: {e.stderr.strip()}")
                time.sleep(2 ** (attempt + 1))


if __name__ == "__main__":
    main()
