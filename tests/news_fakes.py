"""Synthetic RSS/Atom feeds and helpers for the news tests. Everything here is made up: fake
companies, example.invalid links; no real feed content is stored in the repo."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime
from xml.sax.saxutils import escape

import httpx

T0 = datetime(2026, 11, 22, 6, 0, tzinfo=UTC)  # 11:30 IST


def item(
    title: str | None = "Fake headline",
    link: str | None = "https://news.example.invalid/a1",
    description: str | None = "<p>Fake <b>summary</b> &amp; more</p>",
    pub: str | datetime | None = T0,
    extra: str = "",
) -> str:
    parts = ["<item>"]
    if title is not None:
        parts.append(f"<title>{escape(title)}</title>")
    if link is not None:
        parts.append(f"<link>{escape(link)}</link>")
    if description is not None:
        parts.append(f"<description>{escape(description)}</description>")
    if isinstance(pub, datetime):
        parts.append(f"<pubDate>{format_datetime(pub)}</pubDate>")
    elif isinstance(pub, str):
        parts.append(f"<pubDate>{escape(pub)}</pubDate>")
    parts.append(extra)
    parts.append("</item>")
    return "".join(parts)


def rss(*items: str) -> bytes:
    body = "".join(items)
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>Fake feed</title><link>https://news.example.invalid/</link>{body}"
        "</channel></rss>"
    ).encode()


def atom(title: str, link: str, updated: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
        "<title>Fake atom</title><id>urn:fake</id><updated>2026-11-22T06:00:00Z</updated>"
        f'<entry><title>{escape(title)}</title><link href="{escape(link)}"/>'
        f"<id>urn:fake:1</id><updated>{updated}</updated></entry></feed>"
    ).encode()


def many(n: int, prefix: str = "a") -> bytes:
    return rss(
        *(item(f"Fake headline {i}", f"https://news.example.invalid/{prefix}{i}") for i in range(n))
    )


class FeedServer:
    """Serves fake feeds by URL with ETags; answers 304 to a matching If-None-Match.

    ``routes`` maps a URL to bytes, or to an ``httpx.Response`` (errors), or to a callable.
    Every request is recorded, so tests can check headers and that only feed URLs were asked.
    """

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(str(request.url))
        if route is None:
            return httpx.Response(404)
        if callable(route) and not isinstance(route, httpx.Response):
            route = route(request)
        if isinstance(route, httpx.Response):
            return route
        etag = f'"{abs(hash(route))}"'
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        return httpx.Response(
            200,
            stream=httpx.ByteStream(route),
            headers={"ETag": etag, "Content-Type": "application/rss+xml"},
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))
