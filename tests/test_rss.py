"""RSS source: feed list, polite fetching (honest identity, conditional GET, size cap, refusals)
and parsing (HTML, caps, time rules, exchange fields). Synthetic feeds only, no network."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from news_fakes import T0, FeedServer, atom, item, rss

from market_intel.config import REPO_ROOT
from market_intel.sources import rss as rss_mod
from market_intel.sources.rss import Feed, FeedConfigError, FeedFetchError, load_feeds, parse

NEWS = Feed("fake_news", "https://news.example.invalid/feed", "news", "Fake Times")
NSE = Feed("fake_nse", "https://nse.example.invalid/feed", "exchange", "NSE")
BSE = Feed("fake_bse", "https://bse.example.invalid/feed", "exchange", "BSE")


# --- feed list ---------------------------------------------------------------------------


def test_the_committed_feed_list_loads():
    feeds = load_feeds(REPO_ROOT / "config" / "news_feeds.toml")
    assert len(feeds) == 11
    assert sum(f.kind == "news" for f in feeds) == 6
    assert sum(f.kind == "exchange" for f in feeds) == 5
    assert all(f.url.startswith("https://") and f.enabled for f in feeds)
    assert all(f.min_interval_minutes == 10 for f in feeds)
    assert not any("moneycontrol" in f.url for f in feeds)  # excluded: blocks non-browsers


def write_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "feeds.toml"
    path.write_text(body, encoding="utf-8")
    return path


GOOD = '[[feed]]\nname = "a"\nurl = "https://x.invalid/f"\nkind = "news"\npublisher = "P"\n'


def test_defaults_and_options(tmp_path):
    body = GOOD + "min_interval_minutes = 30\nenabled = false\n"
    [feed] = load_feeds(write_toml(tmp_path, body))
    assert (feed.min_interval_minutes, feed.enabled, feed.source) == (30, False, "rss:a")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("", "no \\[\\[feed\\]\\] entries"),
        (GOOD.replace("https://", "http://"), "must start with https"),
        (GOOD.replace('"news"', '"blog"'), "kind must be news or exchange"),
        (GOOD.replace('name = "a"', 'name = "Bad Name"'), "lower-case"),
        (GOOD + GOOD, "duplicate name"),
        (GOOD + "colour = 1\n", "unknown keys"),
        (GOOD + "min_interval_minutes = 0\n", "must be >= 1"),
        (GOOD.replace('publisher = "P"\n', ""), "publisher is required"),
        ("not toml [[", "not valid TOML"),
    ],
)
def test_bad_feed_lists_are_refused(tmp_path, body, message):
    with pytest.raises(FeedConfigError, match=message):
        load_feeds(write_toml(tmp_path, body))


def test_a_missing_feed_list_is_a_clear_error(tmp_path):
    with pytest.raises(FeedConfigError, match="not found"):
        load_feeds(tmp_path / "nope.toml")


# --- fetching ----------------------------------------------------------------------------


def get(server: FeedServer, feed: Feed = NEWS, **kw):
    return rss_mod.fetch(server.client(), feed, max_bytes=kw.pop("max_bytes", 10_000), now=T0, **kw)


def test_fetch_sends_an_honest_user_agent_and_conditional_headers():
    server = FeedServer({NEWS.url: rss(item())})
    first = get(server)
    assert first.content and first.etag
    second = get(server, etag=first.etag, last_modified="Sun, 22 Nov 2026 05:00:00 GMT")
    assert second.content is None  # 304: not modified
    headers = server.requests[1].headers
    assert headers["If-None-Match"] == first.etag
    assert headers["If-Modified-Since"] == "Sun, 22 Nov 2026 05:00:00 GMT"
    for request in server.requests:
        agent = request.headers["User-Agent"]
        assert agent.startswith("market-intel/") and "personal research; RSS reader" in agent
        assert "Mozilla" not in agent and "Chrome" not in agent  # never a browser
    assert [str(r.url) for r in server.requests] == [NEWS.url, NEWS.url]  # only the feed


def test_the_size_cap_applies_to_the_body_and_to_content_length():
    server = FeedServer({NEWS.url: rss(*(item(link=f"https://n.invalid/{i}") for i in range(50)))})
    with pytest.raises(FeedFetchError, match="larger than 500 bytes"):
        get(server, max_bytes=500)
    claims = FeedServer(
        {NEWS.url: httpx.Response(200, headers={"Content-Length": "999999"}, content=b"x")}
    )
    with pytest.raises(FeedFetchError, match="too large"):
        get(claims)


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_refusals_are_one_request_with_the_same_identity(status):
    server = FeedServer({NEWS.url: httpx.Response(status, headers={"Retry-After": "120"})})
    with pytest.raises(FeedFetchError, match=f"HTTP {status}") as caught:
        get(server)
    assert caught.value.retry_after == timedelta(seconds=120)
    assert len(server.requests) == 1  # no retry, so never another identity


def test_network_errors_become_fetch_errors():
    def boom(request):
        raise httpx.ConnectError("refused")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(FeedFetchError, match="ConnectError"):
        rss_mod.fetch(client, NEWS, max_bytes=1000, now=T0)


def test_retry_after_reads_seconds_and_dates_and_is_capped():
    assert rss_mod.retry_after("30", T0) == timedelta(seconds=30)
    later = (T0 + timedelta(minutes=5)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert rss_mod.retry_after(later, T0) == timedelta(minutes=5)
    assert rss_mod.retry_after("999999999", T0) == timedelta(hours=24)
    assert rss_mod.retry_after("soon", T0) is None and rss_mod.retry_after(None, T0) is None


# --- parsing -----------------------------------------------------------------------------


def test_titles_and_summaries_lose_html_and_are_capped():
    long_title = "<b>" + "T" * 600 + "</b>"
    parsed = parse(NEWS, rss(item(long_title, description="<div>" + "S " * 1500 + "</div>")), T0)
    [record] = parsed.records
    assert "<" not in record.title and len(record.title) == 500 and record.title.endswith("…")
    assert len(record.summary) == 2000
    plain = parse(NEWS, rss(item()), T0).records[0]
    assert plain.summary == "Fake summary & more"


def test_item_fields_and_source_tags():
    [record] = parse(NEWS, rss(item()), T0 + timedelta(minutes=1)).records
    assert record.source == "rss:fake_news" and record.feed_name == "fake_news"
    assert (record.kind, record.publisher, record.feed_url) == ("news", "Fake Times", NEWS.url)
    assert record.url == "https://news.example.invalid/a1"
    assert record.exchange_meta is None and record.symbols == []


def test_time_rules_feed_missing_and_future():
    fetched = T0 + timedelta(minutes=30)
    feed_time = parse(NEWS, rss(item(pub=T0)), fetched).records[0]
    assert (feed_time.published_at, feed_time.published_at_source) == (T0, "feed")
    missing = parse(NEWS, rss(item(pub=None)), fetched)
    assert (missing.records[0].published_at, missing.records[0].published_at_source) == (
        fetched,
        "fetched",
    )
    assert missing.without_time == 1
    future = parse(NEWS, rss(item(pub=fetched + timedelta(minutes=6))), fetched).records[0]
    assert (future.published_at, future.published_at_source) == (fetched, "clamped")
    near = parse(NEWS, rss(item(pub=fetched + timedelta(minutes=4))), fetched).records[0]
    assert near.published_at_source == "feed"  # within the 5-minute tolerance


def test_available_at_is_the_later_of_published_and_fetched():
    fetched = T0 + timedelta(minutes=30)
    doc = parse(NEWS, rss(item(pub=T0)), fetched).records[0].to_document()
    assert doc.available_at == fetched
    early = parse(NEWS, rss(item(pub=T0)), T0 - timedelta(minutes=3)).records[0].to_document()
    assert early.available_at == T0


def test_feed_times_are_converted_to_utc():
    ist = "Sun, 22 Nov 2026 11:30:00 +0530"
    [record] = parse(NEWS, rss(item(pub=ist)), T0 + timedelta(hours=1)).records
    assert record.published_at == datetime(2026, 11, 22, 6, 0, tzinfo=UTC)


def test_atom_feeds_parse_too():
    [record] = parse(
        NEWS, atom("Fake atom", "https://n.invalid/x", "2026-11-22T05:00:00Z"), T0
    ).records
    assert record.published_at == datetime(2026, 11, 22, 5, 0, tzinfo=UTC)


def test_entries_without_link_or_title_are_counted_as_malformed():
    parsed = parse(
        NEWS,
        rss(
            item(),
            item(title=None, link="https://n.invalid/2"),
            item(title="No link", link=None),
            item(title="Bad link", link="javascript:alert(1)"),
            item(),  # same link again: kept once
        ),
        T0,
    )
    assert (len(parsed.records), parsed.malformed, parsed.entries) == (1, 3, 4)


def test_garbage_is_not_a_recognised_feed():
    parsed = parse(NEWS, b"<html><body>Access denied</body></html>", T0)
    assert parsed.records == [] and not parsed.recognised
    empty = parse(NEWS, rss(), T0)
    assert empty.records == [] and empty.recognised  # a well-formed feed that is empty


def test_nse_items_carry_company_and_subject_with_ist_times():
    body = rss(
        item(
            "Fake Alpha Limited",
            "https://nse.example.invalid/f1.pdf",
            "Fake Alpha Limited has informed the Exchange |SUBJECT: Fake board outcome",
            pub="22-Nov-2026 11:00:00",
        ),
        item(
            "Fake Beta Limited",
            "https://nse.example.invalid/f2.pdf",
            "TYPE OF SECURITY : x",
            pub="22-Nov-2026 10:45",
        ),
    )
    first, second = parse(NSE, body, T0).records
    assert first.kind == "exchange"
    meta = first.exchange_meta
    assert (meta.exchange, meta.company_raw, meta.symbol_raw) == ("NSE", "Fake Alpha Limited", None)
    assert meta.subject == "Fake board outcome"
    assert meta.attachment_url == "https://nse.example.invalid/f1.pdf"  # stored, never fetched
    assert first.published_at == datetime(2026, 11, 22, 5, 30, tzinfo=UTC)  # 11:00 IST
    assert second.published_at == datetime(2026, 11, 22, 5, 15, tzinfo=UTC)
    assert second.exchange_meta.subject is None and first.published_at_source == "feed"


def test_bse_items_carry_the_scrip_code():
    body = rss(
        item(
            "Fake Gamma Ltd (500001)",
            "https://bse.example.invalid/g.pdf",
            "Fake intimation of board meeting",
            pub="22-Nov-2026 11:15:00",
            extra="<scripcode>500001</scripcode>",
        )
    )
    meta = parse(BSE, body, T0).records[0].exchange_meta
    assert (meta.exchange, meta.company_raw, meta.symbol_raw) == ("BSE", "Fake Gamma Ltd", "500001")
    assert meta.subject == "Fake intimation of board meeting"
