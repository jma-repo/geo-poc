# geo: generative engine optimization toolkit

Get your products named and cited when people ask ChatGPT, Claude, Perplexity, Gemini or Google AI Mode
for a recommendation. Then measure whether it is working.

No tool can guarantee placement in AI answers. Each engine picks sources its own way and changes often. What you
*can* control is (1) whether engines are able to find and read your facts, (2) whether those facts are the best
answer to the questions people actually ask, and (3) whether trusted third parties talk about you. This toolkit
automates the parts of all three that can be automated, and measures the result with statistics instead of
one-off screenshots.

## The method

```
                ┌──────────── catalog.yaml (single source of truth) ────────────┐
                ▼                                ▼                                ▼
   1. ACCESS: can engines use it?    2. ANSWER: is it the best answer?    3. MEASURE: is it working?
   ─────────────────────────────     ──────────────────────────────────   ─────────────────────────────
   static HTML, no JS needed         answer-first pages                   prompt panel (personas × intents)
   schema.org JSON-LD @graph         facts with sources, specs, FAQs      + query fan-out per prompt
   OpenAI commerce product feed      answer cards for gap questions,      4 engines with live web search
   Google Merchant Center feed         grounded and number-checked        N samples each, Wilson 95% CIs
   robots.txt for AI crawlers        visible "last updated" dates         mention, citation, share of voice
   sitemap with lastmod, IndexNow                                         retrieved-but-not-cited pages
   llms.txt                                                               third-party sources to pitch
                                                                          accuracy audit of what AI says
                ▲                                                                 │
                └─────────────────── weekly loop: fix, publish, re-measure ───────┘
```

### 1. Access: make your facts machine-readable everywhere engines look

| Output | Why |
|---|---|
| `feeds/openai-products.jsonl` | ChatGPT shopping ingests merchant product feeds directly. Uses the field names from OpenAI's commerce spec (`item_id`, `title`, `description`, `url`, `brand`, `seller_name`, `image_url`, `availability`, `price`, `target_countries`, `store_country`). `--checkout` marks products eligible for in-chat checkout. |
| `feeds/google-merchant.tsv` | Merchant Center feeds Shopping, AI Overviews, AI Mode and Gemini. Google's Universal Commerce Protocol (UCP) checkout also builds on Merchant Center listings. OpenAI also accepts Google-style feeds. |
| JSON-LD `@graph` on every page | `Product` + `Offer` (price, availability, shipping regions, return policy) + `Organization` (with `sameAs` profiles for entity resolution) + `FAQPage` + `WebPage.dateModified`, all linked by `@id`. Ratings are emitted only when real reviews exist: the catalog refuses a rating without a review count. |
| Server-rendered HTML | Most AI crawlers do not execute JavaScript. Every fact is in the HTML text, not only in JSON-LD. |
| `robots.txt` | Explicitly allows AI search and retrieval bots (`OAI-SearchBot`, `ChatGPT-User`, `PerplexityBot`, `Perplexity-User`, `Claude-SearchBot`, `Claude-User`, and others). Blocking them removes you from answers. Training crawlers (`GPTBot`, `ClaudeBot`, `Google-Extended`, …) are a separate switch: `allow_ai_training`. |
| `sitemap.xml` + `geo indexnow` | Freshness matters in AI answers. IndexNow pushes changed URLs to Bing immediately, and Bing's index is a major retrieval source for ChatGPT search and Copilot. |
| `llms.txt`, `llms-full.txt` | Cheap to publish. No major engine has confirmed it as an input, so it is not relied on. |

### 2. Answer: be the most quotable source for the questions that matter

- **Answer-first layout.** The page opens with a one-sentence direct answer (`summary`), then checkable facts.
  Engines lift short, self-contained passages.
- **Facts with sources.** `claims` render with a link to their source. Adding statistics, citations and
  quotations was the most effective content change in the original GEO study (Aggarwal et al., KDD 2024).
- **Answer cards for gaps.** `geo cards` takes the questions where competitors appear and you do not, and
  drafts a short section answering the question *and* the sub-queries engines run for it. Two guards keep
  drafts honest: Claude is told to use catalog facts only, and a deterministic check rejects any card
  containing a number that is not in the catalog (invented prices, specs, percentages). Drafts go to
  `content/drafts/`. A person reviews them and moves good ones to `content/approved/`, and `geo build`
  refuses to publish a card that failed the check.

### 3. Measure: statistics, not screenshots

- **Prompt panel, not keywords.** `geo panel` writes realistic, conversational questions across personas and
  four intents (informational, comparison, transactional, local). It never names your brand or
  competitors, because the panel measures whether the AI brings them up. It can be seeded from
  `demand-radar` opportunities so you measure the needs real people expressed. Keep a panel fixed across
  runs; add a new version instead of editing one.
- **Query fan-out.** Engines split a question into several web searches and answer from the combined
  results. Each panel prompt stores its likely sub-queries. The report lists the ones your content does not
  cover. Those are what you need to publish.
- **Multiple engines, live search, local context.** Claude (web search tool), OpenAI (Responses API
  `web_search`), Perplexity (Sonar) and Gemini (grounding with Google Search). Location is set to Calgary,
  Alberta where the API supports it. Engines cite different sources, so results are reported per engine.
- **Repeated sampling with confidence intervals.** The same prompt can name different brands on different
  runs. Each (prompt, engine) pair is sampled N times, and mention rates carry Wilson 95% intervals. Do not
  act on differences that sit inside the intervals.
- **Cited vs retrieved.** An answer can fetch your page and still cite someone else. The report counts these
  "retrieved but not cited" cases: the page was found but lost on content.
- **Earned media targets.** Third-party domains that engines cite for your questions (forums, review sites,
  local guides). Getting covered there is usually the fastest way into AI answers.
- **Accuracy audit.** `geo report --judge` checks every answer that mentions you against the catalog and lists
  wrong prices, specs or policies by severity. Being visible with wrong facts costs sales.

## Quick start

```bash
cd geo
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
cp catalog.example.yaml catalog.yaml       # describe your brand, competitors and products
cp .env.example .env && set -a && . ./.env && set +a

geo build                                   # -> site/ (pages, JSON-LD, feeds, robots, sitemap, llms.txt)
geo panel --n 40                            # -> panels/v1.jsonl (review it before using it)
geo probe --samples 3                       # -> runs/probe-<timestamp>.jsonl
geo report runs/probe-<timestamp>.jsonl --judge   # -> .report.md and .report.json
geo cards runs/probe-<timestamp>.report.json      # -> content/drafts/*.json for review
geo indexnow --key <your-indexnow-key>      # after deploying changes
```

Deploy `site/` to your domain, or copy the JSON-LD and copy blocks into an existing store theme. Upload the
feeds in Merchant Center and the OpenAI merchant portal.

Using the demand-radar data: export its `opportunities` table to CSV (`title`, `brief_md` columns), then run
`geo panel --demand-csv opportunities.csv`.

## Configuration

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Panel generation, judging, answer cards, and the Claude probe engine |
| `GEO_CLAUDE_MODEL`, `GEO_CLAUDE_EFFORT` | Model and effort for the toolkit's own reasoning (default `claude-opus-5-5`, `medium`) |
| `GEO_PROBE_CLAUDE_MODEL`, `GEO_PROBE_EFFORT` | Model and effort for the Claude probe (default `claude-opus-5-5`, `low`) |
| `OPENAI_API_KEY` + `GEO_OPENAI_MODEL` | Enables the OpenAI probe. Set the model you want to stand in for ChatGPT. |
| `PERPLEXITY_API_KEY` (+ `GEO_PERPLEXITY_MODEL`, default `sonar`) | Enables the Perplexity probe |
| `GEMINI_API_KEY` + `GEO_GEMINI_MODEL` | Enables the Gemini probe |

An engine is enabled only when its key (and, where listed, model) is set. Claude requests opt in to
server-side refusal fallbacks (`fallbacks: "default"`), so a declined request is retried on a fallback model.

### Cost

A probe makes `prompts × engines × samples` searched answers. 40 prompts × 4 engines × 3 samples = 480 answers
per run. Start with `--limit 10 --samples 2` to check cost, then scale up. Weekly runs are enough: AI answers
shift over weeks, not hours.

## Weekly loop

1. `geo probe` and `geo report --judge` on the same panel.
2. Fix high-severity inaccuracies in `catalog.yaml` first, then rebuild and re-upload feeds.
3. `geo cards` for the top gaps. Review, approve, `geo build`, deploy, `geo indexnow`.
4. Pitch the top third-party domains with real samples and data. Disclose any paid relationship.
5. Compare next week's report with this one. Trust changes that move outside the confidence intervals.

## Tests

```bash
pytest
```

The tests cover the catalog rules, every publisher output (including JSON-LD `@id` linking and script-tag
escaping), each engine's response parsing, mention detection, statistics, gap detection, the grounding check,
and the real `anthropic` SDK's request serialization through a mock transport. They make no network calls.

## Limits

- **Not yet run against live APIs.** Engine adapters follow each provider's documented response format and
  are tested against recorded-shape fixtures. Run `geo probe --limit 2 --samples 1` first and check the raw
  JSONL before a full run.
- API answers approximate, but do not equal, what the consumer apps show (memory, personalization,
  shopping UI, ads). Use them as a consistent proxy and watch trends.
- Feed formats change. The OpenAI field names were checked against the spec in November 2026; check the
  spec again before submitting. In-chat checkout (OpenAI) and UCP checkout (Google) need separate merchant
  approval and payment integration, which this toolkit does not provide.
- Brand mention detection is alias-based. Add common misspellings to `aliases`, and avoid aliases that are
  ordinary words.
- Fan-out coverage uses term overlap. It flags candidates for a person to check; it is not a semantic judgment.
- Platform rules apply. Do not create fake reviews, astroturf forums, or hide text for crawlers. Engines
  and platforms penalize these, and some are illegal.

## References

- Aggarwal et al., *GEO: Generative Engine Optimization*, KDD 2024. arXiv:2311.09735
- *From Citation Selection to Citation Absorption: A Measurement Framework for GEO Across AI Search Platforms*, 2026. arXiv:2604.25707
- *Optimizing Visibility in Generative Engines: A Critical Survey of GEO (2023–2026)*. arXiv:2607.14035
- OpenAI commerce product feed spec: https://developers.openai.com/commerce/specs/file-upload/products
- Google Merchant Center, Universal Commerce Protocol: https://support.google.com/merchants/answer/16837055
- IndexNow: https://www.indexnow.org · llms.txt: https://llmstxt.org · schema.org: https://schema.org/Product
