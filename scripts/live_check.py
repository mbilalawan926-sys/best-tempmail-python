#!/usr/bin/env python3
"""Check 2.0.0 against the real API.

    BTM_API_KEY=btm_sk_live_... PYTHONPATH=src python3 scripts/live_check.py

Everything in tests/ runs against a mock, which means it agrees with whatever
the author assumed. That is not a hypothetical weakness: the GitHub Action for
this API passed 21 mocked tests and then failed on the first real email, and
the attachment field names in these SDKs were guessed wrong until a real
message was inspected.

WHAT THIS CANNOT COVER, stated rather than left to be discovered: nothing here
receives an email. Without sending mail to the inbox, OTP extraction, link
extraction and the already-arrived branch of wait_for_message are not exercised
against real mail. Those response shapes were captured from the live API by hand
and are asserted as verbatim fixtures in the mocked suite.

Cost: about a dozen requests and four inboxes of quota.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from best_tempmail import NotFoundError, TempMail, TempMailError  # noqa: E402

key = os.environ.get("BTM_API_KEY")
if not key:
    print("BTM_API_KEY is not set. Nothing to check against.", file=sys.stderr)
    sys.exit(2)

client = TempMail(api_key=key)
failures = 0


def step(name):
    """Run a check, report it, and keep going so one failure does not hide the
    rest."""

    def decorate(fn):
        global failures
        try:
            fn()
            print(f"  ok    {name}")
        except Exception as exc:  # noqa: BLE001 - a check failing is the point
            failures += 1
            print(f"  FAIL  {name}")
            print(f"        {type(exc).__name__}: {exc}")
        return fn

    return decorate


def parse_inbox_time(value: str) -> datetime:
    """Inbox timestamps arrive as "2026-11-17 16:03:38" with no timezone, and
    are UTC. This is how a consumer has to read them, so the check doubles as
    documentation."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


print("best-tempmail 2.0.0 live check\n")

remaining = {"n": 10_000}


@step("health")
def _health():
    health = client.health()
    assert health.get("status") == "ok", health
    print(f"        api version {health.get('version')}")


@step("get_usage, and the key is accepted")
def _usage():
    usage = client.get_usage()
    assert usage.paid, f"expected a paid plan, got {usage.plan}"
    assert usage.hourly.limit > 0, "the hourly quota did not parse"
    assert usage.key is not None, "a keyed call should carry a key block"
    assert usage.key.last_four, "last_four was empty"
    remaining["n"] = usage.hourly.remaining
    # Never print the key. Four characters is enough to tell keys apart, which
    # is exactly why the API returns only that.
    print(
        f"        plan {usage.plan}, key ...{usage.key.last_four}, "
        f"{usage.hourly.remaining} of {usage.hourly.limit} left this hour"
    )


if remaining["n"] < 100:
    print(f"\nOnly {remaining['n']} requests left this hour. Stopping rather than eating the quota.")
    sys.exit(0)


@step("get_domains")
def _domains():
    domains = client.get_domains()
    assert domains, "no domains came back, so no inbox could be created"
    print(f"        {len(domains)} domains, first is {domains[0]}")


@step("an inbox round trip, and the no-timezone timestamp is UTC")
def _inbox():
    inbox = client.create_inbox()
    try:
        assert inbox.address, "no address"

        # If this were read as local time the expiry would be hours out and,
        # on a two-hour inbox, in the past.
        expires = parse_inbox_time(inbox.expires_at)
        assert expires > datetime.now(timezone.utc), (
            f"expires_at {inbox.expires_at} is not in the future, so it was parsed in the wrong zone"
        )
        print(f"        {inbox.address}, expires {expires.isoformat()}")

        assert client.get_inbox(inbox.address).address == inbox.address

        page = client.list_messages(inbox.address)
        assert len(page) == 0, "a brand new inbox held messages"
        # These fields were missing from the published docs entirely, so
        # confirm production really sends them.
        assert page.limit > 0, "limit was absent from the listing response"
        assert page.scan_limit_reached is False, "scan_limit_reached true on an empty inbox"
        assert page.conclusively_empty is True
        print(
            f"        empty listing: limit {page.limit} offset {page.offset} "
            f"returned {page.returned} has_more {page.has_more} scanned {page.scanned}"
        )

        filtered = client.list_messages(inbox.address, subject="verify", limit=5)
        assert filtered.limit == 5, f"limit 5 was not honoured, got {filtered.limit}"
    finally:
        client.delete_inbox(inbox.address)


@step("a timeout longer than the server cap is honoured, not clamped")
def _wait():
    """The 1.x bug: timeout=70 waited 55 seconds and returned None."""
    inbox = client.create_inbox()
    try:
        started = time.monotonic()
        result = client.wait_for_message(inbox.address, timeout=70)
        elapsed = time.monotonic() - started

        assert result is None, "nobody emailed this inbox, so there should be nothing"
        assert elapsed >= 60, (
            f"gave up after {elapsed:.1f}s on a 70s timeout, so it is still being clamped"
        )
        print(f"        waited {elapsed:.1f}s across multiple long polls, then reported nothing")
    finally:
        client.delete_inbox(inbox.address)


@step("delete is safe to repeat")
def _delete_twice():
    inbox = client.create_inbox()
    assert client.delete_inbox(inbox.address) is True
    # This is what makes cleanup safe, so it has to hold against production and
    # not only against the mock.
    assert client.delete_inbox(inbox.address) is False, "a second delete should not raise"
    try:
        client.delete_inbox(inbox.address, missing_ok=False)
        raise AssertionError("missing_ok=False should still raise")
    except NotFoundError:
        pass


@step("create_inboxes charges one request per inbox")
def _bulk():
    batch = client.create_inboxes(2)
    try:
        assert batch.count == 2, f"asked for 2, got {batch.count}"
        assert batch.quota_charged == 2, f"quota_charged was {batch.quota_charged}"
        assert len(batch.addresses) == 2
        print(f"        {', '.join(batch.addresses)}")
    finally:
        for address in batch.addresses:
            try:
                client.delete_inbox(address)
            except TempMailError:
                pass


@step("a missing inbox is a NotFoundError")
def _missing():
    try:
        client.get_inbox("definitely-not-a-real-inbox-99999@alagen.site")
        raise AssertionError("expected NotFoundError")
    except NotFoundError:
        pass


print()
if failures:
    print(f"{failures} check{'' if failures == 1 else 's'} failed")
    sys.exit(1)
print("all checks passed against the live API")
