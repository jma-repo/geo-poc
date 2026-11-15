#!/usr/bin/env python3
"""生成 workflows/*.json（n8n 可直接导入）。

工作流的唯一来源是本文件和 prompts/*.md。修改后运行：
    python3 scripts/build_workflows.py
"""
import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
OUT = ROOT / "workflows"

ANTHROPIC_VERSION = "2026-10-21"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------- 节点构造器

def _id(workflow: str, name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"demand-radar/{workflow}/{name}"))


class Flow:
    def __init__(self, slug: str, title: str):
        self.slug, self.title = slug, title
        self.nodes, self.connections = [], {}
        self._x = 0

    def add(self, name, type_, version, params, **extra):
        node = {
            "parameters": params,
            "id": _id(self.slug, name),
            "name": name,
            "type": type_,
            "typeVersion": version,
            "position": [self._x, 300],
        }
        node.update(extra)
        self.nodes.append(node)
        self._x += 240
        return name

    def chain(self, *names):
        for a, b in zip(names, names[1:]):
            self.connections.setdefault(a, {"main": [[]]})["main"][0].append(
                {"node": b, "type": "main", "index": 0}
            )

    def dump(self):
        wf = {
            "name": self.title,
            "nodes": self.nodes,
            "connections": self.connections,
            "settings": {"executionOrder": "v1", "timezone": "America/Edmonton"},
            "pinData": {},
            "active": False,
            "tags": [],
        }
        path = OUT / f"{self.slug}.json"
        path.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path


def schedule(flow, name, interval):
    return flow.add(name, "n8n-nodes-base.scheduleTrigger", 1.2, {"rule": {"interval": [interval]}})


def code(flow, name, js):
    return flow.add(name, "n8n-nodes-base.code", 2, {"mode": "runOnceForAllItems", "jsCode": js.strip() + "\n"})


def postgres(flow, name, query, params_expr=None, **extra):
    options = {"queryReplacement": params_expr} if params_expr else {}
    return flow.add(
        name, "n8n-nodes-base.postgres", 2.5,
        {"operation": "executeQuery", "query": query.strip(), "options": options},
        **extra,
    )


def webhook(flow, name, path, auth=True):
    params = {"httpMethod": "POST", "path": path, "responseMode": "lastNode", "options": {}}
    if auth:
        params["authentication"] = "headerAuth"
    return flow.add(name, "n8n-nodes-base.webhook", 2, params, webhookId=_id(flow.slug, path))


def claude_http(flow, name):
    """调用 Claude Messages API。请求体由上一个 Code 节点放在 $json.request。"""
    return flow.add(
        name, "n8n-nodes-base.httpRequest", 4.2,
        {
            "method": "POST",
            "url": "https://api.anthropic.com/v1/messages",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",  # 凭证：Header Name = x-api-key
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "anthropic-version", "value": ANTHROPIC_VERSION},
                {"name": "anthropic-beta", "value": FALLBACK_BETA},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify($json.request) }}",
            "options": {
                "timeout": 600000,
                "batching": {"batch": {"batchSize": 1, "batchInterval": 500}},
            },
        },
        retryOnFail=True, maxTries=3, waitBetweenTries=5000,
        onError="continueRegularOutput",
    )


# 所有调用 Claude 的 Code 节点共用的 JS 片段
CLAUDE_JS = r"""
const MODEL = $env.CLAUDE_MODEL || 'claude-opus-5-5';
const EFFORT = $env.CLAUDE_EFFORT || 'low';

function claudeRequest(system, userText, schema, maxTokens = 16000) {
  return {
    model: MODEL,
    max_tokens: maxTokens,
    system,
    fallbacks: 'default',
    output_config: { effort: EFFORT, format: { type: 'json_schema', schema } },
    messages: [{ role: 'user', content: userText }],
  };
}

function parseClaude(resp) {
  if (!resp || resp.error) throw new Error('API error: ' + JSON.stringify(resp && resp.error).slice(0, 500));
  if (resp.stop_reason === 'refusal') throw new Error('refused: ' + JSON.stringify(resp.stop_details));
  if (resp.stop_reason === 'max_tokens') throw new Error('output truncated at max_tokens');
  const text = (resp.content || []).filter(b => b.type === 'text').map(b => b.text).join('');
  return JSON.parse(text);
}
"""

HASH_JS = r"""
const crypto = require('crypto');
const hash = s => s ? crypto.createHash('sha256').update(String(s)).digest('hex').slice(0, 16) : null;
"""

INSERT_RAW_SQL = """
WITH ins AS (
  INSERT INTO raw_signals (source, external_id, url, title, body, author_hash, geo_hint, posted_at)
  SELECT r.source, r.external_id, r.url, r.title, r.body, r.author_hash, r.geo_hint, r.posted_at
  FROM jsonb_to_recordset($1::jsonb) AS r(
    source text, external_id text, url text, title text, body text,
    author_hash text, geo_hint text, posted_at timestamptz)
  ON CONFLICT (source, external_id) DO NOTHING
  RETURNING id
)
SELECT count(*) AS inserted FROM ins;
"""
ROWS_PARAM = "={{ [ JSON.stringify($json.rows) ] }}"


def str_schema(*keys):
    return {k: {"type": "string"} for k in keys}


# ---------------------------------------------------------------- 01 Reddit 抓取

def wf_reddit():
    f = Flow("01-collect-reddit", "01 采集 · Reddit 需求帖")
    t = schedule(f, "Every Day 07:00", {"triggerAtHour": 7})
    tok = f.add(
        "Reddit Token", "n8n-nodes-base.httpRequest", 4.2,
        {
            "method": "POST",
            "url": "https://www.reddit.com/api/v1/access_token",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpBasicAuth",  # 凭证：Reddit app 的 client_id / secret
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "User-Agent", "value": "={{ $env.REDDIT_USER_AGENT || 'demand-radar/0.1' }}"}]},
            "sendBody": True,
            "contentType": "form-urlencoded",
            "bodyParameters": {"parameters": [{"name": "grant_type", "value": "client_credentials"}]},
            "options": {},
        },
    )
    q = code(f, "Build Queries", r"""
// 在这里维护要监听的社区和“求购”句式
const SUBREDDITS = ['Calgary', 'Alberta', 'PersonalFinanceCanada', 'BuyCanadian',
                    'CampingandHiking', 'Skiing', 'dogs', 'HomeImprovement', 'BuyItForLife'];
const PHRASES = ['"looking for"', '"recommend"', '"where can I buy"', '"wish there was"',
                 '"does anyone know"', '"any suggestions"', '"best way to"'];
const token = $input.first().json.access_token;
if (!token) throw new Error('Reddit token missing: ' + JSON.stringify($input.first().json));
const q = PHRASES.join(' OR ');
return SUBREDDITS.map(sub => ({ json: { token, sub, q } }));
""")
    s = f.add(
        "Reddit Search", "n8n-nodes-base.httpRequest", 4.2,
        {
            "method": "GET",
            "url": "=https://oauth.reddit.com/r/{{ $json.sub }}/search",
            "sendQuery": True,
            "queryParameters": {"parameters": [
                {"name": "q", "value": "={{ $json.q }}"},
                {"name": "restrict_sr", "value": "1"},
                {"name": "sort", "value": "new"},
                {"name": "t", "value": "week"},
                {"name": "limit", "value": "100"},
            ]},
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": "=Bearer {{ $json.token }}"},
                {"name": "User-Agent", "value": "={{ $env.REDDIT_USER_AGENT || 'demand-radar/0.1' }}"},
            ]},
            "options": {"batching": {"batch": {"batchSize": 1, "batchInterval": 1100}}},
        },
        retryOnFail=True, maxTries=3, waitBetweenTries=5000,
        onError="continueRegularOutput",
    )
    n = code(f, "Normalize Posts", HASH_JS + r"""
const GEO = { calgary: 'calgary', alberta: 'alberta', personalfinancecanada: 'canada', buycanadian: 'canada' };
const seen = new Set();
const rows = [];
for (const item of $input.all()) {
  for (const c of (item.json.data && item.json.data.children) || []) {
    const d = c.data || {};
    if (!d.id || seen.has(d.id) || d.stickied || d.over_18) continue;
    seen.add(d.id);
    rows.push({
      source: 'reddit',
      external_id: d.id,
      url: d.permalink ? 'https://www.reddit.com' + d.permalink : null,
      title: String(d.title || '').slice(0, 500),
      body: String(d.selftext || '').slice(0, 8000),
      author_hash: hash(d.author),
      geo_hint: GEO[String(d.subreddit || '').toLowerCase()] || null,
      posted_at: d.created_utc ? new Date(d.created_utc * 1000).toISOString() : null,
    });
  }
}
return [{ json: { rows, count: rows.length } }];
""")
    p = postgres(f, "Insert Raw Signals", INSERT_RAW_SQL, ROWS_PARAM)
    f.chain(t, tok, q, s, n, p)
    return f.dump()


# ---------------------------------------------------------------- 02 Apify（Amazon 差评等）

def wf_apify():
    f = Flow("02-collect-apify-reviews", "02 采集 · Apify（Amazon.ca 差评等）")
    t = schedule(f, "Every Monday 06:00", {"field": "weeks", "triggerAtDay": [1], "triggerAtHour": 6})
    c = code(f, "Apify Jobs", r"""
// 每个 job = 一个 Apify Actor + 它的输入。Actor ID 和输入字段以 Apify 商店里该 Actor 的文档为准。
const JOBS = [
  {
    source: 'amazon_review',
    actor: $env.APIFY_REVIEWS_ACTOR || 'REPLACE_WITH_ACTOR_ID',
    input: {
      productUrls: [
        // 竞品链接：每个候选品类放 3-5 个 Amazon.ca 畅销款
        // { url: 'https://www.amazon.ca/dp/XXXXXXXXXX' },
      ],
      filterByRatings: ['oneStar', 'twoStar', 'threeStar'],
      maxReviews: 100,
    },
  },
];
return JOBS.filter(j => j.input.productUrls.length).map(j => ({ json: j }));
""")
    h = f.add(
        "Run Actor", "n8n-nodes-base.httpRequest", 4.2,
        {
            "method": "POST",
            "url": "=https://api.apify.com/v2/acts/{{ $json.actor.replace('/', '~') }}/run-sync-get-dataset-items",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",  # 凭证：Authorization = Bearer <APIFY_TOKEN>
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify($json.input) }}",
            "options": {"timeout": 300000},
        },
        onError="continueRegularOutput",
    )
    n = code(f, "Normalize Reviews", HASH_JS + r"""
// 不同 Actor 字段名不同，这里兼容常见写法；接新 Actor 时先看一条输出再调整
const jobs = $('Apify Jobs').all();
const rows = [];
$input.all().forEach(item => {
  const job = jobs[(item.pairedItem && item.pairedItem.item) || 0];
  const source = (job && job.json.source) || 'review';
  const r = item.json;
  const rating = Number(r.ratingScore ?? r.rating ?? r.stars ?? 0);
  if (rating > 3) return;
  const id = r.reviewId || r.id || hash((r.reviewUrl || '') + (r.reviewDescription || r.text || ''));
  rows.push({
    source,
    external_id: String(id),
    url: r.reviewUrl || r.url || null,
    title: String(r.reviewTitle || r.title || '').slice(0, 500),
    body: String(r.reviewDescription || r.text || r.body || '').slice(0, 8000),
    author_hash: hash(r.userId || r.reviewerName || r.author),
    geo_hint: 'canada',
    posted_at: r.date ? new Date(r.date).toISOString() : null,
  });
});
return [{ json: { rows, count: rows.length } }];
""")
    p = postgres(f, "Insert Raw Signals", INSERT_RAW_SQL, ROWS_PARAM)
    f.chain(t, c, h, n, p)
    return f.dump()


# ---------------------------------------------------------------- 03 Claude 提炼痛点

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "signal_id": {"type": "integer"},
                    "is_demand": {"type": "boolean"},
                    **str_schema("cluster_key", "pain", "context", "audience", "quote"),
                    "intent_level": {"type": "integer"},
                    **str_schema("product_hint", "geo"),
                },
                "required": ["signal_id", "is_demand", "cluster_key", "pain", "context", "audience",
                             "quote", "intent_level", "product_hint", "geo"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def wf_analyze():
    f = Flow("03-analyze-signals", "03 分析 · Claude 提炼痛点")
    t = schedule(f, "Every 6 Hours", {"field": "hours", "hoursInterval": 6})
    sel = postgres(f, "Fetch Unprocessed", """
SELECT id, source, title, body, geo_hint
FROM raw_signals
WHERE processed = false
ORDER BY id
LIMIT 60;
""")
    b = code(f, "Build Claude Requests", CLAUDE_JS + f"""
const SYSTEM = {json.dumps(prompt('extract_pain_points'), ensure_ascii=False)};
const SCHEMA = {json.dumps(EXTRACT_SCHEMA)};
""" + r"""
const signals = $input.all().map(i => i.json).filter(s => s.id);
const CHUNK = 10;
const out = [];
for (let i = 0; i < signals.length; i += CHUNK) {
  const chunk = signals.slice(i, i + CHUNK).map(s => ({
    signal_id: Number(s.id), source: s.source, geo_hint: s.geo_hint,
    title: s.title, body: String(s.body || '').slice(0, 2000),
  }));
  out.push({ json: {
    signal_ids: chunk.map(s => s.signal_id),
    request: claudeRequest(SYSTEM, 'Signals:\n' + JSON.stringify(chunk, null, 1), SCHEMA),
  }});
}
return out;
""")
    h = claude_http(f, "Claude Extract")
    parse = code(f, "Parse Pain Points", CLAUDE_JS + r"""
const reqs = $('Build Claude Requests').all();
const rows = [], done = [], errors = [];
const clean = (s, n) => String(s || '').trim().slice(0, n);
$input.all().forEach((item, i) => {
  const ids = (reqs[i] && reqs[i].json.signal_ids) || [];
  try {
    const out = parseClaude(item.json);
    const valid = new Set(ids);
    for (const p of out.items || []) {
      if (!valid.has(p.signal_id) || !p.is_demand) continue;
      const key = clean(p.cluster_key, 60).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
      if (!key || !p.pain) continue;
      rows.push({
        signal_id: p.signal_id, cluster_key: key, pain: clean(p.pain, 500),
        context: clean(p.context, 500), audience: clean(p.audience, 300), quote: clean(p.quote, 200),
        intent_level: Math.min(5, Math.max(1, Math.round(Number(p.intent_level) || 1))),
        product_hint: clean(p.product_hint, 200), geo: clean(p.geo, 60).toLowerCase(),
      });
    }
    done.push(...ids);  // 只有成功解析的批次才标记为已处理，失败的下次重试
  } catch (e) {
    errors.push({ signal_ids: ids, error: String(e.message || e).slice(0, 500) });
  }
});
return [{ json: { rows, done, errors } }];
""")
    save = postgres(f, "Save Pain Points", """
WITH ins AS (
  INSERT INTO pain_points (signal_id, cluster_key, pain, context, audience, quote,
                           intent_level, product_hint, geo)
  SELECT r.signal_id, r.cluster_key, r.pain, r.context, r.audience, r.quote,
         r.intent_level, r.product_hint, r.geo
  FROM jsonb_to_recordset($1::jsonb) AS r(
    signal_id bigint, cluster_key text, pain text, context text, audience text,
    quote text, intent_level smallint, product_hint text, geo text)
  RETURNING id
), upd AS (
  UPDATE raw_signals SET processed = true
  WHERE id IN (SELECT jsonb_array_elements_text($2::jsonb)::bigint)
  RETURNING id
)
SELECT (SELECT count(*) FROM ins) AS pain_points_inserted,
       (SELECT count(*) FROM upd) AS signals_processed;
""", "={{ [ JSON.stringify($json.rows), JSON.stringify($json.done) ] }}")
    f.chain(t, sel, b, h, parse, save)
    return f.dump()


# ---------------------------------------------------------------- 04 每周打分 + 简报邮件

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "opportunities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    **str_schema("cluster_key", "title"),
                    **{k: {"type": "integer"} for k in ("demand", "margin", "gap", "logistics", "compliance")},
                    **str_schema("target_price_cad", "brief_md"),
                },
                "required": ["cluster_key", "title", "demand", "margin", "gap", "logistics",
                             "compliance", "target_price_cad", "brief_md"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["opportunities"],
    "additionalProperties": False,
}


def wf_score():
    f = Flow("04-weekly-scoring", "04 打分 · 每周机会榜 + 邮件简报")
    t = schedule(f, "Every Monday 08:00", {"field": "weeks", "triggerAtDay": [1], "triggerAtHour": 8})
    agg = postgres(f, "Aggregate Clusters", """
SELECT p.cluster_key,
       count(*)                                              AS mentions,
       count(DISTINCT s.source)                              AS sources,
       round(avg(p.intent_level)::numeric, 2)                AS avg_intent,
       count(*) FILTER (WHERE p.geo IN ('calgary', 'alberta')) AS local_mentions,
       (array_agg(p.pain  ORDER BY p.intent_level DESC))[1:5]  AS top_pains,
       (array_agg(p.quote ORDER BY p.intent_level DESC))[1:5]  AS top_quotes,
       (array_agg(DISTINCT p.product_hint))[1:5]             AS product_hints,
       (array_agg(s.url   ORDER BY p.intent_level DESC))[1:5]  AS evidence_urls
FROM pain_points p
JOIN raw_signals s ON s.id = p.signal_id
WHERE p.created_at > now() - interval '30 days'
GROUP BY p.cluster_key
HAVING count(*) >= 3
ORDER BY count(*) * avg(p.intent_level) DESC
LIMIT 25;
""")
    b = code(f, "Build Scoring Request", CLAUDE_JS + f"""
const SYSTEM = {json.dumps(prompt('score_opportunities'), ensure_ascii=False)};
const SCHEMA = {json.dumps(SCORE_SCHEMA)};
""" + r"""
const clusters = $input.all().map(i => i.json).filter(c => c.cluster_key);
if (!clusters.length) return [];
const brief = clusters.map(({ evidence_urls, ...c }) => c);  // 链接不发给模型，留作证据
return [{ json: { request: claudeRequest(SYSTEM, 'Clusters:\n' + JSON.stringify(brief, null, 1), SCHEMA) } }];
""")
    h = claude_http(f, "Claude Score")
    parse = code(f, "Weight Scores", CLAUDE_JS + r"""
// 权重在代码里算，不交给模型，保证每周可比
const W = { demand: 0.30, margin: 0.25, gap: 0.20, logistics: 0.15, compliance: 0.10 };
const clusters = Object.fromEntries($('Aggregate Clusters').all().map(i => [i.json.cluster_key, i.json]));
const out = parseClaude($input.first().json);
const d = new Date(); d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
const weekOf = d.toISOString().slice(0, 10);
const rows = [];
for (const o of out.opportunities || []) {
  const c = clusters[o.cluster_key];
  if (!c) continue;  // 模型编造的 cluster 直接丢弃
  const scores = {};
  for (const k of Object.keys(W)) scores[k] = Math.min(10, Math.max(0, Number(o[k]) || 0));
  const total = Object.keys(W).reduce((s, k) => s + scores[k] * W[k], 0) * 10;
  rows.push({
    week_of: weekOf, cluster_key: o.cluster_key, title: String(o.title).slice(0, 200),
    scores: { ...scores, target_price_cad: o.target_price_cad,
              mentions: Number(c.mentions), avg_intent: Number(c.avg_intent),
              local_mentions: Number(c.local_mentions) },
    total_score: Math.round(total * 100) / 100,
    brief_md: o.brief_md,
    evidence: { urls: c.evidence_urls, quotes: c.top_quotes },
  });
}
return [{ json: { rows } }];
""")
    up = postgres(f, "Upsert Opportunities", """
WITH up AS (
  INSERT INTO opportunities (week_of, cluster_key, title, scores, total_score, brief_md, evidence)
  SELECT r.week_of, r.cluster_key, r.title, r.scores, r.total_score, r.brief_md, r.evidence
  FROM jsonb_to_recordset($1::jsonb) AS r(
    week_of date, cluster_key text, title text, scores jsonb,
    total_score numeric, brief_md text, evidence jsonb)
  ON CONFLICT (week_of, cluster_key) DO UPDATE
    SET title = EXCLUDED.title, scores = EXCLUDED.scores, total_score = EXCLUDED.total_score,
        brief_md = EXCLUDED.brief_md, evidence = EXCLUDED.evidence
  RETURNING id, cluster_key, title, total_score, scores
)
SELECT * FROM up ORDER BY total_score DESC;
""", ROWS_PARAM)
    dg = code(f, "Build Digest", r"""
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const top = $input.all().map(i => i.json).filter(o => o.id).slice(0, 10);
const rows = top.map((o, n) => `<tr><td>${n + 1}</td><td>#${o.id}</td><td>${esc(o.title)}</td>
  <td>${o.total_score}</td><td>${esc(o.scores.mentions)}</td><td>${esc(o.scores.local_mentions)}</td>
  <td>${esc(o.scores.target_price_cad)}</td></tr>`).join('');
const html = `<h2>本周需求机会 Top ${top.length}</h2>
<table border="1" cellpadding="6" cellspacing="0">
<tr><th>#</th><th>ID</th><th>机会</th><th>总分</th><th>提及</th><th>本地提及</th><th>目标价 CAD</th></tr>${rows}</table>
<p>完整简报在 opportunities 表。要给商家写外联草稿，调用 /webhook/outreach。</p>`;
return [{ json: { html, subject: `Demand Radar 周报：${top.length} 个机会` } }];
""")
    mail = f.add(
        "Email Digest", "n8n-nodes-base.emailSend", 2.1,
        {
            "fromEmail": "={{ $env.DIGEST_FROM }}",
            "toEmail": "={{ $env.DIGEST_TO }}",
            "subject": "={{ $json.subject }}",
            "emailFormat": "html",
            "html": "={{ $json.html }}",
            "options": {},
        },
    )
    f.chain(t, agg, b, h, parse, up, dg, mail)
    return f.dump()


# ---------------------------------------------------------------- 05 商家外联草稿（人工审核）

OUTREACH_SCHEMA = {
    "type": "object",
    "properties": str_schema("subject", "body"),
    "required": ["subject", "body"],
    "additionalProperties": False,
}


def wf_outreach():
    f = Flow("05-merchant-outreach-drafts", "05 变现 · 商家外联草稿（人工审核后发送）")
    w = webhook(f, "POST /outreach", "outreach")
    fetch = postgres(f, "Fetch Opportunity & Merchants", """
SELECT o.id AS opportunity_id, o.title, o.brief_md, o.scores,
       m.id AS merchant_id, m.name, m.kind, m.language, m.categories
FROM opportunities o
JOIN merchants m ON m.id IN (SELECT jsonb_array_elements_text($2::jsonb)::bigint)
WHERE o.id = $1::bigint;
""", "={{ [ String($json.body.opportunity_id), JSON.stringify($json.body.merchant_ids || []) ] }}")
    b = code(f, "Build Draft Requests", CLAUDE_JS + f"""
const SYSTEM = {json.dumps(prompt('outreach_draft'), ensure_ascii=False)};
const SCHEMA = {json.dumps(OUTREACH_SCHEMA)};
""" + r"""
return $input.all().map(i => i.json).filter(r => r.merchant_id).map(r => {
  const lang = r.language === 'zh' ? 'Simplified Chinese' : 'English';
  const user = `Language: ${lang}
Recipient: ${r.name} (${r.kind}; categories: ${(r.categories || []).join(', ')})
Opportunity: ${r.title}
Scores: ${JSON.stringify(r.scores)}
Brief:
${r.brief_md}`;
  return { json: { opportunity_id: r.opportunity_id, merchant_id: r.merchant_id,
                   language: r.language, request: claudeRequest(SYSTEM, user, SCHEMA, 4000) } };
});
""")
    h = claude_http(f, "Claude Draft")
    parse = code(f, "Parse Drafts", CLAUDE_JS + r"""
const reqs = $('Build Draft Requests').all();
const rows = [], errors = [];
$input.all().forEach((item, i) => {
  const r = reqs[i] && reqs[i].json;
  try {
    const d = parseClaude(item.json);
    rows.push({ opportunity_id: r.opportunity_id, merchant_id: r.merchant_id,
                language: r.language, subject: d.subject, body: d.body });
  } catch (e) { errors.push({ merchant_id: r && r.merchant_id, error: String(e.message || e) }); }
});
return [{ json: { rows, errors } }];
""")
    save = postgres(f, "Save Drafts", """
INSERT INTO outreach_drafts (opportunity_id, merchant_id, language, subject, body)
SELECT r.opportunity_id, r.merchant_id, r.language, r.subject, r.body
FROM jsonb_to_recordset($1::jsonb) AS r(
  opportunity_id bigint, merchant_id bigint, language text, subject text, body text)
RETURNING id, merchant_id, subject, status;
""", ROWS_PARAM)
    f.chain(w, fetch, b, h, parse, save)
    return f.dump()


# ---------------------------------------------------------------- 06 成交回传 → 佣金台账

def wf_conversions():
    f = Flow("06-commission-tracking", "06 变现 · 成交回传与佣金台账")
    w = webhook(f, "POST /conversion", "conversion")
    n = code(f, "Normalize Order", r"""
// 兼容两种回传：自定义 JSON {merchant_id, order_id, amount, code}，
// 或 Shopify 订单 webhook（?merchant_id=123 放在 URL 上）
const b = $input.first().json.body || {};
const q = $input.first().json.query || {};
const code = String(b.code || (b.discount_codes && b.discount_codes[0] && b.discount_codes[0].code) || '')
  .trim().toUpperCase();
const orderId = String(b.order_id || b.id || '');
const amount = Number(b.amount ?? b.subtotal_price ?? b.total_price);  // 佣金按税前小计
const merchantId = Number(b.merchant_id || q.merchant_id);
if (!code || !orderId || !Number.isFinite(amount) || amount < 0 || !merchantId) {
  throw new Error('need code, order_id, amount, merchant_id');
}
return [{ json: { code, orderId, amount: amount.toFixed(2), currency: b.currency || 'CAD',
                  merchantId: String(merchantId) } }];
""")
    ins = postgres(f, "Record Conversion", """
WITH ins AS (
  INSERT INTO conversions (code, merchant_id, order_id, order_amount, currency, commission)
  SELECT pc.code, pc.merchant_id, $2, $3::numeric, $4, round($3::numeric * pc.commission_rate, 2)
  FROM partner_codes pc
  WHERE pc.code = $1 AND pc.merchant_id = $5::bigint AND pc.active
  ON CONFLICT (merchant_id, order_id) DO NOTHING
  RETURNING id, code, order_id, commission
)
SELECT coalesce((SELECT row_to_json(ins) FROM ins), '{"recorded": false}'::json) AS result;
""", "={{ [ $json.code, $json.orderId, $json.amount, $json.currency, $json.merchantId ] }}")
    f.chain(w, n, ins)
    return f.dump()


# ---------------------------------------------------------------- 07 等候名单（你的护城河）

def wf_waitlist():
    f = Flow("07-waitlist-signup", "07 护城河 · 落地页等候名单")
    w = webhook(f, "POST /waitlist", "waitlist", auth=False)
    n = code(f, "Validate Signup", r"""
const crypto = require('crypto');
const b = $input.first().json.body || {};
const email = String(b.email || '').trim().toLowerCase();
if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || email.length > 254) throw new Error('invalid email');
const consent = b.consent === true || b.consent === 'true' || b.consent === 'on';
return [{ json: {
  opportunity_id: b.opportunity_id ? String(Number(b.opportunity_id)) : null,
  email_hash: crypto.createHash('sha256').update(email).digest('hex'),
  email: consent ? email : null,   // CASL：没有明示同意就只存哈希，用于计数
  consent: String(consent),
  source: String(b.source || 'landing').slice(0, 60),
}}];
""")
    ins = postgres(f, "Insert Waitlist", """
INSERT INTO waitlist (opportunity_id, email_hash, email, consent, source)
VALUES (NULLIF($1, '')::bigint, $2, NULLIF($3, ''), $4::boolean, $5)
ON CONFLICT (opportunity_id, email_hash) DO NOTHING
RETURNING true AS ok;
""", "={{ [ $json.opportunity_id || '', $json.email_hash, $json.email || '', $json.consent, $json.source ] }}",
        alwaysOutputData=True)
    f.chain(w, n, ins)
    return f.dump()


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for build in (wf_reddit, wf_apify, wf_analyze, wf_score, wf_outreach, wf_conversions, wf_waitlist):
        print("wrote", build().relative_to(ROOT))
