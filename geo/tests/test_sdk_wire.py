"""Drive the real anthropic SDK through a mock transport and check the exact JSON it would send.
Catches SDK signature or serialization mismatches that fakes cannot."""
import json

import anthropic
import httpx2

from geo.llm import Claude
from geo.measure import engines, panel

CANNED = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
    "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


def client_capturing(sent, content):
    def handler(request: httpx2.Request):
        sent.append({"url": str(request.url), "headers": dict(request.headers), "body": json.loads(request.content)})
        return httpx2.Response(200, json={**CANNED, "content": content})
    return anthropic.Anthropic(api_key="test", max_retries=0,
                               http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def test_structured_call_on_the_wire():
    sent = []
    body = {"prompts": [{"text": "q", "persona": "p", "intent": "local", "fanout": ["a"]}]}
    out = Claude(client_capturing(sent, [{"type": "text", "text": json.dumps(body)}])).structured("sys", "user", panel.PanelDraft)
    assert out.prompts[0].fanout == ["a"]
    req = sent[0]
    assert req["url"] == "https://api.anthropic.com/v1/messages"
    assert "server-side-fallback-2026-07-01" in req["headers"]["anthropic-beta"]
    b = req["body"]
    assert b["model"] == "claude-opus-5-5" and b["fallbacks"] == "default"
    assert b["output_config"]["effort"] == "medium"
    assert b["output_config"]["format"]["type"] == "json_schema"
    assert "prompts" in b["output_config"]["format"]["schema"]["properties"]


def test_probe_call_on_the_wire():
    sent = []
    content = [{"type": "text", "text": "Use a cover.",
                "citations": [{"type": "web_search_result_location", "url": "https://a.example/1",
                               "title": "A", "encrypted_index": "x", "cited_text": "c"}]}]
    a = engines.ClaudeEngine("claude-opus-5-5", client=client_capturing(sent, content)).ask("q")
    assert a.error is None, a.error
    assert a.cited == ["https://a.example/1"]
    tool = sent[0]["body"]["tools"][0]
    assert tool["type"] == "web_search_20260209" and tool["user_location"]["country"] == "CA"
