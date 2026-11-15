import json

import httpx
import pytest
import respx

from geo.content import cards
from geo.llm import Claude
from geo.measure import analyze, engines, judge, panel, probe, report
from conftest import Block, fake_client, parsed


# ------------------------------------------------------------------ mention detection & stats

def test_mentions_order_aliases_and_word_boundaries(cat):
    pats = analyze._patterns(cat)
    text = "Try Storm Wrap first. Foothill Gear also works. Northshielded is not a brand."
    assert analyze.mentions(text, pats) == ["Stormwrap", "Foothill Gear Co."]


def test_wilson():
    lo, hi = analyze.wilson(0, 10)
    assert lo == 0 and 0.25 < hi < 0.35
    lo, hi = analyze.wilson(5, 10)
    assert lo < 0.5 < hi
    assert analyze.wilson(0, 0) == (0.0, 0.0)


def _row(pid, engine, text, cited=(), retrieved=(), sample=0, intent="local"):
    return {"run_id": "r1", "prompt_id": pid, "intent": intent, "sample": sample, "engine": engine,
            "model": "m", "text": text, "cited": list(cited), "retrieved": list(retrieved), "error": None}


ROWS = [
    _row("p1", "claude", "Foothill Gear is great; Stormwrap too.", ["https://www.foothillgear.example/x"]),
    _row("p1", "claude", "Stormwrap is the usual pick.", ["https://reddit.com/r/Calgary/1"],
         ["https://www.foothillgear.example/hail-cover-sedan/"], sample=1),
    _row("p2", "perplexity", "Stormwrap or Northshield.", ["https://reddit.com/a", "https://www.wirecutter.example/b",
                                                         "https://stormwrap.example/p"]),
    _row("p2", "perplexity", "Stormwrap.", ["https://reddit.com/c"], sample=1),
    {**_row("p2", "perplexity", ""), "error": "HTTPStatusError: 429"},
]


def test_annotate_and_metrics(cat):
    ann = analyze.annotate(ROWS, cat)
    assert len(ann) == 4  # errored sample excluded
    m = analyze.metrics(ann, cat)
    o = m["overall"]
    assert o["answers"] == 4
    assert o["mention_rate"] == 0.25
    assert o["citation_rate"] == 0.25
    assert o["retrieved_not_cited"] == 1
    assert o["brand_mention_rates"]["Stormwrap"] == 1.0
    assert o["avg_rank_when_mentioned"] == 1
    assert m["by_engine"]["perplexity"]["mention_rate"] == 0
    assert m["top_third_party_domains"][0] == ("reddit.com", 3)
    assert "stormwrap.example" not in dict(m["top_third_party_domains"])  # competitor's own site is not a target


def test_gaps_find_missing_prompts_and_uncovered_fanout(cat):
    prompts = [{"id": "p1", "text": "q1", "fanout": []},
               {"id": "p2", "text": "best hail protection for a car in calgary",
                "fanout": ["hail cover foam thickness", "insurance deductible hail claim alberta"]}]
    ann = analyze.annotate(ROWS, cat)
    g = analyze.gaps(ann, prompts, cat, cat.facts_text())
    assert [x["prompt_id"] for x in g] == ["p2"]
    assert g[0]["leader"] == "Stormwrap" and g[0]["leader_rate"] == 1.0
    assert g[0]["uncovered_fanout"] == ["insurance deductible hail claim alberta"]
    assert g[0]["sources_engines_used"][0] == ("reddit.com", 2)


def test_report_renders(cat):
    ann = analyze.annotate(ROWS, cat)
    md = report.render(cat, analyze.metrics(ann, cat), analyze.gaps(ann, [{"id": "p2", "text": "Q2", "fanout": []}], cat, ""),
                       [{"engine": "claude", "recommended": True, "inaccuracies": [
                           {"claim": "costs 99 CAD", "correction": "189.00 CAD", "severity": "high"}]}], "r1")
    assert "Mentioned in **25%** of answers" in md
    assert "| claude | 2 | 50% |" in md
    assert "### Q2" in md
    assert "**high** (claude): \"costs 99 CAD\" → 189.00 CAD" in md


# ------------------------------------------------------------------ engine adapters

def test_claude_engine_resumes_pause_turn_and_splits_cited_vs_retrieved():
    first = type("R", (), {"stop_reason": "pause_turn", "content": [
        Block(type="server_tool_use", id="s1", name="web_search", input={"query": "hail cover"}),
        Block(type="web_search_tool_result", tool_use_id="s1",
              content=[{"type": "web_search_result", "url": "https://a.example/1"},
                       {"type": "web_search_result", "url": "https://b.example/2"}])]})()
    second = type("R", (), {"stop_reason": "end_turn", "content": [
        Block(type="text", text="Use a padded cover.", citations=[{"type": "web_search_result_location",
                                                                     "url": "https://a.example/1"}]),
        Block(type="text", text=" Park inside if you can.", citations=None)]})()
    client = fake_client(first, second)
    a = engines.ClaudeEngine("claude-opus-5-5", client=client).ask("q")
    assert a.error is None
    assert a.text == "Use a padded cover. Park inside if you can."
    assert a.cited == ["https://a.example/1"]
    assert a.retrieved == ["https://a.example/1", "https://b.example/2"]
    calls = client.beta.messages.calls
    assert len(calls) == 2 and calls[1]["messages"][-1]["role"] == "assistant"
    tool = calls[0]["tools"][0]
    assert tool["type"] == "web_search_20260209" and tool["user_location"]["city"] == "Calgary"
    assert calls[0]["fallbacks"] == "default"


def test_engine_errors_are_captured_not_raised():
    a = engines.ClaudeEngine("m", client=fake_client()).ask("q")  # no queued response -> IndexError
    assert a.error and a.text == ""


@respx.mock
def test_openai_engine(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    route = respx.post(engines.OpenAIEngine.url).mock(return_value=httpx.Response(200, json={"output": [
        {"type": "web_search_call", "action": {"sources": [{"url": "https://x.example/a"}]}},
        {"type": "message", "content": [{"type": "output_text", "text": "Stormwrap.",
                                         "annotations": [{"type": "url_citation", "url": "https://x.example/a"}]}]}]}))
    a = engines.OpenAIEngine("model-x").ask("q")
    assert (a.text, a.cited, a.retrieved) == ("Stormwrap.", ["https://x.example/a"], ["https://x.example/a"])
    body = json.loads(route.calls[0].request.content)
    assert body["tools"][0]["type"] == "web_search"
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-test"


@respx.mock
def test_perplexity_engine(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx")
    respx.post(engines.PerplexityEngine.url).mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": "Answer [1]"}}],
        "citations": ["https://r.example/1"],
        "search_results": [{"url": "https://r.example/1"}, {"url": "https://r.example/2"}]}))
    a = engines.PerplexityEngine("sonar").ask("q")
    assert a.cited == ["https://r.example/1"] and len(a.retrieved) == 2


@respx.mock
def test_gemini_engine_resolves_redirect_sources(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    respx.post(f"{engines.GeminiEngine.base}/model-g:generateContent").mock(return_value=httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": "Hi"}]}, "groundingMetadata": {"groundingChunks": [
            {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc", "title": "reddit.com"}},
            {"web": {"uri": "https://direct.example/p", "title": "Direct"}}]}}]}))
    a = engines.GeminiEngine("model-g").ask("q")
    assert a.cited == ["reddit.com", "https://direct.example/p"]
    assert [engines.domain_of(c) for c in a.cited] == ["reddit.com", "direct.example"]


@respx.mock
def test_http_errors_become_answer_errors(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx")
    respx.post(engines.PerplexityEngine.url).mock(return_value=httpx.Response(429))
    assert "429" in engines.PerplexityEngine("sonar").ask("q").error


def test_from_env_only_enables_configured_engines(monkeypatch):
    for k in ["ANTHROPIC_API_KEY", "GEO_ENABLE_CLAUDE", "OPENAI_API_KEY", "GEO_OPENAI_MODEL",
              "PERPLEXITY_API_KEY", "GEMINI_API_KEY", "GEO_GEMINI_MODEL"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "x")  # key without a model: stays off
    monkeypatch.setenv("PERPLEXITY_API_KEY", "x")
    assert [e.name for e in engines.from_env()] == ["perplexity"]


# ------------------------------------------------------------------ probe

class EchoEngine(engines.Engine):
    name = "echo"

    def _ask(self, prompt):
        return engines.Answer(self.name, self.model, f"Foothill Gear for {prompt}")


def test_probe_run_samples_every_pair(tmp_path):
    prompts = [{"id": "a", "text": "x", "intent": "local"}, {"id": "b", "text": "y", "intent": "comparison"}]
    path = probe.run(prompts, [EchoEngine("m")], samples=3, out=tmp_path, workers=2)
    rows = probe.load(path)
    assert len(rows) == 6 and {r["sample"] for r in rows} == {0, 1, 2}
    assert rows[0]["intent"] in {"local", "comparison"}


# ------------------------------------------------------------------ Claude-powered steps

def test_panel_generation_drops_brand_leaks_and_duplicates(cat):
    draft = panel.PanelDraft(prompts=[
        panel.PanelPrompt(text="Best way to protect my car from hail in Calgary?", persona="condo owner",
                          intent="local", fanout=["car hail cover calgary"]),
        panel.PanelPrompt(text="best way to protect my car  from hail in calgary?", persona="dup",
                          intent="local", fanout=[]),
        panel.PanelPrompt(text="Is Stormwrap worth it?", persona="x", intent="comparison", fanout=[])])
    client = fake_client(parsed(draft))
    out = panel.generate(cat, 3, claude=Claude(client))
    assert [p["text"] for p in out] == ["Best way to protect my car from hail in Calgary?"]
    assert len(out[0]["id"]) == 12
    sent = client.beta.messages.calls[0]
    assert sent["output_format"] is panel.PanelDraft and "Foothill" not in sent["messages"][0]["content"]


def test_claude_refusal_raises(cat):
    with pytest.raises(RuntimeError, match="declined"):
        Claude(fake_client(parsed(None, stop="refusal"))).structured("s", "u", panel.PanelDraft)


def test_judge_only_reviews_answers_that_mention_us(cat):
    ann = analyze.annotate(ROWS, cat)
    v = judge.Verdict(sentiment="positive", recommended=True, inaccuracies=[])
    client = fake_client(parsed(v))
    out = judge.judge(ann, cat, claude=Claude(client))
    assert len(out) == 1 and out[0]["recommended"] is True
    assert "189.00 CAD" in client.beta.messages.calls[0]["messages"][0]["content"]


def test_card_grounding_check(cat):
    ok = cards.Card(product_id="FG-HC-01", question="Can I stop hail damage for under 200 dollars?",
                    answer="Yes. The Foothill Hail Cover costs 189.00 CAD and has a 10 mm foam core.",
                    key_facts=["Fits sedans 4.4 to 4.9 m long"])
    assert cards.check(ok, cat) == []
    bad = ok.model_copy(update={"answer": "It blocks 99% of hail and costs 149 CAD.", "product_id": "nope"})
    problems = cards.check(bad, cat)
    assert any("unknown product_id" in p for p in problems)
    assert any("['149', '99']" in p for p in problems)


def test_card_drafts_are_marked_for_review(cat, tmp_path):
    good = cards.Card(product_id="FG-HC-01", question="Hail cover for a condo parking spot?",
                      answer="A padded cover is the practical option; this one installs in about 3 minutes.",
                      key_facts=["10 mm foam core"])
    bad = good.model_copy(update={"answer": "Rated 5 stars by 1000 drivers."})
    gap_list = [{"prompt_id": "p1", "prompt": "q1", "uncovered_fanout": ["a"]},
                {"prompt_id": "p2", "prompt": "q2", "uncovered_fanout": []}]
    paths = cards.draft(cat, gap_list, tmp_path, claude=Claude(fake_client(parsed(good), parsed(bad))))
    docs = [json.loads(p.read_text()) for p in paths]
    assert [d["status"] for d in docs] == ["needs_review", "rejected"]
    assert docs[1]["problems"] == ["numbers not in catalog: ['1000', '5']"]
