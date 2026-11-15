# Demand Radar

The demand side only: automatically find consumer needs, turn them into opportunity briefs, and sell or refer
that demand to factories and merchants for a commission. No inventory, no manufacturing, no retail.

The business model and the anti-circumvention design are in [docs/business-model.md](docs/business-model.md)
(Chinese).

## Pipeline

```
01 Reddit "looking for" posts ─┐
02 Amazon.ca low-star reviews ─┼─► raw_signals ─► 03 Claude extracts pain points ─► pain_points
   (add more sources)          ┘                                                   │
                                                                                   ▼
                          04 weekly clustering and scoring ─► opportunities ─► email digest to you
                                                                                   │
                     you pick an opportunity and merchants ─► 05 outreach drafts (pending review) ─► you send
                                                                                   │
    07 landing-page waitlist ─► waitlist (buyers you own)                          ▼
                                                     merchant sale ─► 06 commission ledger (conversions)
```

| Workflow | Trigger | What it does |
|---|---|---|
| `01-collect-reddit` | Daily 07:00 | Searches target subreddits for "recommend", "where can I buy", "wish there was" posts |
| `02-collect-apify-reviews` | Mondays 06:00 | Pulls 1–3 star competitor reviews through an Apify actor |
| `03-analyze-signals` | Every 6 hours | Claude turns raw text into structured pain points and clusters them |
| `04-weekly-scoring` | Mondays 08:00 | Aggregates 30 days, Claude scores five dimensions and writes briefs, code applies the weights, emails a digest |
| `05-merchant-outreach-drafts` | `POST /webhook/outreach` | Drafts outreach emails (English or Chinese) for chosen merchants. **Drafts only, never sent automatically.** |
| `06-commission-tracking` | `POST /webhook/conversion` | Merchants report orders; commissions are recorded by dedicated discount code |
| `07-waitlist-signup` | `POST /webhook/waitlist` | Landing-page waitlist: the buyers you own, which is your moat |

All schedules use the `America/Edmonton` time zone.

## Quick start

```bash
cp .env.example .env          # set passwords and the digest recipient
docker compose up -d          # starts Postgres (tables created automatically) and n8n
# open http://localhost:5678 and create the n8n owner account
docker compose exec n8n n8n import:workflow --separate --input=/workflows
```

After importing, create these credentials in n8n under **Credentials** and select them in each node:

| Credential | n8n type | Used by | Values |
|---|---|---|---|
| Radar DB | Postgres | All Postgres nodes | host `postgres`; database, user and password from `.env` |
| Anthropic API | Header Auth | Claude Extract / Score / Draft | Name `x-api-key`, Value your API key |
| Reddit App | Basic Auth | Reddit Token | Create a script app at reddit.com/prefs/apps; client_id and secret |
| Apify | Header Auth | Run Actor | Name `Authorization`, Value `Bearer <APIFY_TOKEN>` |
| Webhook Key | Header Auth | POST /outreach, POST /conversion | A header name and long random value of your choice; give it to merchants for order reporting |
| SMTP | SMTP | Email Digest | Your mail provider's SMTP settings |

Run `03 → 01 → 04` once by hand with **Execute workflow**, check the results, then switch them to **Active**.

### Day-to-day

```bash
# add a merchant
docker compose exec postgres psql -U radar -d radar -c \
  "INSERT INTO merchants (name, kind, country, language, categories, contact_email)
   VALUES ('Ningbo plastics factory', 'factory', 'CN', 'zh', ARRAY['auto'], 'sales@example.cn');"

# draft outreach for opportunity 7 to merchants 1 and 2
curl -X POST http://localhost:5678/webhook/outreach \
  -H 'X-Radar-Key: <your Webhook Key value>' -H 'Content-Type: application/json' \
  -d '{"opportunity_id": 7, "merchant_ids": [1, 2]}'

# after signing, give the merchant a dedicated discount code at 10% commission
docker compose exec postgres psql -U radar -d radar -c \
  "INSERT INTO partner_codes (code, merchant_id, opportunity_id, commission_rate) VALUES ('HAIL10', 1, 7, 0.10);"

# the merchant (or their store) reports an order
curl -X POST http://localhost:5678/webhook/conversion \
  -H 'X-Radar-Key: ...' -H 'Content-Type: application/json' \
  -d '{"merchant_id": 1, "order_id": "1001", "amount": 129.99, "code": "HAIL10"}'
```

To feed the `geo` module, export opportunities:

```bash
docker compose exec postgres psql -U radar -d radar --csv \
  -c "SELECT title, brief_md FROM opportunities ORDER BY total_score DESC LIMIT 30" > opportunities.csv
```

## Free collectors (no n8n needed)

`collectors/` is a small Python alternative to workflows `01` and `02` that uses only free sources. It writes
the same `raw_signals` rows, so workflow `03` analyzes them unchanged.

```bash
pip install httpx pyyaml
python3 -m collectors.run --check                     # one small request per source; shows what works
python3 -m collectors.run --days 7 --database-url postgresql://radar:...@localhost:5432/radar
```

Edit `collectors/config.yaml` for subreddits, phrases, topics and seeds. Schedule it with cron, e.g. daily:
`0 7 * * * cd /path/to/demand-radar && python3 -m collectors.run --days 2 --database-url ...`

| Source | Collector | Cost | Key | Limits and notes |
|---|---|---|---|---|
| Reddit via [Arctic Shift](https://github.com/ArthurHeitmann/arctic_shift) | `arctic_shift` | Free | None | Open-source, community-run archive of public Reddit data. Its full-text search times out, so the collector pages through recent posts and comments (newest first, `reddit_max_pages` × 100 per subreddit) and matches `reddit_queries` locally. Commercial use of Reddit data is subject to Reddit's terms. |
| Hacker News via Algolia | `hn` | Free | None | About 10,000 requests per hour. Skews toward tech buyers. |
| Stack Exchange (diy, outdoors, pets, mechanics…) | `stackexchange` | Free | Optional | 300 requests/day without a key, 10,000 with a free key from stackapps.com. Content is CC BY-SA: attribute if you republish. |
| Google autocomplete | `autocomplete` | Free | None | Unofficial endpoint, no SLA. Shows what people type next. Keep volume low. |
| YouTube comments | `youtube` | Free | `YOUTUBE_API_KEY` | Google Cloud project, enable YouTube Data API v3, create an API key. 10,000 units/day; a search costs 100, a comment page 1. |

Bluesky is not included: its public search endpoint returns 403 without a login (checked November 2026).
It needs a free account and app password.

Each source runs independently, so one failure never stops the others. Requests are spaced (`--delay`,
default 1 s) and retried on 429/5xx. Usernames are never stored, only a short hash.

### Daily collection into a private data repository

Collected posts are other people's text, so keep them out of public repositories. Create a private
repository for data (for example `demand-data`), clone it next to this one, and run once a day:

```bash
python3 -m collectors.daily --data-dir ../demand-data --commit
```

Each run writes `signals/YYYY-MM-DD.jsonl` with a two-day lookback (overlap is removed at analysis time),
appends per-source status to `runs.log`, and commits and pushes. `daily_sources` in `config.yaml` picks the
sources. Analyze any period by pointing the pipeline at the directory:
`python3 -m analyze.pipeline ../demand-data/signals --out results/`.

## Analysis without n8n (DeepSeek or Claude)

`analyze/` runs the same steps as workflows `03` and `04` on collector output, with either model provider.

```bash
export LLM_PROVIDER=deepseek DEEPSEEK_API_KEY=...      # or LLM_PROVIDER=claude ANTHROPIC_API_KEY=...
python3 -m analyze.pipeline signals.jsonl --out results/
# -> results/report.md, opportunities.json, pain_points.jsonl
```

| Provider | Default model | Structured output |
|---|---|---|
| `deepseek` | `deepseek-flash` (`DEEPSEEK_MODEL=deepseek-v4-pro` for the larger model) | JSON mode only, so the schema goes in the prompt and every reply is validated; an invalid reply is retried once with the error |
| `claude` | `claude-opus-5-5` (`CLAUDE_MODEL`) | Native JSON Schema structured outputs |

Compared with the n8n workflows it adds one step: extraction batches name clusters independently, so the
model merges synonymous cluster keys before aggregation and scoring. Autocomplete suggestions are folded into
one signal per seed phrase. Scoring weights are applied in code, as in workflow `04`.

## Customizing

- **Communities and phrases to watch:** the `Build Queries` node in `01`
- **Competitor products to mine:** the `Apify Jobs` node in `02`
- **Prompts:** `prompts/*.md`
- **Scoring weights:** the `Weight Scores` node in `04` (demand 30%, margin 25%, gap 20%, logistics 15%, compliance 10%)
- **Model:** `CLAUDE_MODEL` in `.env`. The default is `claude-opus-5-5`. Pain-point extraction is the
  highest-volume step, so `claude-haiku-5-5` costs much less there; compare both on the same batch first.

The workflow JSON files are generated by `scripts/build_workflows.py` from Python and the prompt files. Either
edit in the n8n UI and export over `workflows/*.json`, or edit the script, run
`python3 scripts/build_workflows.py`, and re-import. Do not mix the two, or they will overwrite each other.

## Tests

```bash
node tests/test_code_nodes.mjs    # runs every Code node in a mocked n8n runtime and checks the wiring
python3 -m pytest                  # collectors: parsing per source, retries, isolation; Postgres load if TEST_DATABASE_URL is set
DATABASE_URL=postgresql://radar:pass@localhost:5432/radar python3 tests/test_sql.py
                                   # runs every SQL query against real Postgres in a temporary database
```

## Known limits

- **Not yet imported into a live n8n instance.** Node parameters follow the n8n 1.x format. If a node
  reports a parameter error after import, reselect the option in the UI.
- Postgres nodes pass query parameters as an array expression `{{ [ ... ] }}` so JSON containing commas is
  passed safely. If your n8n version does not accept the array form, switch to the string form.
- Not yet run with real Claude, Reddit or Apify keys. The free collectors have not been run against the live
  sources either: the development environment's network blocked those hosts. Run `--check` first.
- **Reddit's Data API requires written permission for commercial use.** Apply before scaling. Check that any
  Apify scraping complies with the target platform's terms.
- `06` authenticates with a header secret. Native Shopify webhooks cannot add custom headers; verify
  `X-Shopify-Hmac-Sha256` instead when integrating Shopify directly.
- `07` is a public endpoint. Add a CAPTCHA or rate limiting before launch.
- Canada's anti-spam law (CASL): plain-text emails are stored only with explicit consent. Outreach and
  marketing email must comply as well.
