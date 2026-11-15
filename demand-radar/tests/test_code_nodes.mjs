// 在模拟的 n8n 运行时里执行每个 Code 节点，并检查工作流结构。
//   node tests/test_code_nodes.mjs
import { readFileSync, readdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import assert from 'node:assert/strict';

const require = createRequire(import.meta.url);
const dir = new URL('../workflows/', import.meta.url);
const flows = Object.fromEntries(readdirSync(dir).filter(f => f.endsWith('.json'))
  .map(f => [f.slice(0, 2), JSON.parse(readFileSync(new URL(f, dir), 'utf8'))]));

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const wrap = arr => arr.map(x => (x && x.json ? x : { json: x }));

async function run(flow, nodeName, input, refs = {}, env = {}) {
  const node = flows[flow].nodes.find(n => n.name === nodeName);
  assert.ok(node, `${flow}/${nodeName} not found`);
  const items = wrap(input);
  const $input = { all: () => items, first: () => items[0] };
  const $ = name => {
    assert.ok(name in refs, `${nodeName} reads $('${name}') but the test gave no fixture`);
    return { all: () => wrap(refs[name]) };
  };
  const fn = new AsyncFunction('$input', '$', '$env', 'require', node.parameters.jsCode);
  return fn($input, $, env, require);
}

let passed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log('ok  ', name); }
  catch (e) { console.error('FAIL', name, '\n', e); process.exit(1); }
}

// ------------------------------------------------------------ 结构
await test('所有工作流：节点名唯一、连线指向存在的节点、除触发器外都有上游', () => {
  for (const [k, wf] of Object.entries(flows)) {
    const names = wf.nodes.map(n => n.name);
    assert.equal(new Set(names).size, names.length, `${k} duplicate names`);
    const targets = new Set();
    for (const [src, c] of Object.entries(wf.connections)) {
      assert.ok(names.includes(src), `${k}: unknown source ${src}`);
      for (const t of c.main.flat()) { assert.ok(names.includes(t.node), `${k}: unknown target ${t.node}`); targets.add(t.node); }
    }
    for (const n of wf.nodes) {
      const trigger = /Trigger$|webhook$/.test(n.type);
      assert.equal(targets.has(n.name), !trigger, `${k}: ${n.name} wiring`);
    }
  }
});

await test('所有 Code 节点语法正确', () => {
  for (const wf of Object.values(flows))
    for (const n of wf.nodes.filter(n => n.type === 'n8n-nodes-base.code'))
      new AsyncFunction('$input', '$', '$env', 'require', n.parameters.jsCode);
});

await test('$("节点名") 引用的节点都存在于同一工作流', () => {
  for (const [k, wf] of Object.entries(flows)) {
    const names = new Set(wf.nodes.map(n => n.name));
    for (const n of wf.nodes.filter(n => n.type === 'n8n-nodes-base.code'))
      for (const [, ref] of n.parameters.jsCode.matchAll(/\$\('([^']+)'\)/g))
        assert.ok(names.has(ref), `${k}/${n.name} -> ${ref}`);
  }
});

// ------------------------------------------------------------ 01 Reddit
await test('01 Build Queries：每个社区一条查询，缺 token 报错', async () => {
  const out = await run('01', 'Build Queries', [{ access_token: 'tok' }]);
  assert.ok(out.length >= 5);
  assert.equal(out[0].json.token, 'tok');
  assert.match(out[0].json.q, /"looking for" OR /);
  await assert.rejects(run('01', 'Build Queries', [{ error: 'invalid_grant' }]), /token missing/);
});

await test('01 Normalize Posts：去重、跳过置顶/NSFW、作者只存哈希、打地域标签', async () => {
  const post = (id, extra = {}) => ({ data: { id, title: 'T', selftext: 'B', author: 'alice',
    subreddit: 'Calgary', permalink: `/r/Calgary/${id}`, created_utc: 1762000000, ...extra } });
  const out = await run('01', 'Normalize Posts', [
    { data: { children: [post('a'), post('b', { stickied: true }), post('c', { over_18: true })] } },
    { data: { children: [post('a'), post('d', { subreddit: 'dogs' })] } },
    { error: 'http 429' },
  ]);
  const { rows } = out[0].json;
  assert.deepEqual(rows.map(r => r.external_id), ['a', 'd']);
  assert.equal(rows[0].geo_hint, 'calgary');
  assert.equal(rows[1].geo_hint, null);
  assert.equal(rows[0].author_hash.length, 16);
  assert.ok(!JSON.stringify(rows).includes('alice'));
  assert.equal(rows[0].posted_at, '2025-11-01T12:26:40.000Z');
});

// ------------------------------------------------------------ 02 Apify
await test('02 Apify Jobs：没有配置竞品链接时不发请求', async () => {
  assert.deepEqual(await run('02', 'Apify Jobs', [{}]), []);
});

await test('02 Normalize Reviews：只保留 ≤3 星，按 pairedItem 找回来源', async () => {
  const jobs = [{ source: 'amazon_review' }, { source: 'other_review' }];
  const out = await run('02', 'Normalize Reviews', [
    { json: { reviewId: 'r1', ratingScore: 5, reviewDescription: 'great' }, pairedItem: { item: 0 } },
    { json: { reviewId: 'r2', ratingScore: 2, reviewTitle: 'cracked', reviewDescription: 'broke at -30C' }, pairedItem: { item: 0 } },
    { json: { id: 'r3', rating: 1, text: 'too small' }, pairedItem: { item: 1 } },
  ], { 'Apify Jobs': jobs });
  const { rows } = out[0].json;
  assert.deepEqual(rows.map(r => [r.external_id, r.source]), [['r2', 'amazon_review'], ['r3', 'other_review']]);
});

// ------------------------------------------------------------ 03 分析
await test('03 Build Claude Requests：10 条一批、结构化输出、模型可由环境变量覆盖', async () => {
  const sigs = Array.from({ length: 12 }, (_, i) => ({ id: String(i + 1), source: 'reddit', title: 't', body: 'x'.repeat(5000) }));
  const out = await run('03', 'Build Claude Requests', sigs, {}, { CLAUDE_MODEL: 'claude-haiku-5-5' });
  assert.equal(out.length, 2);
  assert.deepEqual(out[1].json.signal_ids, [11, 12]);
  const req = out[0].json.request;
  assert.equal(req.model, 'claude-haiku-5-5');
  assert.equal(req.output_config.format.type, 'json_schema');
  assert.equal(req.output_config.effort, 'low');
  assert.equal(req.fallbacks, 'default');
  assert.ok(req.messages[0].content.length < 30000, 'body truncated');
  const def = await run('03', 'Build Claude Requests', sigs.slice(0, 1));
  assert.equal(def[0].json.request.model, 'claude-opus-5-5');
});

const claudeReply = obj => ({ stop_reason: 'end_turn', content: [{ type: 'text', text: JSON.stringify(obj) }] });

await test('03 Parse Pain Points：丢弃非需求和越界 ID，失败批次不标记已处理', async () => {
  const item = (id, extra = {}) => ({ signal_id: id, is_demand: true, cluster_key: 'Car Hail  Protection!',
    pain: 'hail', context: '', audience: '', quote: 'q', intent_level: 9, product_hint: 'cover', geo: 'Calgary', ...extra });
  const out = await run('03', 'Parse Pain Points', [
    claudeReply({ items: [item(1), item(2, { is_demand: false }), item(99)] }),
    { error: { message: 'overloaded' } },
    { stop_reason: 'refusal', stop_details: { category: null }, content: [] },
  ], { 'Build Claude Requests': [{ signal_ids: [1, 2] }, { signal_ids: [3] }, { signal_ids: [4] }] });
  const { rows, done, errors } = out[0].json;
  assert.equal(rows.length, 1);
  assert.equal(rows[0].cluster_key, 'car-hail-protection');
  assert.equal(rows[0].intent_level, 5);
  assert.equal(rows[0].geo, 'calgary');
  assert.deepEqual(done, [1, 2]);
  assert.equal(errors.length, 2);
  assert.match(errors[1].error, /refused/);
});

// ------------------------------------------------------------ 04 打分
const cluster = { cluster_key: 'car-hail-protection', mentions: '12', sources: '2', avg_intent: '3.8',
  local_mentions: '9', top_pains: ['p'], top_quotes: ['q'], product_hints: ['cover'], evidence_urls: ['https://r/1'] };

await test('04 Build Scoring Request：证据链接不发给模型；无数据时不调用', async () => {
  const out = await run('04', 'Build Scoring Request', [cluster]);
  const text = out[0].json.request.messages[0].content;
  assert.ok(text.includes('car-hail-protection') && !text.includes('https://r/1'));
  assert.deepEqual(await run('04', 'Build Scoring Request', [{}]), []);
});

await test('04 Weight Scores：代码加权、截断越界分、丢弃模型编造的 cluster', async () => {
  const opp = (key, s) => ({ cluster_key: key, title: 'Hail cover', demand: s, margin: s, gap: s,
    logistics: s, compliance: 15, target_price_cad: '60-90', brief_md: '# b' });
  const out = await run('04', 'Weight Scores',
    [claudeReply({ opportunities: [opp('car-hail-protection', 8), opp('made-up', 10)] })],
    { 'Aggregate Clusters': [cluster] });
  const { rows } = out[0].json;
  assert.equal(rows.length, 1);
  assert.equal(rows[0].scores.compliance, 10);
  assert.equal(rows[0].total_score, 82);  // (8×0.9 + 10×0.1) × 10
  assert.equal(rows[0].scores.mentions, 12);
  assert.deepEqual(rows[0].evidence.urls, ['https://r/1']);
  assert.equal(new Date(rows[0].week_of + 'T12:00:00Z').getUTCDay(), 1, 'week_of is a Monday');
});

await test('04 Build Digest：HTML 转义', async () => {
  const out = await run('04', 'Build Digest', [{ id: '1', title: '<script>x</script>', total_score: '82.00',
    scores: { mentions: 12, local_mentions: 9, target_price_cad: '60-90' } }]);
  assert.ok(out[0].json.html.includes('&lt;script&gt;') && !out[0].json.html.includes('<script>'));
});

// ------------------------------------------------------------ 05 外联
await test('05 Build Draft Requests / Parse Drafts：中文商家用中文，失败单独记录', async () => {
  const rows = [
    { opportunity_id: '7', merchant_id: '1', name: '宁波塑料', kind: 'factory', language: 'zh', categories: ['auto'], title: 'Hail cover', brief_md: 'b', scores: {} },
    { opportunity_id: '7', merchant_id: '2', name: 'YYC Auto', kind: 'local_shop', language: 'en', categories: null, title: 'Hail cover', brief_md: 'b', scores: {} },
  ];
  const reqs = await run('05', 'Build Draft Requests', rows);
  assert.match(reqs[0].json.request.messages[0].content, /Simplified Chinese/);
  assert.match(reqs[1].json.request.messages[0].content, /Language: English/);
  const out = await run('05', 'Parse Drafts',
    [claudeReply({ subject: '合作', body: '您好' }), { stop_reason: 'max_tokens', content: [] }],
    { 'Build Draft Requests': reqs.map(r => r.json) });
  assert.equal(out[0].json.rows.length, 1);
  assert.equal(out[0].json.errors[0].merchant_id, '2');
});

// ------------------------------------------------------------ 06 佣金
await test('06 Normalize Order：兼容 Shopify 订单，用税前小计，缺字段报错', async () => {
  const out = await run('06', 'Normalize Order', [{ query: { merchant_id: '3' }, body: {
    id: 5550001, subtotal_price: '120.00', total_price: '138.60', currency: 'CAD', discount_codes: [{ code: 'hail10 ' }] } }]);
  assert.deepEqual(out[0].json, { code: 'HAIL10', orderId: '5550001', amount: '120.00', currency: 'CAD', merchantId: '3' });
  await assert.rejects(run('06', 'Normalize Order', [{ body: { order_id: '1', amount: 10 } }]), /need code/);
});

// ------------------------------------------------------------ 07 等候名单
await test('07 Validate Signup：未同意只存哈希，非法邮箱拒绝', async () => {
  const out = await run('07', 'Validate Signup', [{ body: { email: ' A@B.ca ', opportunity_id: '7' } }]);
  assert.equal(out[0].json.email, null);
  assert.equal(out[0].json.consent, 'false');
  assert.equal(out[0].json.email_hash.length, 64);
  const yes = await run('07', 'Validate Signup', [{ body: { email: 'a@b.ca', consent: 'on' } }]);
  assert.equal(yes[0].json.email, 'a@b.ca');
  assert.equal(yes[0].json.email_hash, out[0].json.email_hash);
  await assert.rejects(run('07', 'Validate Signup', [{ body: { email: 'nope' } }]), /invalid email/);
});

console.log(`\n${passed} tests passed`);
