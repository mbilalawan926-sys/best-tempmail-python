"""Tests for everything 2.0.0 adds or changes.

Run with::

    PYTHONPATH=src python3 -m pytest tests/test_v2.py -q

These use their own mock server rather than tests/mock_server.py, because
several of them need to control how many times an endpoint has been called, and
a couple deliberately REFUSE to answer the long poll at all.

That refusal is the point. A mock that answers everything proves very little:
the GitHub Action for this API passed 21 tests against a mock that always
answered /wait, then failed on the first real email because /wait cannot see
mail that had already been delivered.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from best_tempmail import NotFoundError, TempMail, TempMailError

ADDRESS = "test1234@dextde.site"


# ---------------------------------------------------------------------------
# A mock the test itself writes the behaviour of.
# ---------------------------------------------------------------------------


class Recorder:
    """What the server was asked, so a test can assert on the requests and not
    only on the responses."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def record(self, method: str, raw_path: str, body: Optional[str]) -> None:
        parsed = urlparse(raw_path)
        self.calls.append(
            {
                "method": method,
                "raw": raw_path,
                "path": unquote(parsed.path),
                "query": parse_qs(parsed.query),
                "body": json.loads(body) if body else None,
            }
        )

    def count(self, needle: str) -> int:
        return sum(1 for c in self.calls if needle in c["raw"])

    def called(self, needle: str) -> bool:
        return self.count(needle) > 0

    @property
    def paths(self) -> List[str]:
        return [f"{c['method']} {c['raw']}" for c in self.calls]


def make_server(handler: Callable[[Dict[str, Any], Recorder], Any]):
    """Start a mock on a random port.

    ``handler`` receives the recorded request and the recorder, and returns
    either a dict (sent as 200) or a ``(status, dict)`` tuple.
    """
    recorder = Recorder()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _handle(self, method: str) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length).decode() if length else None
            recorder.record(method, self.path, body)

            try:
                result = handler(recorder.calls[-1], recorder)
            except AssertionError as exc:
                # A handler assertion means the client called something it
                # should not have. Surface it as a 500 carrying the reason, so
                # the test fails with that message rather than a bare status.
                result = (500, {"error": f"mock refused: {exc}"})

            status, payload = result if isinstance(result, tuple) else (200, result)
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

    server = HTTPServer(("127.0.0.1", 0), H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{server.server_port}/v1"
    return server, base_url, recorder


@pytest.fixture
def api():
    """Yields a factory: give it a handler, get back a client and a recorder."""
    servers = []

    def build(handler, **client_kwargs):
        server, base_url, recorder = make_server(handler)
        servers.append(server)
        client = TempMail(api_key="KEY", base_url=base_url, max_retries=0, **client_kwargs)
        return client, recorder

    yield build

    for server in servers:
        server.shutdown()
        server.server_close()


def summary(message_id: str = "msg1", **extra: Any) -> Dict[str, Any]:
    return {
        "id": message_id,
        "from": '"Example" <no-reply@example.com>',
        "subject": "Please confirm your email address",
        "date": "2026-11-18T12:27:32.000Z",
        "has_attachments": False,
        **extra,
    }


# ---------------------------------------------------------------------------
# THE REGRESSION: mail that had already arrived
# ---------------------------------------------------------------------------


def test_wait_sees_mail_that_had_already_arrived(api):
    def handler(call, rec):
        assert "/wait" not in call["raw"], (
            "the client long-polled instead of looking at what was already in the inbox"
        )
        return {"success": True, "messages": [summary("already-here")]}

    client, rec = api(handler)

    message = client.wait_for_message(ADDRESS, timeout=5)

    assert message is not None and message.id == "already-here"
    assert not rec.called("/wait"), f"the long poll was called: {rec.paths}"


def test_wait_for_code_sees_mail_that_had_already_arrived(api):
    def handler(call, rec):
        assert "/wait" not in call["raw"], (
            "the client long-polled instead of looking at what was already in the inbox"
        )
        if call["path"].endswith("/otp"):
            return {"success": True, "message_id": "m1", "code": "004007", "confidence": "high"}
        return {"success": True, "messages": [summary("m1")]}

    client, rec = api(handler)

    # A leading zero is why this is a string and not an int.
    assert client.wait_for_code(ADDRESS, timeout=5) == "004007"
    assert not rec.called("/wait")


def test_include_existing_false_restores_the_pre_2_0_behaviour(api):
    """The escape hatch for watching an inbox for successive messages."""

    def handler(call, rec):
        if "/wait" in call["raw"]:
            return {"success": True, "message": None, "timed_out": True}
        # There IS mail here, and include_existing=False must ignore it.
        return {"success": True, "messages": [summary("old-mail")]}

    client, rec = api(handler)

    assert client.wait_for_message(ADDRESS, timeout=1, include_existing=False) is None
    assert rec.called("/wait"), "it should have long-polled"
    assert not rec.called("/messages"), "it should not have looked at existing mail"


# ---------------------------------------------------------------------------
# The timeout is no longer silently clamped
# ---------------------------------------------------------------------------


def test_a_timeout_longer_than_the_server_cap_keeps_polling(api):
    """1.0.0 clamped timeout to one 55s request and then returned None.

    So wait_for_message(timeout=120) actually waited 55 seconds and reported no
    mail, with nothing telling the caller it had given up early.
    """

    def handler(call, rec):
        if "/wait" not in call["raw"]:
            return {"success": True, "messages": []}

        asked = int(call["query"]["timeout"][0])
        assert asked <= 55, f"asked the server for {asked}s, which it would clamp"

        # First long poll reports a server-side timeout, which is a 200 and not
        # a failure. The client must go round again.
        if rec.count("/wait") == 1:
            return {"success": True, "message": None, "timed_out": True}
        return {"success": True, "message": summary("arrived-late")}

    client, rec = api(handler)

    message = client.wait_for_message(ADDRESS, timeout=120)

    assert message is not None and message.id == "arrived-late"
    assert rec.count("/wait") == 2, f"expected two long polls, got {rec.count('/wait')}"


def test_wait_returns_none_when_nothing_arrives(api):
    def handler(call, rec):
        if "/wait" in call["raw"]:
            return {"success": True, "message": None, "timed_out": True}
        return {"success": True, "messages": []}

    client, _ = api(handler)

    # Still None rather than raising: nothing has gone wrong, the mail simply
    # did not arrive. That contract is unchanged from 1.0.0.
    assert client.wait_for_message(ADDRESS, timeout=1) is None


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_a_filter_never_uses_the_long_poll(api):
    """/wait cannot filter, so it would return the first message to arrive.

    In a real signup a welcome email often beats the verification email by a
    second, and an unfiltered wait then hands back the wrong one.
    """

    def handler(call, rec):
        assert "/wait" not in call["raw"], "the long poll cannot filter"
        assert call["query"].get("subject") == ["verify"], "the filter was dropped"

        if rec.count("/messages") < 2:
            return {"success": True, "messages": []}
        return {"success": True, "messages": [summary("the-right-one")]}

    client, rec = api(handler)

    message = client.wait_for_message(ADDRESS, timeout=8, subject="verify")

    assert message is not None and message.id == "the-right-one"
    assert not rec.called("/wait")


def test_every_filter_reaches_the_query_string(api):
    def handler(call, rec):
        return {"success": True, "messages": []}

    client, rec = api(handler)

    client.list_messages(
        ADDRESS,
        limit=20,
        offset=40,
        from_="service.com",
        subject="verify",
        q="ORD-48812",
        since="2026-07-04T00:00:00Z",
        until="2026-07-05T00:00:00Z",
        has_attachments=True,
    )

    query = rec.calls[0]["query"]
    assert query["limit"] == ["20"]
    assert query["offset"] == ["40"]
    assert query["from"] == ["service.com"]
    assert query["subject"] == ["verify"]
    assert query["q"] == ["ORD-48812"]
    assert query["since"] == ["2026-07-04T00:00:00Z"]
    assert query["until"] == ["2026-07-05T00:00:00Z"]
    assert query["has_attachments"] == ["true"]


def test_has_attachments_false_is_a_filter_not_an_absence(api):
    client, rec = api(lambda call, rec: {"success": True, "messages": []})

    client.list_messages(ADDRESS, has_attachments=False)
    assert rec.calls[0]["query"]["has_attachments"] == ["false"]

    client.list_messages(ADDRESS)
    assert "has_attachments" not in rec.calls[1]["query"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 201},
        {"limit": 0},
        {"offset": -1},
        {"offset": 5001},
    ],
)
def test_out_of_range_paging_is_refused_before_any_request(api, kwargs):
    def handler(call, rec):
        raise AssertionError("the API must not be called with out-of-range paging")

    client, rec = api(handler)

    with pytest.raises(ValueError):
        client.list_messages(ADDRESS, **kwargs)

    assert rec.calls == []


# ---------------------------------------------------------------------------
# What a list cannot carry
# ---------------------------------------------------------------------------


def test_list_messages_exposes_the_paging_and_scan_fields(api):
    def handler(call, rec):
        return {
            "success": True,
            "address": ADDRESS,
            "messages": [summary("m1")],
            "limit": 100,
            "offset": 0,
            "returned": 1,
            "has_more": True,
            "scanned": 250,
            "scan_limit_reached": True,
        }

    client, _ = api(handler)
    page = client.list_messages(ADDRESS)

    assert page.has_more is True
    assert page.scanned == 250
    assert page.scan_limit_reached is True
    assert len(page) == 1
    assert page.first is not None and page.first.id == "m1"
    # Not conclusive: the search was cut short, so the message may exist
    # further back.
    assert page.conclusively_empty is False


def test_an_empty_page_distinguishes_no_match_from_exhausted_search(api):
    client, _ = api(
        lambda call, rec: {"success": True, "messages": [], "scan_limit_reached": False}
    )
    assert client.list_messages(ADDRESS).conclusively_empty is True

    client, _ = api(
        lambda call, rec: {"success": True, "messages": [], "scan_limit_reached": True}
    )
    assert client.list_messages(ADDRESS).conclusively_empty is False


def test_get_messages_still_returns_a_plain_list(api):
    """The 1.0.0 signature keeps working, filters or not."""
    client, rec = api(lambda call, rec: {"success": True, "messages": [summary("m1")]})

    result = client.get_messages(ADDRESS, limit=5, subject="verify")

    assert isinstance(result, list)
    assert result[0].id == "m1"
    assert rec.calls[0]["query"]["subject"] == ["verify"]


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def test_links_are_typed_and_trackers_are_unwrapped(api):
    def handler(call, rec):
        return {
            "success": True,
            "message_id": "m1",
            "action_url": "https://example.com/verify?token=abc",
            "confidence": "high",
            "links": [
                {
                    "url": "https://click.tracker/xyz",
                    "unwrapped_url": "https://example.com/verify?token=abc",
                    "text": "Confirm",
                    "type": "action",
                    "score": 9,
                },
                {
                    "url": "https://example.com/unsubscribe",
                    "unwrapped_url": None,
                    "text": "Unsubscribe",
                    "type": "unsubscribe",
                    "score": 0,
                },
            ],
        }

    client, _ = api(handler)
    links = client.get_links(ADDRESS, "m1")

    assert links.action_url == "https://example.com/verify?token=abc"
    # destination prefers the unwrapped URL, and falls back to the URL.
    assert links.links[0].destination == "https://example.com/verify?token=abc"
    assert links.links[1].destination == "https://example.com/unsubscribe"
    assert len(links.action_links) == 1
    assert len(links.of_type("unsubscribe")) == 1


def test_wait_for_action_url_raises_rather_than_guessing(api):
    def handler(call, rec):
        if call["path"].endswith("/links"):
            return {"success": True, "message_id": "m1", "action_url": None, "confidence": "none"}
        return {"success": True, "messages": [summary("m1")]}

    client, _ = api(handler)

    with pytest.raises(TempMailError, match="no clear call-to-action"):
        client.wait_for_action_url(ADDRESS, timeout=5)


def test_wait_for_code_raises_but_wait_for_otp_does_not(api):
    """The same situation, two deliberately different answers.

    A caller asking for a bare string has nowhere to put "the mail came but
    held no code". A caller asking for the result object does.
    """

    def handler(call, rec):
        if call["path"].endswith("/otp"):
            return {"success": True, "message_id": "m1", "code": None, "confidence": "none"}
        return {"success": True, "messages": [summary("m1")]}

    client, _ = api(handler)

    with pytest.raises(TempMailError, match="no verification code"):
        client.wait_for_code(ADDRESS, timeout=5)

    result = client.wait_for_otp(ADDRESS, timeout=5)
    assert result is not None
    assert result.code is None
    assert result.confidence == "none"


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


def test_usage_keyed(api):
    def handler(call, rec):
        return {
            "success": True,
            "plan": "founders",
            "paid": True,
            "hourly": {
                "limit": 2000,
                "used": 15,
                "remaining": 1985,
                "resets_at": "2026-11-17T14:19:05.000Z",
                "resets_in_seconds": 927,
            },
            "lifetime_requests": 9766,
            "key": {"id": 3, "last_four": "4cf2", "status": "active"},
            "subscription": {"status": "active", "days_remaining": 273, "in_grace_period": False},
        }

    client, _ = api(handler)
    usage = client.get_usage()

    assert usage.paid is True
    assert usage.hourly.remaining == 1985
    assert usage.hourly.nearly_exhausted() is False
    assert usage.key is not None and usage.key.last_four == "4cf2"
    assert usage.subscription is not None and usage.subscription.days_remaining == 273
    # A paid plan has no daily cap, which is None rather than zero.
    assert usage.inboxes_created_today is None


def test_usage_keyless_distinguishes_none_from_zero(api):
    def handler(call, rec):
        return {
            "success": True,
            "plan": "free",
            "paid": False,
            "hourly": {"limit": 150, "used": 1, "remaining": 149, "resets_in_seconds": 3600},
            "inboxes_created_today": 0,
            "upgrade": "https://best-tempmail.com/api/pricing",
        }

    client, _ = api(handler)
    usage = client.get_usage()

    assert usage.paid is False
    assert usage.key is None
    assert usage.subscription is None
    # Zero means "the cap applies and none have been used", which is not the
    # same as "no cap applies".
    assert usage.inboxes_created_today == 0
    assert usage.upgrade_url is not None


# ---------------------------------------------------------------------------
# Bulk creation
# ---------------------------------------------------------------------------


def test_bulk_create_reports_the_quota_it_charged(api):
    def handler(call, rec):
        assert call["body"] == {"count": 2}
        return {
            "success": True,
            "count": 2,
            "quota_charged": 2,
            "lifetime_minutes": 120,
            "inboxes": [
                {"address": "a@simtim.site", "created_at": "2026-11-17 14:03:38", "expires_at": "2026-11-17 16:03:38"},
                {"address": "b@linsal.site", "created_at": "2026-11-17 14:03:38", "expires_at": "2026-11-17 16:03:38"},
            ],
        }

    client, _ = api(handler)
    batch = client.create_inboxes(2)

    assert batch.quota_charged == 2
    assert batch.addresses == ["a@simtim.site", "b@linsal.site"]
    assert len(batch) == 2


@pytest.mark.parametrize("count", [0, -1, 51, True, 2.5, "3"])
def test_bulk_create_refuses_an_impossible_count_locally(api, count):
    def handler(call, rec):
        raise AssertionError("the API must not be called for a count that cannot be valid")

    client, rec = api(handler)

    with pytest.raises(ValueError):
        client.create_inboxes(count)

    assert rec.calls == []


# ---------------------------------------------------------------------------
# Inbox lifetime, and the note that warns it was clamped
# ---------------------------------------------------------------------------


def test_lifetime_minutes_is_sent_and_the_clamp_note_is_surfaced(api):
    def handler(call, rec):
        assert call["body"]["lifetime_minutes"] == 43200
        return {
            "success": True,
            "address": ADDRESS,
            "created_at": "2026-11-17 14:03:38",
            "expires_at": "2026-11-17 16:03:38",
            "lifetime_minutes": 120,
            "note": "lifetime_minutes clamped to the plan maximum of 120",
        }

    client, _ = api(handler)
    inbox = client.create_inbox(lifetime_minutes=43200)

    # Asking for more than the plan allows does not fail: the inbox is created
    # at the maximum, and note is the only warning that it expires sooner than
    # asked. Unreachable in 1.0.0, which had no lifetime_minutes at all.
    assert inbox.lifetime_minutes == 120
    assert inbox.note is not None and "clamped" in inbox.note


def test_create_inbox_omits_what_was_not_asked_for(api):
    client, rec = api(lambda call, rec: {"success": True, "address": ADDRESS})

    client.create_inbox(username="chosen")

    assert rec.calls[0]["body"] == {"username": "chosen"}


# ---------------------------------------------------------------------------
# Cleanup, and addresses in URLs
# ---------------------------------------------------------------------------


def test_delete_inbox_tolerates_one_that_is_already_gone(api):
    """Cleanup usually runs after the thing it cleans up may have expired."""
    client, _ = api(lambda call, rec: (404, {"error": "Inbox not found"}))

    assert client.delete_inbox(ADDRESS) is False

    # The old behaviour is still available for anyone who wants the error.
    client, _ = api(lambda call, rec: (404, {"error": "Inbox not found"}))
    with pytest.raises(NotFoundError):
        client.delete_inbox(ADDRESS, missing_ok=False)


def test_a_plus_addressed_inbox_survives_the_url(api):
    """Mail to user+tag@domain arrives at user@domain. The plus has to reach
    the server as a plus and not as an encoded space."""
    client, rec = api(lambda call, rec: {"success": True, "messages": []})

    client.list_messages("user+netflix@dextde.site")

    raw = rec.calls[0]["raw"]
    assert "%2B" in raw, f"the plus was not encoded: {raw}"
    assert "@" in raw, f"the @ should stay literal: {raw}"
    assert rec.calls[0]["path"] == "/v1/inboxes/user+netflix@dextde.site/messages"
