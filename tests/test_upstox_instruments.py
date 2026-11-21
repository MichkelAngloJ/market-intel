"""Upstox instrument file: download (conditional GET, caps, rotation) and untrusted parsing.
No network: httpx.MockTransport. Test files are built here from small FAKE objects."""

import gzip
from datetime import date
from decimal import Decimal

import httpx
import pytest
from instrument_fakes import Server, eq, gz_bytes, index, write_gz

from market_intel.sources import upstox_instruments as ui

URL = "https://assets.example.invalid/instruments/NSE.json.gz"
TODAY = date(2026, 11, 22)


# --- parsing and filtering ---------------------------------------------------------------


def test_keeps_eq_be_and_indices_and_counts_the_rest(tmp_path):
    objects = [
        eq("FAKEA", "INE000A01011"),
        eq("FAKEB", "INE000B01012", series="BE"),
        eq("FAKEC", "INE000C01013", series="SM"),  # SME series: skipped
        index("FAKE 50"),
        {"segment": "NSE_FO", "instrument_type": "FUT", "instrument_key": "NSE_FO|1"},
        {"segment": "NSE_COM", "instrument_type": "FUT"},
    ]
    result = ui.parse_file(write_gz(tmp_path, objects))
    assert [i.symbol for i in result.instruments] == ["FAKEA", "FAKEB", "FAKE 50"]
    assert result.skipped_by_segment == {"NSE_EQ:other_series": 1, "NSE_FO": 1, "NSE_COM": 1}
    assert result.malformed == 0
    assert result.count_by() == {"EQ": 1, "BE": 1, "INDEX": 1}


def test_records_are_tagged_upstox_and_mapped_to_the_table(tmp_path):
    result = ui.parse_file(write_gz(tmp_path, [eq("FAKEA", "INE000A01011"), index("FAKE 50")]))
    stock, idx = result.instruments
    assert stock.source == "upstox" and idx.source == "upstox"
    assert (stock.exchange, stock.segment, stock.series) == ("NSE", "EQ", "EQ")
    assert stock.isin == "INE000A01011" and stock.instrument_key == "NSE_EQ|INE000A01011"
    assert stock.tick_size == Decimal("0.05")  # 5 paise
    assert stock.lot_size == 1
    assert (idx.segment, idx.series, idx.isin, idx.tick_size) == ("INDEX", None, None, None)


def test_malformed_objects_are_skipped_and_counted(tmp_path):
    good = eq("FAKEA", "INE000A01011")
    objects = [
        good,
        "not an object",
        42,
        {"segment": ["NSE_EQ"]},  # odd segment type
        eq("FAKEB", "not-an-isin"),
        {**eq("FAKEC", "INE000C01013"), "trading_symbol": ""},
        {**eq("FAKED", "INE000D01014"), "lot_size": "lots"},
        {**eq("FAKEE", "INE000E01015"), "instrument_key": "NSE_FO|INE000E01015"},
        {k: v for k, v in eq("FAKEF", "INE000F01016").items() if k != "isin"},
        dict(good),  # duplicate key
        {**eq("FAKEA", "INE000G01017")},  # duplicate symbol
        {**index("FAKE 50"), "__class__": "os.system", "eval": "rm -rf"},  # extras ignored
    ]
    result = ui.parse_file(write_gz(tmp_path, objects))
    assert [i.symbol for i in result.instruments] == ["FAKEA", "FAKE 50"]
    assert result.malformed == 10


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"plain text", "not gzip"),
        (gzip.compress(b"{not json"), "not valid JSON"),
        (gzip.compress(b'{"an": "object"}'), "not a JSON array"),
        (b"\x1f\x8b" + b"broken gzip", "not valid gzip"),
    ],
)
def test_bad_files_are_refused(tmp_path, content, message):
    path = tmp_path / "bad.json.gz"
    path.write_bytes(content)
    with pytest.raises(ui.InstrumentFileError, match=message):
        ui.parse_file(path)


def test_decompression_is_capped(tmp_path, monkeypatch):
    path = write_gz(tmp_path, [eq(f"FAKE{i}", "INE000A01011") for i in range(200)])
    monkeypatch.setattr(ui, "CHUNK", 1024)
    with pytest.raises(ui.InstrumentFileError, match="decompresses to more than 4096"):
        ui.read_json_gz(path, max_bytes=4096)


def test_a_compressed_file_over_the_cap_is_refused_before_reading(tmp_path, monkeypatch):
    path = write_gz(tmp_path, [eq("FAKEA", "INE000A01011")])
    monkeypatch.setattr(ui, "MAX_COMPRESSED_BYTES", 10)
    with pytest.raises(ui.InstrumentFileError, match="larger than 10 bytes"):
        ui.parse_file(path)


# --- download ----------------------------------------------------------------------------


def test_download_saves_a_dated_file_and_sends_the_user_agent(tmp_path):
    server = Server(gz_bytes([eq("FAKEA", "INE000A01011")]))
    got = ui.download(URL, tmp_path, TODAY, client=server.client())
    assert got.path == ui.raw_dir(tmp_path) / "NSE-20261122.json.gz"
    assert got.path.read_bytes() == server.body
    assert got.etag == '"v1"' and not got.not_modified
    request = server.requests[0]
    assert request.headers["User-Agent"] == "market-intel"
    assert "If-None-Match" not in request.headers  # nothing remembered yet


def test_not_modified_after_the_validators_are_saved(tmp_path):
    server = Server(gz_bytes([eq("FAKEA", "INE000A01011")]))
    first = ui.download(URL, tmp_path, TODAY, client=server.client())
    ui.save_validators(tmp_path, URL, first)
    second = ui.download(URL, tmp_path, TODAY, client=server.client())
    assert second.not_modified and second.path is None
    sent = server.requests[1].headers
    assert sent["If-None-Match"] == '"v1"'
    assert sent["If-Modified-Since"] == "Sun, 22 Nov 2026 00:30:00 GMT"


def test_validators_for_another_url_are_ignored(tmp_path):
    server = Server(gz_bytes([]))
    ui.save_validators(tmp_path, "https://other.example.invalid/x.gz", ui.Download(None, '"v1"'))
    got = ui.download(URL, tmp_path, TODAY, client=server.client())
    assert not got.not_modified
    assert "If-None-Match" not in server.requests[0].headers


def test_unconditional_download_ignores_saved_validators(tmp_path):
    server = Server(gz_bytes([]))
    ui.save_validators(tmp_path, URL, ui.Download(None, '"v1"'))
    got = ui.download(URL, tmp_path, TODAY, conditional=False, save=False, client=server.client())
    assert got.path is not None and got.path.name == "dry-run.tmp"
    assert not list(ui.raw_dir(tmp_path).glob("NSE-*"))


def test_content_length_over_the_cap_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "MAX_COMPRESSED_BYTES", 100)
    server = Server(b"x" * 50, headers={"Content-Length": "5000"})  # claims to be big
    with pytest.raises(ui.InstrumentFileError, match="too large"):
        ui.download(URL, tmp_path, TODAY, client=server.client())
    assert not list(ui.raw_dir(tmp_path).iterdir())  # no partial file left


def test_streamed_size_over_the_cap_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "MAX_COMPRESSED_BYTES", 100)

    def handler(request):
        # no Content-Length: a streamed body that turns out too big
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * 160))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ui.InstrumentFileError, match="larger than 100 bytes"):
        ui.download(URL, tmp_path, TODAY, client=client)
    assert not list(ui.raw_dir(tmp_path).iterdir())


def test_http_errors_raise(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(httpx.HTTPStatusError):
        ui.download(URL, tmp_path, TODAY, client=client)


def test_only_the_newest_seven_files_are_kept(tmp_path):
    folder = ui.raw_dir(tmp_path)
    folder.mkdir(parents=True)
    for day in range(1, 10):
        (folder / f"NSE-202611{day:02d}.json.gz").write_bytes(b"old")
    ui.download(URL, tmp_path, TODAY, client=Server(gz_bytes([])).client())
    names = sorted(p.name for p in folder.glob("NSE-*.json.gz"))
    assert len(names) == 7
    assert names[-1] == "NSE-20261122.json.gz" and names[0] == "NSE-20261104.json.gz"
