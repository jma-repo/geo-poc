-- Demand Radar 数据库结构
-- 需求端业务：抓需求 → 提炼痛点 → 打分成机会 → 卖给/推荐给商家 → 按成交拿佣金

-- 1. 原始信号：各渠道抓回来的帖子、评论、差评
CREATE TABLE IF NOT EXISTS raw_signals (
  id            BIGSERIAL PRIMARY KEY,
  source        TEXT NOT NULL,               -- reddit / amazon_review / youtube / kickstarter ...
  external_id   TEXT NOT NULL,               -- 渠道内的唯一 ID，用于去重
  url           TEXT,
  title         TEXT,
  body          TEXT,
  author_hash   TEXT,                        -- 只存哈希，不存用户名
  geo_hint      TEXT,                        -- 如 calgary / alberta / canada
  posted_at     TIMESTAMPTZ,
  collected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed     BOOLEAN NOT NULL DEFAULT false,
  UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_unprocessed ON raw_signals (processed) WHERE processed = false;

-- 2. 痛点：Claude 从信号中提炼出的结构化需求
CREATE TABLE IF NOT EXISTS pain_points (
  id            BIGSERIAL PRIMARY KEY,
  signal_id     BIGINT NOT NULL REFERENCES raw_signals(id) ON DELETE CASCADE,
  cluster_key   TEXT NOT NULL,               -- 归一化的短标识，如 car-hail-protection
  pain          TEXT NOT NULL,
  context       TEXT,
  audience      TEXT,
  quote         TEXT,
  intent_level  SMALLINT NOT NULL CHECK (intent_level BETWEEN 1 AND 5),  -- 5 = 明确想买
  product_hint  TEXT,
  geo           TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pain_cluster ON pain_points (cluster_key, created_at);

-- 3. 机会：每周按 cluster 汇总、打分后的选品机会
CREATE TABLE IF NOT EXISTS opportunities (
  id            BIGSERIAL PRIMARY KEY,
  week_of       DATE NOT NULL,
  cluster_key   TEXT NOT NULL,
  title         TEXT NOT NULL,
  scores        JSONB NOT NULL,              -- {demand, margin, gap, logistics, compliance}
  total_score   NUMERIC(5,2) NOT NULL,
  brief_md      TEXT NOT NULL,               -- 给商家看的需求简报
  evidence      JSONB,                       -- 引用的原话和链接
  status        TEXT NOT NULL DEFAULT 'new', -- new / pitched / matched / dropped
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (week_of, cluster_key)
);

-- 4. 商家/工厂：你的买方
CREATE TABLE IF NOT EXISTS merchants (
  id            BIGSERIAL PRIMARY KEY,
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'factory',  -- factory / brand / local_shop / seller
  country       TEXT,
  categories    TEXT[],
  contact_email TEXT,
  language      TEXT NOT NULL DEFAULT 'en',       -- en / zh
  agreement     TEXT,                             -- 合同状态：none / nda / signed
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 5. 外联草稿：一律 pending_review，人工审核后才发
CREATE TABLE IF NOT EXISTS outreach_drafts (
  id             BIGSERIAL PRIMARY KEY,
  opportunity_id BIGINT NOT NULL REFERENCES opportunities(id),
  merchant_id    BIGINT NOT NULL REFERENCES merchants(id),
  language       TEXT NOT NULL,
  subject        TEXT NOT NULL,
  body           TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'pending_review', -- pending_review / approved / sent / rejected
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 6. 归因码：每个“商家 × 机会”一个专属折扣码或联盟链接
CREATE TABLE IF NOT EXISTS partner_codes (
  code            TEXT PRIMARY KEY,
  merchant_id     BIGINT NOT NULL REFERENCES merchants(id),
  opportunity_id  BIGINT REFERENCES opportunities(id),
  commission_rate NUMERIC(5,4) NOT NULL,     -- 0.1000 = 10%
  active          BOOLEAN NOT NULL DEFAULT true,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 7. 成交与佣金台账
CREATE TABLE IF NOT EXISTS conversions (
  id            BIGSERIAL PRIMARY KEY,
  code          TEXT NOT NULL REFERENCES partner_codes(code),
  merchant_id   BIGINT NOT NULL REFERENCES merchants(id),
  order_id      TEXT NOT NULL,
  order_amount  NUMERIC(12,2) NOT NULL,
  currency      TEXT NOT NULL DEFAULT 'CAD',
  commission    NUMERIC(12,2) NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / paid / reversed
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (merchant_id, order_id)
);

-- 8. 等候名单：你自己掌握的需求方（防止商家绕开你）
CREATE TABLE IF NOT EXISTS waitlist (
  id             BIGSERIAL PRIMARY KEY,
  opportunity_id BIGINT REFERENCES opportunities(id),
  email_hash     TEXT NOT NULL,
  email          TEXT,                         -- 仅在已同意接收邮件时保存
  consent        BOOLEAN NOT NULL DEFAULT false,
  source         TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE NULLS NOT DISTINCT (opportunity_id, email_hash)
);
