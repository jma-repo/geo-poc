"""Parser tests use response shapes from each API's public documentation.
They prove the mapping logic, not that the live APIs still answer this way: run `--check` for that."""
import json
import os
import shutil
import subprocess

import httpx
import pytest
import respx

from collectors import run, sources


@pytest.fixture
def http():
    return sources.Http(delay=0)


@respx.mock
def test_arctic_shift_pages_and_filters_locally(http):
    def post(i, title):
        return {"id": f"p{i}", "title": title, "selftext": "", "author": "someone",
                "permalink": f"/r/Calgary/comments/p{i}/x/", "created_utc": 1762000000 + i}
    page1 = [post(i, "Where can I buy a hail cover?" if i == 195 else "Traffic on Deerfoot") for i in range(199, 99, -1)]
    page2 = [post(50, "Can anyone recommend ice cleats")]
    posts = respx.get("https://arctic-shift.photon-reddit.com/api/posts/search").mock(
        side_effect=[httpx.Response(200, json={"data": page1}), httpx.Response(200, json={"data": page2})])
    respx.get("https://arctic-shift.photon-reddit.com/api/comments/search").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "c1", "body": "I wish there was a cheaper one", "author": "other", "link_id": "t3_p5",
                  "created_utc": 1762000100}, {"id": "c2", "body": "lol", "author": "x", "created_utc": 1762000101}]}))
    rows = list(sources.arctic_shift(http, ["Calgary"], ["recommend", '"where can I buy"', '"wish there was"'], 7))
    assert [r["external_id"] for r in rows] == ["p195", "p50", "c_c1"]
    assert posts.calls[0].request.url.params["sort"] == "desc"
    assert posts.calls[1].request.url.params["before"] == str(1762000100)  # next page ends at oldest seen
    assert "query" not in posts.calls[0].request.url.params              # no server-side full-text search
    assert rows[2]["url"] == "https://www.reddit.com/r/Calgary/comments/p5/_/c1/"
    assert all(r["geo_hint"] == "calgary" for r in rows) and "someone" not in json.dumps(rows)


@respx.mock
def test_arctic_shift_error_payload_raises(http):
    respx.get("https://arctic-shift.photon-reddit.com/api/posts/search").mock(
        return_value=httpx.Response(200, json={"data": None, "error": "Timeout. Maybe slow down a bit"}))
    with pytest.raises(RuntimeError, match="slow down"):
        list(sources.arctic_shift(http, ["Calgary"], ["x"], 1))


@respx.mock
def test_hn(http):
    respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(return_value=httpx.Response(200, json={"hits": [
        {"objectID": "1", "title": None, "story_title": "Ask HN: winter gear", "comment_text": "Need boots",
         "author": "a", "created_at_i": 1762000000}]}))
    (r,) = sources.hn(http, ["boots"], 7)
    assert r["title"] == "Ask HN: winter gear" and r["body"] == "Need boots"
    assert r["posted_at"].startswith("2025-11-01")


@respx.mock
def test_stackexchange_uses_key_when_set(http, monkeypatch):
    monkeypatch.setenv("STACKEXCHANGE_KEY", "k")
    route = respx.get("https://api.stackexchange.com/2.3/search/advanced").mock(return_value=httpx.Response(200, json={
        "items": [{"question_id": 9, "title": "Protect car from hail?", "body": "<p>How?</p>",
                   "link": "https://diy.stackexchange.com/q/9", "owner": {"user_id": 5}, "creation_date": 1762000000}]}))
    (r,) = sources.stackexchange(http, ["diy"], ["hail"], 7)
    assert r["source"] == "stackexchange:diy" and r["external_id"] == "9"
    assert route.calls[0].request.url.params["key"] == "k"


@respx.mock
def test_autocomplete_expands_a_to_z(http):
    route = respx.get("https://suggestqueries.google.com/complete/search").mock(
        side_effect=lambda req: httpx.Response(200, json=[req.url.params["q"], [req.url.params["q"] + " suv"]]))
    rows = list(sources.autocomplete(http, ["hail cover for"]))
    assert len(route.calls) == 27 and len(rows) == 27
    assert rows[1]["title"] == "hail cover for a suv"


@respx.mock
def test_youtube_skips_videos_with_comments_disabled(http, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "y")
    api = "https://www.googleapis.com/youtube/v3"
    respx.get(f"{api}/search").mock(return_value=httpx.Response(200, json={"items": [
        {"id": {"videoId": "v1"}, "snippet": {"title": "Hail cover test"}},
        {"id": {"videoId": "v2"}, "snippet": {"title": "No comments"}}]}))
    respx.get(f"{api}/commentThreads", params={"videoId": "v1"}).mock(return_value=httpx.Response(200, json={"items": [
        {"id": "t1", "snippet": {"topLevelComment": {"snippet": {"textDisplay": "Does it fit a RAV4?",
                                                                    "authorChannelId": {"value": "ch"},
                                                                    "publishedAt": "2026-11-02T00:00:00Z"}}}}]}))
    respx.get(f"{api}/commentThreads", params={"videoId": "v2"}).mock(return_value=httpx.Response(403))
    rows = list(sources.youtube(http, ["hail cover"], 7))
    assert [r["external_id"] for r in rows] == ["t1"]


@respx.mock
def test_retry_on_429_then_success(monkeypatch):
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    route = respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(
        side_effect=[httpx.Response(429, headers={"retry-after": "1"}), httpx.Response(200, json={"hits": []})])
    assert list(sources.hn(sources.Http(delay=0), ["x"], 1)) == []
    assert route.call_count == 2


@respx.mock
def test_one_failing_source_does_not_stop_others(http, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(return_value=httpx.Response(200, json={"hits": [
        {"objectID": "1", "title": "t", "author": "a", "created_at_i": 1762000000}]}))
    respx.get("https://api.stackexchange.com/2.3/search/advanced").mock(return_value=httpx.Response(400))
    cfg = {"queries": ["x"], "stackexchange_sites": ["diy"]}
    rows, report = run.collect(["hn", "stackexchange", "youtube"], cfg, 7, http)
    assert len(rows) == 1
    assert report["hn"] == "ok: 1 rows"
    assert report["stackexchange"].startswith("error: HTTPStatusError")
    assert report["youtube"] == "skipped: set YOUTUBE_API_KEY"


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL") or not shutil.which("psql"), reason="needs TEST_DATABASE_URL")
def test_load_postgres_dedupes_and_handles_large_batches():
    url = os.environ["TEST_DATABASE_URL"]
    schema = open(os.path.join(os.path.dirname(__file__), "../../db/schema.sql")).read()
    subprocess.run(["psql", url, "-q", "-v", "ON_ERROR_STOP=1"], input="DROP TABLE IF EXISTS conversions, partner_codes, "
                   "outreach_drafts, waitlist, merchants, opportunities, pain_points, raw_signals CASCADE;" + schema,
                   text=True, check=True, capture_output=True)
    rows = [sources.row("hackernews", i, None, "t", "x" * 8000 + "$rows$ 'quote'", "a", None, 1762000000) for i in range(1200)]
    assert run.load_postgres(rows, url) == 1200   # ~10 MB, three batches, body contains the default dollar tag
    assert run.load_postgres(rows[:10], url) == 0  # duplicates ignored
