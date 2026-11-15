"""Free data-source collectors. Each returns rows in the raw_signals shape:

    {source, external_id, url, title, body, author_hash, geo_hint, posted_at}

No API key needed: arctic_shift (Reddit archive), hn (Hacker News via Algolia),
stackexchange, autocomplete (Google suggestions), bluesky.
Free key needed: youtube (YouTube Data API v3).
"""
from __future__ import annotations

import hashlib
import os
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

import httpx

UA = os.environ.get("COLLECTOR_USER_AGENT", "demand-radar/0.1 (research; contact via repo)")


def author_hash(name) -> str | None:
    return hashlib.sha256(str(name).encode()).hexdigest()[:16] if name else None


def iso(ts: float | int | str | None) -> str | None:
    if ts in (None, ""):
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()


def row(source, external_id, url, title, body, author=None, geo=None, posted=None) -> dict:
    return {"source": source, "external_id": str(external_id), "url": url,
            "title": (title or "")[:500], "body": (body or "")[:8000],
            "author_hash": author_hash(author), "geo_hint": geo, "posted_at": iso(posted)}


class Http:
    """httpx client with a polite delay between requests and retry on 429/5xx."""

    def __init__(self, delay: float = 1.0, client: httpx.Client | None = None):
        self.delay = delay
        self.client = client or httpx.Client(timeout=30, headers={"User-Agent": UA}, follow_redirects=True)
        self._last = 0.0

    def get(self, url: str, **params):
        for attempt in range(4):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            r = self.client.get(url, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("retry-after", 2 ** (attempt + 1))))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        raise RuntimeError(f"gave up on {url} after retries (last status {r.status_code})")


GEO_SUBS = {"calgary": "calgary", "alberta": "alberta", "edmonton": "alberta",
            "canada": "canada", "personalfinancecanada": "canada", "buycanadian": "canada"}


def arctic_shift(http: Http, subreddits: list[str], queries: list[str], days: int) -> Iterable[dict]:
    """Reddit posts and comments via Arctic Shift, an open-source public Reddit archive. No key."""
    base = "https://arctic-shift.photon-reddit.com/api"
    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    for sub in subreddits:
        geo = GEO_SUBS.get(sub.lower())
        for q in queries:
            for kind, field in (("posts", "query"), ("comments", "body")):
                data = http.get(f"{base}/{kind}/search", subreddit=sub, **{field: q}, after=after, limit=100)
                for d in data.get("data") or []:
                    if kind == "posts":
                        yield row("reddit", d["id"], f"https://www.reddit.com{d.get('permalink', '')}",
                                  d.get("title"), d.get("selftext"), d.get("author"), geo, d.get("created_utc"))
                    else:
                        yield row("reddit", f"c_{d['id']}",
                                  f"https://www.reddit.com/r/{sub}/comments/{str(d.get('link_id', '')).removeprefix('t3_')}/_/{d['id']}/",
                                  "", d.get("body"), d.get("author"), geo, d.get("created_utc"))


def hn(http: Http, queries: list[str], days: int) -> Iterable[dict]:
    """Hacker News stories and comments via the Algolia HN Search API. No key."""
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    for q in queries:
        data = http.get("https://hn.algolia.com/api/v1/search_by_date", query=q, tags="(story,comment)",
                        numericFilters=f"created_at_i>{since}", hitsPerPage=100)
        for h in data.get("hits", []):
            yield row("hackernews", h["objectID"], f"https://news.ycombinator.com/item?id={h['objectID']}",
                      h.get("title") or h.get("story_title"), h.get("story_text") or h.get("comment_text"),
                      h.get("author"), None, h.get("created_at_i"))


def stackexchange(http: Http, sites: list[str], queries: list[str], days: int) -> Iterable[dict]:
    """Stack Exchange questions. No key: 300 requests/day per IP; a free key raises it to 10,000."""
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    key = os.environ.get("STACKEXCHANGE_KEY")
    for site in sites:
        for q in queries:
            params = dict(q=q, site=site, fromdate=since, order="desc", sort="creation", filter="withbody", pagesize=100)
            if key:
                params["key"] = key
            data = http.get("https://api.stackexchange.com/2.3/search/advanced", **params)
            for it in data.get("items", []):
                yield row(f"stackexchange:{site}", it["question_id"], it.get("link"), it.get("title"),
                          it.get("body"), (it.get("owner") or {}).get("user_id"), None, it.get("creation_date"))
            if data.get("backoff"):
                time.sleep(int(data["backoff"]))


def autocomplete(http: Http, seeds: list[str], country: str = "ca") -> Iterable[dict]:
    """Google search suggestions: what people type after a seed phrase. Unofficial endpoint; keep volume low."""
    for seed in seeds:
        for suffix in ["", *(" " + c for c in string.ascii_lowercase)]:
            data = http.get("https://suggestqueries.google.com/complete/search",
                            client="firefox", hl="en", gl=country, q=seed + suffix)
            for s in (data[1] if isinstance(data, list) and len(data) > 1 else []):
                yield row("autocomplete", f"{country}:{s}", None, s, f"Search suggestion for '{seed}'", None,
                          "canada" if country == "ca" else None, None)


def bluesky(http: Http, queries: list[str], days: int) -> Iterable[dict]:
    """Bluesky public post search. The public AppView may require auth for search; then this yields nothing."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for q in queries:
        data = http.get("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts", q=q, since=since, limit=100)
        for p in data.get("posts", []):
            handle = (p.get("author") or {}).get("handle", "")
            rkey = p.get("uri", "").rsplit("/", 1)[-1]
            yield row("bluesky", p.get("uri"), f"https://bsky.app/profile/{handle}/post/{rkey}", "",
                      (p.get("record") or {}).get("text"), handle, None, (p.get("record") or {}).get("createdAt"))


def youtube(http: Http, queries: list[str], days: int, videos_per_query: int = 5) -> Iterable[dict]:
    """YouTube comments on recent videos matching each query. Free key, 10,000 quota units/day:
    a search costs 100 units, a comment page 1 unit."""
    key = os.environ["YOUTUBE_API_KEY"]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    api = "https://www.googleapis.com/youtube/v3"
    for q in queries:
        found = http.get(f"{api}/search", part="snippet", q=q, type="video", publishedAfter=since,
                         regionCode="CA", relevanceLanguage="en", maxResults=videos_per_query, key=key)
        for v in found.get("items", []):
            vid = v["id"]["videoId"]
            try:
                threads = http.get(f"{api}/commentThreads", part="snippet", videoId=vid, maxResults=100,
                                   order="relevance", textFormat="plainText", key=key)
            except httpx.HTTPStatusError:
                continue  # comments disabled
            for t in threads.get("items", []):
                c = t["snippet"]["topLevelComment"]["snippet"]
                yield row("youtube", t["id"], f"https://www.youtube.com/watch?v={vid}&lc={t['id']}",
                          v["snippet"].get("title"), c.get("textDisplay"), c.get("authorChannelId", {}).get("value"),
                          None, c.get("publishedAt"))


COLLECTORS: dict[str, Callable] = {
    "arctic_shift": lambda http, cfg, days: arctic_shift(http, cfg["subreddits"], cfg["reddit_queries"], days),
    "hn": lambda http, cfg, days: hn(http, cfg["queries"], days),
    "stackexchange": lambda http, cfg, days: stackexchange(http, cfg["stackexchange_sites"], cfg["queries"], days),
    "autocomplete": lambda http, cfg, days: autocomplete(http, cfg["autocomplete_seeds"]),
    "bluesky": lambda http, cfg, days: bluesky(http, cfg["queries"], days),
    "youtube": lambda http, cfg, days: youtube(http, cfg["queries"], days),
}
NEEDS_KEY = {"youtube": "YOUTUBE_API_KEY"}
