# geo-poc

A proof of concept for a demand-first product business in Calgary: find what people want, connect that demand
to manufacturers, and make the resulting products show up when people ask AI assistants for recommendations.

| Module | What it does | Stack |
|---|---|---|
| [`demand-radar/`](demand-radar/) | Collects buying signals (Reddit posts, low-star Amazon.ca reviews), turns them into scored product opportunities, drafts merchant outreach, and tracks commissions on referred sales. | n8n, Postgres, Claude API |
| [`geo/`](geo/) | Generative engine optimization: publishes AI-readable product data (pages, JSON-LD, ChatGPT and Google feeds, crawler files) and measures visibility across Claude, ChatGPT, Perplexity and Gemini with repeated sampling. | Python, Claude API |

## How they fit together

```
demand-radar                                  geo
────────────                                  ───
signals → pain points → opportunities ──CSV──► prompt panel (the questions buyers ask AI)
                    │                                │
                    ▼                                ▼
          merchant outreach, commissions      probe engines → report → fixes → re-measure
                                                     ▲
                    product catalog ─────────────────┘ (pages, structured data, feeds)
```

1. `demand-radar` finds a need, for example hail protection for cars parked outside in Calgary.
2. A merchant or factory builds or stocks the product; `demand-radar` tracks the referral commission.
3. `geo` publishes the product's facts where AI engines read them, then measures whether the engines
   recommend it for the same questions buyers asked in step 1.

## Getting started

- `demand-radar`: see [demand-radar/README.md](demand-radar/README.md) (Docker Compose, n8n workflow import).
- `geo`: see [geo/README.md](geo/README.md) (Python 3.10+, `pip install -e geo`).

## Tests

```bash
cd geo && pip install -e '.[test]' && pytest                  # 32 tests
cd demand-radar && node tests/test_code_nodes.mjs             # 15 tests, all n8n Code nodes
DATABASE_URL=postgresql://... python3 demand-radar/tests/test_sql.py   # 16 checks against real Postgres
```

## Status

Proof of concept. All logic is covered by offline tests. Nothing has been run yet against live third-party APIs
(Claude, OpenAI, Perplexity, Gemini, Reddit, Apify) or inside a running n8n instance. Each module's README
lists its known limits.
