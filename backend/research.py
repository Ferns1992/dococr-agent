"""Web research for DocChat: search the public web and read the best sources.

Two independent sources, no API key required:

* Wikipedia REST summary + the extracts API, which is fast, structured and
  dependable for factual lookups.
* DuckDuckGo's HTML endpoint for everything else, parsed to pull result
  titles/links, then the top pages are fetched and reduced to text so the
  model answers from the page rather than from a search snippet.

Everything here is best-effort: any failure degrades to "no web results"
rather than raising, because a research answer with fewer sources is still
useful.
"""

from __future__ import annotations

import asyncio
import re
from typing import List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx

import config

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 DocChat/1.0"
)

WIKI_API = "https://en.wikipedia.org/api/rest_v1/page/summary/"
WIKI_SEARCH = "https://en.wikipedia.org/w/api.php"
DDG_HTML = "https://html.duckduckgo.com/html/"

# Pages that are never useful as a research source.
BLOCKED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "pinterest.com", "www.pinterest.com",
    "amazon.com", "www.amazon.com",
    "facebook.com", "www.facebook.com",
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com",
    "twitter.com", "x.com", "www.x.com",
    "linkedin.com", "www.linkedin.com",
}

_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")


def _clean(html: str) -> str:
    text = _SCRIPT.sub(" ", html)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text, flags=re.I)
    text = _TAG.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&rsquo;", "'")
        .replace("&mdash;", "-")
    )
    text = _WS.sub(" ", text)
    return _NL.sub("\n\n", text).strip()


def _ddg_dest(href: str) -> Optional[str]:
    """DuckDuckGo wraps outbound links in /l/?uddg=<encoded>."""
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target) or None
    if parsed.scheme in ("http", "https"):
        return href
    return None


def _results_from_html(html: str, limit: int) -> List[dict]:
    out: List[dict] = []
    seen = set()
    for match in re.finditer(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html,
        re.I | re.S,
    ):
        url = _ddg_dest(match.group(1))
        if not url:
            continue
        host = urlparse(url).netloc.lower()
        if not host or host in BLOCKED_HOSTS or host.endswith(".pdf"):
            continue
        title = _clean(match.group(2))
        if not title or url in seen:
            continue
        seen.add(url)
        out.append({"title": title, "url": url, "snippet": ""})
        if len(out) >= limit:
            break
    return out


async def _wikipedia(client: httpx.AsyncClient, query: str) -> List[dict]:
    try:
        r = await client.get(
            WIKI_SEARCH,
            params={
                "action": "query", "format": "json", "generator": "search",
                "gsrsearch": query, "gsrlimit": 1, "prop": "extracts",
                "exintro": 1, "explaintext": 1, "redirects": 1,
            },
            timeout=20.0,
        )
        if r.status_code != 200:
            return []
        pages = (r.json().get("query") or {}).get("pages") or {}
        results = []
        for page in pages.values():
            extract = (page.get("extract") or "").strip()
            if len(extract) < 200:
                continue
            title = page.get("title", "")
            results.append({
                "title": title,
                "url": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
                "snippet": extract[:2500],
                "text": extract[:4000],
            })
        return results
    except Exception:
        return []


async def _duckduckgo(client: httpx.AsyncClient, query: str, limit: int) -> List[dict]:
    try:
        r = await client.get(DDG_HTML, params={"q": query}, headers={"User-Agent": UA}, timeout=25.0)
        if r.status_code != 200:
            return []
        return _results_from_html(r.text, limit)
    except Exception:
        return []


async def _read_page(client: httpx.AsyncClient, url: str, limit: int = 4500) -> str:
    try:
        r = await client.get(
            url, headers={"User-Agent": UA}, follow_redirects=True, timeout=25.0
        )
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or "html" not in ctype and "text" not in ctype:
            return ""
        if "text/plain" in ctype:
            return r.text[:limit]
        return _clean(r.text)[:limit]
    except Exception:
        return ""


async def search(client: httpx.AsyncClient, query: str, limit: int = 5) -> List[dict]:
    """Return a de-duplicated list of {title,url,text} research sources."""
    wiki, web = await asyncio.gather(
        _wikipedia(client, query),
        _duckduckgo(client, query, limit),
    )

    results: List[dict] = []
    seen = set()

    for item in wiki:
        results.append(item)
        seen.add(item["url"])

    # Fetch the top pages concurrently; a page that fails is simply skipped.
    readable = await asyncio.gather(
        *(_read_page(client, item["url"]) for item in web[:limit])
    )
    for item, text in zip(web, readable):
        if not text or len(text) < 240:
            continue
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        results.append({**item, "text": text})

    return results[: limit + 1]


def build_web_context(sources: List[dict]) -> str:
    blocks = []
    for i, item in enumerate(sources, start=1):
        body = (item.get("text") or item.get("snippet") or "").strip()
        if not body:
            continue
        blocks.append(f"[W{i}] {item['title']} — {item['url']}\n{body}")
    return "\n\n".join(blocks)
