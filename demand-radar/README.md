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
DATABASE_URL=postgresql://radar:pass@localhost:5432/radar python3 tests/test_sql.py
                                   # runs every SQL query against real Postgres in a temporary database
```

## Known limits

- **Not yet imported into a live n8n instance.** Node parameters follow the n8n 1.x format. If a node
  reports a parameter error after import, reselect the option in the UI.
- Postgres nodes pass query parameters as an array expression `{{ [ ... ] }}` so JSON containing commas is
  passed safely. If your n8n version does not accept the array form, switch to the string form.
- Not yet run with real Claude, Reddit or Apify keys.
- **Reddit's Data API requires written permission for commercial use.** Apply before scaling. Check that any
  Apify scraping complies with the target platform's terms.
- `06` authenticates with a header secret. Native Shopify webhooks cannot add custom headers; verify
  `X-Shopify-Hmac-Sha256` instead when integrating Shopify directly.
- `07` is a public endpoint. Add a CAPTCHA or rate limiting before launch.
- Canada's anti-spam law (CASL): plain-text emails are stored only with explicit consent. Outreach and
  marketing email must comply as well.
