"""Run the panel against every engine, several times each.

AI answers are non-deterministic: the same prompt can name different brands on different runs.
One sample per prompt is noise; the toolkit samples each (prompt, engine) pair and reports rates
with confidence intervals.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .engines import Engine


def run(prompts: list[dict], engines: list[Engine], samples: int, out: Path, workers: int = 4) -> Path:
    if not engines:
        raise SystemExit("No engines configured. Set API keys (see README).")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"probe-{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    jobs = [(p, e, s) for p in prompts for e in engines for s in range(samples)]

    def one(job):
        p, e, s = job
        ans = e.ask(p["text"])
        return {"run_id": run_id, "prompt_id": p["id"], "intent": p.get("intent"), "sample": s,
                "at": datetime.now(timezone.utc).isoformat(), **ans.to_dict()}

    with ThreadPoolExecutor(workers) as pool, path.open("w", encoding="utf-8") as f:
        for row in pool.map(one, jobs):
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
