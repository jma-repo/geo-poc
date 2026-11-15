import json

import httpx
import pytest
import respx

from analyze import llm, pipeline


def ds_reply(obj_or_text, finish="stop"):
    content = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text)
    return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}]})


@pytest.fixture
def ds(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    return llm.DeepSeek(model="deepseek-flash")


@respx.mock
def test_deepseek_puts_schema_in_prompt_and_uses_json_mode(ds):
    route = respx.post(llm.DeepSeek.url).mock(return_value=ds_reply({"merges": [{"key": "a", "canonical": "b"}]}))
    out = ds.json("sys", "user", pipeline.MergeMap)
    assert out.merges[0].canonical == "b"
    body = json.loads(route.calls[0].request.content)
    assert body["response_format"] == {"type": "json_object"} and body["model"] == "deepseek-flash"
    assert '"canonical"' in body["messages"][0]["content"]
    assert route.calls[0].request.headers["authorization"] == "Bearer k"


@respx.mock
def test_deepseek_retries_invalid_json_with_the_error(ds):
    route = respx.post(llm.DeepSeek.url).mock(side_effect=[
        ds_reply({"merges": [{"key": "a"}]}),  # missing field
        ds_reply({"merges": [{"key": "a", "canonical": "a"}]})])
    assert ds.json("s", "u", pipeline.MergeMap).merges[0].key == "a"
    retry = json.loads(route.calls[1].request.content)["messages"]
    assert retry[-2]["role"] == "assistant" and "invalid" in retry[-1]["content"]


@respx.mock
def test_deepseek_retries_rate_limits_and_rejects_truncation(ds):
    respx.post(llm.DeepSeek.url).mock(side_effect=[httpx.Response(429), ds_reply("{}", finish="length")])
    with pytest.raises(RuntimeError, match="truncated"):
        ds.json("s", "u", pipeline.MergeMap)


def test_load_signals_folds_autocomplete_by_seed(tmp_path):
    rows = [{"source": "autocomplete", "title": t, "body": "Search suggestion for 'hail cover for'"} for t in
            ["hail cover for suv", "hail cover for truck", "hail cover for suv"]]
    rows.append({"source": "reddit", "title": "", "body": "[removed]"})
    rows.append({"source": "reddit", "title": "Need boots", "body": "x"})
    f = tmp_path / "s.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    sig = pipeline.load_signals(f)
    assert [s["source"] for s in sig] == ["reddit", "autocomplete"]
    assert sig[1]["body"] == "hail cover for suv; hail cover for truck"
    assert [s["signal_id"] for s in sig] == [0, 1]


class FakeLLM(llm.LLM):
    name = "fake"

    def __init__(self, *outs):
        self.outs = list(outs)
        self.calls = []

    def json(self, system, user, schema, max_tokens=16000):
        self.calls.append((system, user, schema))
        out = self.outs.pop(0)
        if isinstance(out, Exception):
            raise out
        return schema.model_validate(out)


def pain(sid, key, intent=3, demand=True, geo=""):
    return {"signal_id": sid, "is_demand": demand, "cluster_key": key, "pain": f"pain {sid}", "context": "",
            "audience": "", "quote": "q", "intent_level": intent, "product_hint": "h", "geo": geo}


def test_extract_keeps_demand_in_batch_and_survives_failed_batch():
    signals = [{"signal_id": i, "source": "reddit", "title": "t", "body": "b"} for i in range(4)]
    fake = FakeLLM({"items": [pain(0, "Dog Boots!"), pain(1, "x", demand=False), pain(99, "y")]},
                   RuntimeError("boom"))
    pains, errors = pipeline.extract(fake, signals, batch=2, workers=1)
    assert [(p["signal_id"], p["cluster_key"]) for p in pains] == [(0, "dog-boots")]
    assert errors == ["batch 2-3: RuntimeError: boom"]


def test_merge_aggregate_score_end_to_end():
    signals = [{"signal_id": i, "source": "reddit" if i % 2 else "stackexchange:diy", "url": f"u{i}"} for i in range(5)]
    pains = [pain(0, "car-hail-cover", 5, geo="calgary"), pain(1, "hail-protection-car", 4),
             pain(2, "car-hail-cover", 3), pain(3, "dog-toys", 2), pain(4, "dog-toys", 2)]
    fake = FakeLLM({"merges": [{"key": "hail-protection-car", "canonical": "car-hail-cover"}]})
    mapping = pipeline.merge_clusters(fake, pains)
    assert mapping == {"car-hail-cover": "car-hail-cover", "hail-protection-car": "car-hail-cover", "dog-toys": "dog-toys"}
    for p in pains:
        p["cluster_key"] = mapping[p["cluster_key"]]
    clusters = pipeline.aggregate(pains, signals, min_mentions=2)
    assert [c["cluster_key"] for c in clusters] == ["car-hail-cover", "dog-toys"]
    assert clusters[0]["mentions"] == 3 and clusters[0]["sources"] == 2 and clusters[0]["local_mentions"] == 1
    opp = {"title": "Hail cover", "demand": 8, "margin": 8, "gap": 8, "logistics": 8, "compliance": 10,
           "target_price_cad": "60-90", "brief_md": "b"}
    fake = FakeLLM({"opportunities": [dict(opp, cluster_key="car-hail-cover"), dict(opp, cluster_key="invented")]})
    rows = pipeline.score(fake, clusters)
    assert [r["cluster_key"] for r in rows] == ["car-hail-cover"]
    assert rows[0]["total_score"] == 82.0
    assert "evidence_urls" not in fake.calls[0][1]  # links stay out of the prompt
    md = pipeline.report(rows, 5, 5, 2, "fake", [])
    assert "| 1 | Hail cover | 82.0 | 3 |" in md
