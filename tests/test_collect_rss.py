"""FR-01 / FR-13a collector tests. No live network — poll() is mocked directly with NormalizedItem dicts."""

from __future__ import annotations

import pytest
from feed_commons import PollError

from macro_monitor import collector_rss
from macro_monitor.collector_rss import collect_rss, fetch_and_log_rss

FED_ITEMS = [
    {
        "title": "FOMC holds rates; SPY reaction watched",
        "link": "https://example.test/press/1",
        "guid": "https://example.test/press/1",
        "pub_date": "2026-07-04T14:00:00+00:00",
        "description_excerpt": "test",
    },
    {
        "title": "Treasury yields drift, TLT in focus",
        "link": "https://example.test/press/2",
        "guid": "https://example.test/press/2",
        "pub_date": "2026-07-04T15:30:00+00:00",
        "description_excerpt": "test",
    },
]


def test_fetch_and_log_parses_and_tags(monkeypatch, log_conn):
    monkeypatch.setattr(
        collector_rss,
        "poll",
        lambda url, excerpt_max_length=300, timeout_seconds=15: FED_ITEMS,
    )
    result = fetch_and_log_rss(
        log_conn, "fed-press", "https://example.test/fed.xml", symbol_universe=["SPY", "TLT"]
    )
    assert result.ok is True
    assert result.inserted == 2
    rows = log_conn.execute(
        "SELECT observed_date, tagged_symbols FROM raw_observations ORDER BY id"
    ).fetchall()
    assert rows[0]["observed_date"] == "2026-07-04"
    assert "SPY" in rows[0]["tagged_symbols"]
    assert "TLT" in rows[1]["tagged_symbols"]


def test_rerun_is_append_only_noop(monkeypatch, log_conn):
    monkeypatch.setattr(
        collector_rss,
        "poll",
        lambda url, excerpt_max_length=300, timeout_seconds=15: FED_ITEMS,
    )
    r1 = fetch_and_log_rss(log_conn, "fed-press", "https://example.test/fed.xml")
    r2 = fetch_and_log_rss(log_conn, "fed-press", "https://example.test/fed.xml")
    assert r1.inserted == 2
    assert r2.inserted == 0  # dedup — no new rows on re-run (FR-14)
    count = log_conn.execute("SELECT COUNT(*) c FROM raw_observations").fetchone()["c"]
    assert count == 2


def test_http_error_is_logged_not_raised(monkeypatch, log_conn):
    def boom(url, excerpt_max_length=300, timeout_seconds=15):
        raise PollError("timeout")

    monkeypatch.setattr(collector_rss, "poll", boom)

    logged_calls = []

    def fake_log_event(level, event, msg=None, **fields):
        logged_calls.append((level, event, fields))

    monkeypatch.setattr(collector_rss, "log_event", fake_log_event)

    result = fetch_and_log_rss(
        log_conn, "fed-press", "https://example.test/fed.xml", run_id="test-run-timeout"
    )
    assert result.ok is False
    assert result.source == "fed-press"
    assert result.error == "poll: timeout"

    assert len(logged_calls) == 1
    level, event, fields = logged_calls[0]
    assert level == "error"
    assert event == "collect.fetch_failed"
    assert fields["run_id"] == "test-run-timeout"
    assert fields["source"] == "fed-press"
    assert fields["err_type"] == "timeout"
    assert fields["err_msg"] == "timeout"  # str(PollError("timeout")) with no __cause__ chained
    assert fields["outcome"] == "failed"


def test_broad_exception_from_poll_is_logged_not_raised(monkeypatch, log_conn):
    # Belt-and-suspenders safety net: an exception poll() raises that is NOT a PollError (an
    # unanticipated bug in feed-commons) must still be caught, logged, and returned as a failed
    # FeedResult -- never propagate and crash the whole unattended collection run.
    def boom(url, excerpt_max_length=300, timeout_seconds=15):
        raise ValueError("unexpected feed-commons bug")

    monkeypatch.setattr(collector_rss, "poll", boom)

    logged_calls = []

    def fake_log_event(level, event, msg=None, **fields):
        logged_calls.append((level, event, fields))

    monkeypatch.setattr(collector_rss, "log_event", fake_log_event)

    result = fetch_and_log_rss(
        log_conn, "fed-press", "https://example.test/fed.xml", run_id="test-run-broad"
    )
    assert result.ok is False
    assert result.source == "fed-press"
    assert result.error == "poll: network_error"

    assert len(logged_calls) == 1
    level, event, fields = logged_calls[0]
    assert level == "error"
    assert event == "collect.fetch_failed"
    assert fields["run_id"] == "test-run-broad"
    assert fields["source"] == "fed-press"
    assert fields["err_type"] == "network_error"
    assert fields["err_msg"] == "unexpected feed-commons bug"
    assert fields["outcome"] == "failed"


def test_poll_error_with_chained_cause_uses_cause_message(monkeypatch, log_conn):
    # plan.md: feed-commons chains the original exception (`raise PollError(code) from exc`) on
    # at least one path -- when present, err_msg must use str(exc.__cause__), the real diagnostic
    # detail, not the bounded PollError code string.
    underlying = ValueError("actual httpx connection reset detail")

    def boom(url, excerpt_max_length=300, timeout_seconds=15):
        try:
            raise underlying
        except ValueError as e:
            raise PollError("network_error") from e

    monkeypatch.setattr(collector_rss, "poll", boom)

    logged_calls = []

    def fake_log_event(level, event, msg=None, **fields):
        logged_calls.append((level, event, fields))

    monkeypatch.setattr(collector_rss, "log_event", fake_log_event)

    result = fetch_and_log_rss(log_conn, "fed-press", "https://example.test/fed.xml")
    assert result.ok is False

    fetch_failed = [c for c in logged_calls if c[1] == "collect.fetch_failed"]
    assert len(fetch_failed) == 1
    _, _, fields = fetch_failed[0]
    assert fields["err_msg"] == "actual httpx connection reset detail"


def test_malformed_xml_is_logged_not_raised(monkeypatch, log_conn):
    def boom(url, excerpt_max_length=300, timeout_seconds=15):
        raise PollError("parse_error")

    monkeypatch.setattr(collector_rss, "poll", boom)
    result = fetch_and_log_rss(log_conn, "fed-press", "https://example.test/fed.xml")
    assert result.ok is False


def test_collect_rss_summary_any_success(monkeypatch, log_conn):
    # fed-press succeeds; add a second source that fails — summary.any_success stays True (FR-13a).
    from macro_monitor import sources

    sources.add_source(log_conn, "wsj-markets", "rss", "https://wsj.test/f.xml", "2026-07-05")

    def selective(url, excerpt_max_length=300, timeout_seconds=15):
        if "wsj" in url:
            raise PollError("network_error")
        return FED_ITEMS

    monkeypatch.setattr(collector_rss, "poll", selective)
    summary = collect_rss(log_conn, symbol_universe=["SPY"])
    assert summary.any_success is True
    assert {r.source for r in summary.results} == {"fed-press", "wsj-markets"}
    assert any(not r.ok for r in summary.results)


def test_title_used_not_description_excerpt(monkeypatch, log_conn):
    # AC-3: title_or_snippet + tag_symbols() input must be the item's title, never its
    # description_excerpt, even when the two are clearly different strings.
    item = dict(FED_ITEMS[0])
    item["title"] = "SPY rally continues"
    item["description_excerpt"] = "Completely different excerpt text unrelated to title"
    monkeypatch.setattr(
        collector_rss,
        "poll",
        lambda url, excerpt_max_length=300, timeout_seconds=15: [item],
    )
    tag_symbols_calls = []
    real_tag_symbols = collector_rss.tag_symbols

    def spy_tag_symbols(text, universe):
        tag_symbols_calls.append(text)
        return real_tag_symbols(text, universe)

    monkeypatch.setattr(collector_rss, "tag_symbols", spy_tag_symbols)

    result = fetch_and_log_rss(
        log_conn, "fed-press", "https://example.test/fed.xml", symbol_universe=["SPY"]
    )
    assert result.inserted == 1
    row = log_conn.execute("SELECT title_or_snippet FROM raw_observations").fetchone()
    assert row["title_or_snippet"] == item["title"]
    assert row["title_or_snippet"] != item["description_excerpt"]
    assert item["description_excerpt"] not in row["title_or_snippet"]
    assert tag_symbols_calls == [item["title"]]


def test_observed_date_from_pub_date(monkeypatch, log_conn):
    # AC-4: a non-None ISO-8601 pub_date drives observed_date via its date portion.
    item = dict(FED_ITEMS[0])
    item["pub_date"] = "2026-07-04T12:00:00+00:00"
    monkeypatch.setattr(
        collector_rss,
        "poll",
        lambda url, excerpt_max_length=300, timeout_seconds=15: [item],
    )
    result = fetch_and_log_rss(log_conn, "fed-press", "https://example.test/fed.xml")
    assert result.inserted == 1
    row = log_conn.execute("SELECT observed_date FROM raw_observations").fetchone()
    assert row["observed_date"] == "2026-07-04"


def test_observed_date_falls_back_to_fallback_date(monkeypatch, log_conn):
    # AC-5: pub_date=None + an explicit fallback_date argument -> observed_date = fallback_date.
    item = dict(FED_ITEMS[0])
    item["pub_date"] = None
    monkeypatch.setattr(
        collector_rss,
        "poll",
        lambda url, excerpt_max_length=300, timeout_seconds=15: [item],
    )
    result = fetch_and_log_rss(
        log_conn, "fed-press", "https://example.test/fed.xml", fallback_date="2026-07-01"
    )
    assert result.inserted == 1
    row = log_conn.execute("SELECT observed_date FROM raw_observations").fetchone()
    assert row["observed_date"] == "2026-07-01"


def test_observed_date_falls_back_to_now_when_no_fallback(monkeypatch, log_conn):
    # AC-6: pub_date=None + no fallback_date -> observed_date = db.now_iso()[:10].
    item = dict(FED_ITEMS[0])
    item["pub_date"] = None
    monkeypatch.setattr(
        collector_rss,
        "poll",
        lambda url, excerpt_max_length=300, timeout_seconds=15: [item],
    )
    monkeypatch.setattr(collector_rss.db, "now_iso", lambda: "2026-08-15T00:00:00+00:00")
    result = fetch_and_log_rss(log_conn, "fed-press", "https://example.test/fed.xml")
    assert result.inserted == 1
    row = log_conn.execute("SELECT observed_date FROM raw_observations").fetchone()
    assert row["observed_date"] == "2026-08-15"


@pytest.mark.parametrize("code", ["invalid_url", "http_error", "parse_error", "network_error"])
def test_poll_error_codes_are_logged_not_raised(monkeypatch, log_conn, code):
    # AC-7: every PollError code (not just "timeout", already covered by
    # test_http_error_is_logged_not_raised) is caught uniformly -- fetch_and_log_rss returns
    # FeedResult(ok=False, ...) without raising, and the emitted log event carries the code but
    # never a raw feed URL body or entry content.
    def boom(url, excerpt_max_length=300, timeout_seconds=15):
        raise PollError(code)

    monkeypatch.setattr(collector_rss, "poll", boom)

    logged_calls = []

    def fake_log_event(level, event, msg=None, **fields):
        logged_calls.append((level, event, fields))

    monkeypatch.setattr(collector_rss, "log_event", fake_log_event)

    feed_url = "https://example.test/fed.xml"
    result = fetch_and_log_rss(log_conn, "fed-press", feed_url, run_id="test-run-code")

    assert result.ok is False
    assert isinstance(result, collector_rss.FeedResult)
    assert result.error == f"poll: {code}"

    fetch_failed = [c for c in logged_calls if c[1] == "collect.fetch_failed"]
    assert len(fetch_failed) == 1
    level, event, fields = fetch_failed[0]
    assert level == "error"
    assert event == "collect.fetch_failed"
    assert fields["run_id"] == "test-run-code"
    assert fields["source"] == "fed-press"
    assert fields["err_type"] == code
    assert fields["err_msg"] == code  # str(PollError(code)) with no __cause__ chained
    assert fields["outcome"] == "failed"
    for value in fields.values():
        assert feed_url not in str(value)


def test_entry_rejection_counts_and_no_raw_content_in_log(monkeypatch, log_conn):
    # AC-8: 4 items, insert_observation raises for exactly 2 (items 2 and 4) -> seen=4,
    # inserted=2, rejected=2 (catches a `rejected = 1` mutation, not just `+= 1`, since both would
    # produce the same result with only one rejection), ok=True, and the collect.entry_rejected
    # log line contains no raw title or url and carries the exact expected field values.
    items = [
        {
            "title": "First headline about SPY",
            "link": "https://example.test/press/first",
            "guid": "https://example.test/press/first",
            "pub_date": "2026-07-04T14:00:00+00:00",
            "description_excerpt": "test",
        },
        {
            "title": "Second headline that will be rejected",
            "link": "https://example.test/press/second-rejected",
            "guid": "https://example.test/press/second-rejected",
            "pub_date": "2026-07-04T14:05:00+00:00",
            "description_excerpt": "test",
        },
        {
            "title": "Third headline about TLT",
            "link": "https://example.test/press/third",
            "guid": "https://example.test/press/third",
            "pub_date": "2026-07-04T14:10:00+00:00",
            "description_excerpt": "test",
        },
        {
            "title": "Fourth headline that will also be rejected",
            "link": "https://example.test/press/fourth-rejected",
            "guid": "https://example.test/press/fourth-rejected",
            "pub_date": "2026-07-04T14:15:00+00:00",
            "description_excerpt": "test",
        },
    ]
    monkeypatch.setattr(
        collector_rss,
        "poll",
        lambda url, excerpt_max_length=300, timeout_seconds=15: items,
    )

    call_count = {"n": 0}
    real_insert_observation = collector_rss.insert_observation

    def flaky_insert_observation(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] in (2, 4):
            raise ValueError("simulated DB-level rejection")
        return real_insert_observation(*args, **kwargs)

    monkeypatch.setattr(collector_rss, "insert_observation", flaky_insert_observation)

    logged_calls = []

    def fake_log_event(level, event, msg=None, **fields):
        logged_calls.append((level, event, fields))

    monkeypatch.setattr(collector_rss, "log_event", fake_log_event)

    result = fetch_and_log_rss(
        log_conn, "fed-press", "https://example.test/fed.xml", run_id="test-run-reject"
    )

    assert result.ok is True
    assert result.seen == 4
    assert result.inserted == 2
    assert result.rejected == 2

    rejected_calls = [c for c in logged_calls if c[1] == "collect.entry_rejected"]
    assert len(rejected_calls) == 2
    raw_titles = [items[1]["title"], items[3]["title"]]
    raw_urls = [items[1]["link"], items[3]["link"]]
    for level, event, fields in rejected_calls:
        assert level == "warn"
        assert event == "collect.entry_rejected"
        assert fields["run_id"] == "test-run-reject"
        assert fields["source"] == "fed-press"
        assert fields["err_type"] == "ValueError"
        for value in fields.values():
            text = str(value)
            for raw in raw_titles + raw_urls:
                assert raw not in text


def test_poll_called_with_excerpt_and_timeout_arguments(monkeypatch, log_conn):
    # plan.md Risk areas: assert the exact excerpt_max_length=300, timeout_seconds=15 call shape
    # (AC-1) that fetch_and_log_rss must pass to poll(). No default values on this mock's
    # signature -- a call that omits either kwarg raises TypeError instead of silently matching
    # the expected value via the mock's own default, which is what let a dropped kwarg slip past
    # an earlier, laxer version of this test.
    captured_calls = []

    def recording_poll(url, *, excerpt_max_length, timeout_seconds):
        captured_calls.append(
            {
                "url": url,
                "excerpt_max_length": excerpt_max_length,
                "timeout_seconds": timeout_seconds,
            }
        )
        return FED_ITEMS

    monkeypatch.setattr(collector_rss, "poll", recording_poll)
    result = fetch_and_log_rss(log_conn, "fed-press", "https://example.test/fed.xml")

    assert result.ok is True
    assert len(captured_calls) == 1
    assert captured_calls[0]["excerpt_max_length"] == 300
    assert captured_calls[0]["timeout_seconds"] == 15


def test_collect_rss_forwards_source_name_url_and_kwargs(monkeypatch, log_conn):
    # collect_rss() must call fetch_and_log_rss with the row's real "name"/"url_or_query" values
    # and forward symbol_universe/run_id through unchanged -- none of this was previously asserted,
    # so a wrong dict key or a dropped kwarg (e.g. symbol_universe silently becoming None) would
    # have gone unnoticed.
    captured_calls = []
    real_fetch_and_log_rss = collector_rss.fetch_and_log_rss

    def recording_fetch_and_log_rss(conn, source, url, **kwargs):
        captured_calls.append({"source": source, "url": url, **kwargs})
        return real_fetch_and_log_rss(conn, source, url, **kwargs)

    monkeypatch.setattr(collector_rss, "fetch_and_log_rss", recording_fetch_and_log_rss)
    monkeypatch.setattr(
        collector_rss,
        "poll",
        lambda url, excerpt_max_length=300, timeout_seconds=15: FED_ITEMS,
    )

    collect_rss(log_conn, symbol_universe=["SPY", "TLT"], run_id="test-run-collect")

    fed_call = next(c for c in captured_calls if c["source"] == "fed-press")
    assert fed_call["url"] == "https://example.test/fed.xml"
    assert fed_call["symbol_universe"] == ["SPY", "TLT"]
    assert fed_call["run_id"] == "test-run-collect"
