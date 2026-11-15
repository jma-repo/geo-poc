#!/usr/bin/env python3
"""把 workflows/*.json 里每个 Postgres 节点的 SQL 拿出来，在真实数据库上按业务顺序跑一遍。

    DATABASE_URL=postgresql://postgres@localhost:5432/postgres python3 tests/test_sql.py

会新建并删除一个临时库 dr_sql_test。参数按文本传入，和 n8n queryReplacement 的行为一致。
"""
import csv
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:5432/postgres")
TEST_DB = "dr_sql_test"
_parts = urlsplit(BASE_URL)
DB_URL = urlunsplit(_parts._replace(path="/" + TEST_DB))


def psql(url, sql):
    r = subprocess.run(["psql", url, "-v", "ON_ERROR_STOP=1", "-q", "--csv", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return list(csv.DictReader(io.StringIO(r.stdout)))


QUERIES = {}
for wf in sorted((ROOT / "workflows").glob("*.json")):
    for node in json.loads(wf.read_text())["nodes"]:
        if node["type"] == "n8n-nodes-base.postgres":
            QUERIES[(wf.stem[:2], node["name"])] = node["parameters"]["query"]


def run(key, *params):
    q = QUERIES[key].rstrip().rstrip(";")
    n = max([int(x) for x in re.findall(r"\$(\d+)", q)] or [0])
    assert n == len(params), f"{key}: query wants {n} params, got {len(params)}"
    lit = lambda v: "$v$" + str(v) + "$v$"
    if not n:
        return psql(DB_URL, q)
    stmt = f"PREPARE q({', '.join(['text'] * n)}) AS {q};\nEXECUTE q({', '.join(map(lit, params))});"
    return psql(DB_URL, stmt)


checks = 0


def check(cond, msg):
    global checks
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    checks += 1
    print("ok  ", msg)


def main():
    psql(BASE_URL, f"DROP DATABASE IF EXISTS {TEST_DB}")
    psql(BASE_URL, f"CREATE DATABASE {TEST_DB}")
    try:
        psql(DB_URL, (ROOT / "db/schema.sql").read_text())
        psql(DB_URL, (ROOT / "db/schema.sql").read_text())  # 幂等

        signals = [{"source": "reddit", "external_id": f"t{i}", "url": f"https://r/{i}",
                    "title": f"title {i}", "body": "body, with commas", "author_hash": "ab",
                    "geo_hint": "calgary" if i < 2 else None,
                    "posted_at": "2026-11-01T10:00:00.000Z"} for i in range(4)]
        r = run(("01", "Insert Raw Signals"), json.dumps(signals))
        check(r[0]["inserted"] == "4", "01 插入 4 条原始信号")
        r = run(("02", "Insert Raw Signals"), json.dumps(signals))
        check(r[0]["inserted"] == "0", "02 重复信号被去重")

        r = run(("03", "Fetch Unprocessed"))
        check(len(r) == 4, "03 取出 4 条未处理信号")
        ids = [int(x["id"]) for x in r]
        pains = [{"signal_id": ids[i], "cluster_key": "car-hail-protection",
                  "pain": f"hail dented my car {i}", "context": "summer", "audience": "drivers",
                  "quote": "it's the third time", "intent_level": 4, "product_hint": "hail cover",
                  "geo": "calgary"} for i in range(3)]
        r = run(("03", "Save Pain Points"), json.dumps(pains), json.dumps(ids[:3]))
        check(r[0] == {"pain_points_inserted": "3", "signals_processed": "3"}, "03 写入痛点并标记已处理")
        check(len(run(("03", "Fetch Unprocessed"))) == 1, "03 失败批次保持未处理，下次重试")

        r = run(("04", "Aggregate Clusters"))
        check(len(r) == 1 and r[0]["mentions"] == "3" and r[0]["local_mentions"] == "3",
              "04 聚合出 1 个 cluster，3 次提及")
        opp = [{"week_of": "2026-11-09", "cluster_key": "car-hail-protection", "title": "Hail cover",
                "scores": {"demand": 8}, "total_score": 71.5, "brief_md": "# brief",
                "evidence": {"urls": ["https://r/0"]}}]
        r = run(("04", "Upsert Opportunities"), json.dumps(opp))
        opp_id = r[0]["id"]
        opp[0]["total_score"] = 75
        r = run(("04", "Upsert Opportunities"), json.dumps(opp))
        check(r[0]["id"] == opp_id and r[0]["total_score"] == "75.00", "04 同周同 cluster 覆盖更新")

        m = psql(DB_URL, "INSERT INTO merchants (name, kind, language, categories) VALUES "
                         "('Ningbo Plastics', 'factory', 'zh', ARRAY['auto']), "
                         "('YYC Auto', 'local_shop', 'en', NULL) RETURNING id")
        mids = [x["id"] for x in m]
        r = run(("05", "Fetch Opportunity & Merchants"), opp_id, json.dumps([int(i) for i in mids]))
        check(len(r) == 2 and {x["language"] for x in r} == {"zh", "en"}, "05 取出机会和 2 个商家")
        drafts = [{"opportunity_id": int(opp_id), "merchant_id": int(mids[0]), "language": "zh",
                   "subject": "合作", "body": "您好"}]
        r = run(("05", "Save Drafts"), json.dumps(drafts))
        check(r[0]["status"] == "pending_review", "05 外联草稿默认待审核")

        psql(DB_URL, f"INSERT INTO partner_codes (code, merchant_id, opportunity_id, commission_rate) "
                     f"VALUES ('HAIL10', {mids[0]}, {opp_id}, 0.1)")
        r = run(("06", "Record Conversion"), "HAIL10", "1001", "129.99", "CAD", mids[0])
        check(json.loads(r[0]["result"])["commission"] == 13.00, "06 记录佣金 129.99 × 10% = 13.00")
        r = run(("06", "Record Conversion"), "HAIL10", "1001", "129.99", "CAD", mids[0])
        check(json.loads(r[0]["result"]) == {"recorded": False}, "06 同一订单不重复计佣")
        r = run(("06", "Record Conversion"), "HAIL10", "1002", "50.00", "CAD", mids[1])
        check(json.loads(r[0]["result"]) == {"recorded": False}, "06 折扣码与商家不匹配时拒绝")

        r = run(("07", "Insert Waitlist"), opp_id, "h1", "a@b.ca", "true", "landing")
        check(r[0]["ok"] == "t", "07 等候名单写入")
        check(run(("07", "Insert Waitlist"), opp_id, "h1", "a@b.ca", "true", "landing") == [],
              "07 同一邮箱不重复")
        run(("07", "Insert Waitlist"), "", "h2", "", "false", "landing")
        check(run(("07", "Insert Waitlist"), "", "h2", "", "false", "landing") == [],
              "07 无机会 ID 时也去重，未同意不存邮箱")
        row = psql(DB_URL, "SELECT email IS NULL AS no_email FROM waitlist WHERE email_hash='h2'")
        check(row[0]["no_email"] == "t", "07 未同意时 email 为 NULL")
    finally:
        psql(BASE_URL, f"DROP DATABASE IF EXISTS {TEST_DB}")
    covered = {k for k in QUERIES}
    print(f"\n{checks} checks passed, {len(covered)} SQL nodes exercised")


if __name__ == "__main__":
    main()
