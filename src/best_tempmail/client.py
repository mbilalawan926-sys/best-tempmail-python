"""Client for the Best Temp Mail API."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote, urlencode

import requests

from .models import (
    Attachment,
    AttachmentFile,
    AuthenticationError,
    BulkResult,
    HourlyQuota,
    Inbox,
    KeyInfo,
    Link,
    LinksResult,
    Message,
    MessageList,
    MessageSummary,
    NotFoundError,
    OtpResult,
    PaymentRequiredError,
    RateLimit,
    RateLimitError,
    SubscriptionInfo,
    TempMailError,
    TimeoutError,
    Usage,
    WebhookRegistration,
)

DEFAULT_BASE_URL = "https://api.best-tempmail.com/v1"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2

# The server clamps waits to 55 seconds; sending more just wastes a round trip.
MAX_WAIT_SECONDS = 55

# Default total wait across however many requests that takes.
DEFAULT_WAIT_SECONDS = 60

# Gap between polls when a filter rules out the long-poll endpoint.
FILTERED_POLL_INTERVAL = 2.0

# Server-enforced ceilings on the message listing.
MAX_LIST_LIMIT = 200
MAX_LIST_OFFSET = 5000

# Most inboxes one bulk call may ask for.
MAX_BULK_COUNT = 50


def _esc(address: str) -> str:
    """Encode an address for a URL path.

    The ``@`` is left literal, which is the form the published examples use and
    is known to work, while everything else is percent-encoded so that a plus
    in a plus-addressed inbox cannot be read as an encoded space.
    """
    return quote(address, safe="@")


class TempMail:
    """Client for the Best Temp Mail API.

    ::

        from best_tempmail import TempMail

        client = TempMail(api_key="btm_sk_live_...")

        inbox = client.create_inbox()
        message = client.wait_for_message(inbox.address)
        otp = client.get_otp(inbox.address, message.id)
        print(otp.code)

    Constructing without an ``api_key`` uses the free tier, which needs no key
    but is rate limited per IP and cannot use webhooks, OTP extraction or
    attachment downloads.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._extra_headers = headers or {}

        # A session reuses the underlying TCP connection, which matters when a
        # test suite makes many calls in quick succession.
        self._session = requests.Session()

        #: Rate limit from the most recent response, or None before any call.
        self.rate_limit: Optional[RateLimit] = None

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"Accept": "application/json", **self._extra_headers}
        if self.api_key:
            h["x-api-key"] = self.api_key
        if extra:
            h.update(extra)
        return h

    def _capture_rate_limit(self, response: requests.Response) -> None:
        limit = response.headers.get("x-ratelimit-limit")
        remaining = response.headers.get("x-ratelimit-remaining")
        if limit and remaining:
            self.rate_limit = RateLimit(
                limit=int(limit),
                remaining=int(remaining),
                reset=int(response.headers.get("x-ratelimit-reset", 0)),
            )

    def _to_error(self, response: requests.Response) -> TempMailError:
        """Turn a non-2xx response into the most specific error available."""
        body: Dict[str, Any] = {}
        message = f"Request failed with status {response.status_code}"
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("error"):
                message = body["error"]
        except ValueError:
            pass  # non-JSON error body; keep the generic message

        request_id = body.get("request_id") if isinstance(body, dict) else None
        status = response.status_code

        if status == 401:
            return AuthenticationError(message, status, request_id, body)
        if status == 402:
            return PaymentRequiredError(
                message, body.get("plan"), body.get("upgrade"), request_id, body
            )
        if status == 404:
            return NotFoundError(message, status, request_id, body)
        if status == 429:
            retry_after = response.headers.get("retry-after")
            return RateLimitError(
                message, int(retry_after) if retry_after else None, request_id, body
            )
        return TempMailError(message, status, request_id, body)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        raw: bool = False,
        timeout: Optional[float] = None,
    ) -> Any:
        """One HTTP call, retrying only failures that retrying can fix.

        401, 402 and 404 are never retried: the request was understood and
        refused, so repeating it only wastes quota and delays the error
        reaching the caller. Network errors, timeouts, 429 and 5xx are retried
        with exponential backoff.
        """
        url = f"{self.base_url}{path}"
        request_timeout = timeout if timeout is not None else self.timeout
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                # 0.5s, 1s, 2s ... capped so a retry storm cannot stall a suite.
                time.sleep(min(0.5 * (2 ** (attempt - 1)), 4.0))

            try:
                response = self._session.request(
                    method,
                    url,
                    headers=self._headers(
                        {"Content-Type": "application/json"} if json_body is not None else None
                    ),
                    json=json_body,
                    timeout=request_timeout,
                )
                self._capture_rate_limit(response)

                if not response.ok:
                    error = self._to_error(response)
                    retryable = response.status_code == 429 or response.status_code >= 500
                    if not retryable or attempt == self.max_retries:
                        raise error
                    last_error = error
                    continue

                return response if raw else response.json()

            except requests.exceptions.Timeout:
                last_error = TimeoutError(
                    f"Request to {path} timed out after {request_timeout}s"
                )
                if attempt == self.max_retries:
                    raise last_error
            except requests.exceptions.RequestException as exc:
                last_error = TempMailError(str(exc))
                if attempt == self.max_retries:
                    raise last_error

        raise last_error if last_error else TempMailError("Request failed")

    # ------------------------------------------------------------------
    # service
    # ------------------------------------------------------------------

    def get_domains(self) -> List[str]:
        """Domains inboxes can be created on.

        Worth calling rather than hardcoding: the list changes as domains are
        added or rotated, and callers who read it keep working when it does.
        """
        return self._request("GET", "/domains")["domains"]

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/health")

    # ------------------------------------------------------------------
    # inboxes
    # ------------------------------------------------------------------

    @staticmethod
    def _to_inbox(r: Dict[str, Any]) -> Inbox:
        return Inbox(
            address=r["address"],
            created_at=r.get("created_at", ""),
            expires_at=r.get("expires_at", ""),
            lifetime_minutes=r.get("lifetime_minutes"),
            note=r.get("note"),
        )

    def create_inbox(
        self,
        username: Optional[str] = None,
        domain: Optional[str] = None,
        lifetime_minutes: Optional[int] = None,
    ) -> Inbox:
        """Create a disposable inbox. Every argument may be omitted.

        ``lifetime_minutes`` is capped by the plan: 2 hours on Free, 7 days on
        Founders and Developer, 30 days on Pro. Asking for longer does not
        fail. The inbox is created at the maximum and ``Inbox.note`` explains
        that it was clamped, which is the only warning you get that it expires
        sooner than you asked.
        """
        body: Dict[str, Any] = {}
        if username:
            body["username"] = username
        if domain:
            body["domain"] = domain
        if lifetime_minutes is not None:
            body["lifetime_minutes"] = lifetime_minutes
        r = self._request("POST", "/inboxes", json_body=body)
        return self._to_inbox(r)

    def create_inboxes(
        self,
        count: int,
        domain: Optional[str] = None,
        lifetime_minutes: Optional[int] = None,
    ) -> BulkResult:
        """Create up to 50 inboxes in one call. Requires a paid plan.

        For a suite that runs in parallel and wants an address per worker. Each
        inbox costs one request of quota, reported back as
        ``BulkResult.quota_charged``.

        Leaving ``domain`` unset spreads the batch across the available
        domains, which is usually preferable: fifty signups from one domain
        inside a minute looks like a single bulk sender to whatever is being
        signed up to. A username cannot be set here, since a batch cannot share
        one prefix.
        """
        if not isinstance(count, int) or isinstance(count, bool):
            raise ValueError(f"count must be an int, got {type(count).__name__}")
        if count < 1 or count > MAX_BULK_COUNT:
            raise ValueError(f"count must be between 1 and {MAX_BULK_COUNT}, got {count}")

        body: Dict[str, Any] = {"count": count}
        if domain:
            body["domain"] = domain
        if lifetime_minutes is not None:
            body["lifetime_minutes"] = lifetime_minutes

        r = self._request("POST", "/inboxes/bulk", json_body=body)
        inboxes = [self._to_inbox(i) for i in r.get("inboxes", [])]
        return BulkResult(
            count=r.get("count", len(inboxes)),
            quota_charged=r.get("quota_charged", len(inboxes)),
            inboxes=inboxes,
            lifetime_minutes=r.get("lifetime_minutes"),
        )

    def get_inbox(self, address: str) -> Inbox:
        r = self._request("GET", f"/inboxes/{_esc(address)}")
        return self._to_inbox(r)

    def delete_inbox(self, address: str, missing_ok: bool = True) -> bool:
        """Delete an inbox and its messages.

        With ``missing_ok`` (the default), an inbox that has already expired or
        been deleted returns False instead of raising. Cleanup usually runs
        after the thing it is cleaning up may already be gone, and anything
        that turns a passing test red for tidying up after itself is a bad
        trade. Pass ``missing_ok=False`` to get the old behaviour of raising
        ``NotFoundError``.
        """
        try:
            return bool(self._request("DELETE", f"/inboxes/{_esc(address)}").get("success"))
        except NotFoundError:
            if missing_ok:
                return False
            raise

    # ------------------------------------------------------------------
    # messages
    # ------------------------------------------------------------------

    @staticmethod
    def _to_summary(m: Dict[str, Any]) -> MessageSummary:
        return MessageSummary(
            id=m["id"],
            from_=m.get("from", ""),
            subject=m.get("subject", ""),
            date=m.get("date"),
            has_attachments=m.get("has_attachments", False),
        )

    @staticmethod
    def _list_query(
        limit: Optional[int],
        offset: Optional[int],
        from_: Optional[str],
        subject: Optional[str],
        q: Optional[str],
        since: Optional[str],
        until: Optional[str],
        has_attachments: Optional[bool],
    ) -> str:
        """Build the query string, refusing what the server would reject.

        Validating locally means a typo fails immediately with a clear message
        instead of costing a request and coming back as a 400.
        """
        params: Dict[str, str] = {}
        if limit is not None:
            if not isinstance(limit, int) or limit < 1 or limit > MAX_LIST_LIMIT:
                raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}, got {limit}")
            params["limit"] = str(limit)
        if offset is not None:
            if not isinstance(offset, int) or offset < 0 or offset > MAX_LIST_OFFSET:
                raise ValueError(f"offset must be between 0 and {MAX_LIST_OFFSET}, got {offset}")
            params["offset"] = str(offset)
        if from_:
            params["from"] = from_
        if subject:
            params["subject"] = subject
        if q:
            params["q"] = q
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        if has_attachments is not None:
            # An explicit False is a filter, not an absence.
            params["has_attachments"] = "true" if has_attachments else "false"

        return f"?{urlencode(params)}" if params else ""

    def list_messages(
        self,
        address: str,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        from_: Optional[str] = None,
        subject: Optional[str] = None,
        q: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        has_attachments: Optional[bool] = None,
    ) -> MessageList:
        """A page of messages, with the paging and search metadata.

        Every filter is optional and they combine, so you can ask for the
        second page of messages from one sender that arrived today and mention
        a particular word in the body. The filtering happens on the server: a
        busy inbox never has to be downloaded and sifted locally.

            page = client.list_messages(
                address, q="ORD-48812", since="2026-07-04T00:00:00Z", limit=20
            )
            if not page.messages and page.conclusively_empty:
                ...  # nothing matches, definitively

        ``q`` searches the sender, the subject and the decoded body, ignoring
        text inside script and style blocks so a tracking script cannot produce
        a match.
        """
        query = self._list_query(
            limit, offset, from_, subject, q, since, until, has_attachments
        )
        r = self._request("GET", f"/inboxes/{_esc(address)}/messages{query}")
        messages = [self._to_summary(m) for m in r.get("messages", [])]

        return MessageList(
            address=r.get("address", address),
            messages=messages,
            limit=r.get("limit", 0),
            offset=r.get("offset", 0),
            returned=r.get("returned", len(messages)),
            has_more=r.get("has_more", False),
            scanned=r.get("scanned", 0),
            scan_limit_reached=r.get("scan_limit_reached", False),
        )

    def get_messages(
        self,
        address: str,
        limit: int = 100,
        *,
        offset: Optional[int] = None,
        from_: Optional[str] = None,
        subject: Optional[str] = None,
        q: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        has_attachments: Optional[bool] = None,
    ) -> List[MessageSummary]:
        """The messages in an inbox as a plain list.

        Kept as it was, and now accepting the same filters as
        ``list_messages``. Use ``list_messages`` when you need ``has_more`` or
        ``scan_limit_reached``, which a list cannot carry.
        """
        return self.list_messages(
            address,
            limit=limit,
            offset=offset,
            from_=from_,
            subject=subject,
            q=q,
            since=since,
            until=until,
            has_attachments=has_attachments,
        ).messages

    def get_message(self, address: str, message_id: str) -> Message:
        r = self._request("GET", f"/inboxes/{_esc(address)}/messages/{quote(message_id)}")
        m = r["message"]
        return Message(
            id=m["id"],
            from_=m.get("from", ""),
            to=m.get("to", ""),
            subject=m.get("subject", ""),
            date=m.get("date", ""),
            text=m.get("text", ""),
            html=m.get("html", ""),
            attachments=[
                Attachment(
                    index=a.get("index", i),
                    filename=a.get("filename", ""),
                    mime=a.get("mime", ""),
                    size=a.get("size", 0),
                    downloadable=a.get("downloadable", False),
                    download_url=a.get("download_url"),
                    requires_plan=a.get("requires_plan"),
                )
                for i, a in enumerate(m.get("attachments", []))
            ],
        )

    def _long_poll(self, address: str, seconds: int, since: Optional[str]) -> Optional[MessageSummary]:
        """One long poll. None means the server timed out, which is not a
        failure: it answers 200 with ``timed_out`` true so the caller can go
        round again."""
        path = f"/inboxes/{_esc(address)}/wait?timeout={seconds}"
        if since:
            path += f"&since={quote(since)}"

        # The HTTP timeout sits above the wait window so the connection is not
        # cut while the server is still legitimately holding it open.
        r = self._request("GET", path, timeout=seconds + 10)

        if r.get("timed_out") is True:
            return None
        m = r.get("message")
        return self._to_summary(m) if m else None

    def wait_for_message(
        self,
        address: str,
        timeout: int = 30,
        since: Optional[str] = None,
        *,
        subject: Optional[str] = None,
        from_: Optional[str] = None,
        q: Optional[str] = None,
        include_existing: bool = True,
    ) -> Optional[MessageSummary]:
        """Wait until a matching message is in the inbox.

        Returns None on timeout rather than raising, because nothing has gone
        wrong: the mail simply has not arrived. That lets a caller assert
        without wrapping this in a try/except. Use ``wait_for_code`` when you
        want a missing message to be an error.

        **Changed in 2.0.0.** This now looks at what is ALREADY in the inbox
        before waiting for anything new, and ``timeout`` is honoured in full
        rather than being clamped to one 55-second request.

        Both changes fix the same class of silent failure. The long-poll
        endpoint signals mail ARRIVING, so a message that landed before the
        call started was invisible to it. In a test that is the common case,
        not the edge case: you create the inbox, trigger the signup, and the
        email frequently wins the race against the next line of code. The old
        behaviour then burned the whole timeout and returned None while the
        message sat there. This is not hypothetical: the GitHub Action for this
        same API shipped with 21 passing tests and failed on the first real
        email for exactly this reason.

        Pass ``include_existing=False`` for the pre-2.0 behaviour of waiting
        only for genuinely new mail, which is the right choice when you are
        watching an inbox for successive messages.

        ``subject``, ``from_`` and ``q`` narrow which message counts. Setting
        one matters more than it looks: without a filter the first message to
        arrive wins, and in a real signup a welcome email often beats the
        verification email by a second, so an unfiltered wait hands back the
        wrong message and the test then fails a long way from the cause.

        Note that a filter switches this to polling the message listing,
        because the long-poll endpoint cannot filter.
        """
        filtering = bool(subject or from_ or q)
        deadline = time.monotonic() + timeout

        def look() -> Optional[MessageSummary]:
            return self.list_messages(
                address, limit=1, subject=subject, from_=from_, q=q
            ).first

        if include_existing:
            found = look()
            if found is not None:
                return found

        while time.monotonic() < deadline:
            if filtering:
                time.sleep(min(FILTERED_POLL_INTERVAL, max(0.0, deadline - time.monotonic())))
                found = look()
                if found is not None:
                    return found
                continue

            remaining = deadline - time.monotonic()
            seconds = max(1, min(MAX_WAIT_SECONDS, int(remaining)))
            message = self._long_poll(address, seconds, since)
            if message is not None:
                return message

        return None

    def wait_for_code(
        self,
        address: str,
        timeout: int = 30,
        *,
        subject: Optional[str] = None,
        from_: Optional[str] = None,
        q: Optional[str] = None,
        include_existing: bool = True,
    ) -> str:
        """Wait for a message and return its verification code as a string.

        The one-liner most tests want. Requires a paid plan for the extraction
        step.

        Raises rather than returning None, because a caller asking for a bare
        string has nowhere to put "the mail came but held no code" and would
        otherwise type an empty string into the form under test. Use
        ``wait_for_otp`` when you would rather inspect the candidates.
        """
        message = self.wait_for_message(
            address,
            timeout,
            subject=subject,
            from_=from_,
            q=q,
            include_existing=include_existing,
        )
        if message is None:
            raise TempMailError(
                f"No matching message arrived at {address} within {timeout}s"
            )

        otp = self.get_otp(address, message.id)
        if not otp.code:
            raise TempMailError(
                f"Message {message.id} arrived but no verification code could be "
                f"extracted from it (confidence {otp.confidence})"
            )
        return otp.code

    def get_otp(self, address: str, message_id: str) -> OtpResult:
        """Extract the verification code from a message. Requires a paid plan.

        ``code`` is None when nothing scored highly enough to be trusted;
        check ``candidates`` in that case rather than assuming there was no
        code at all.
        """
        r = self._request("GET", f"/inboxes/{_esc(address)}/messages/{quote(message_id)}/otp")
        return OtpResult(
            message_id=r["message_id"],
            code=r.get("code"),
            confidence=r.get("confidence", "none"),
            candidates=r.get("candidates", []),
        )

    def wait_for_otp(
        self,
        address: str,
        timeout: int = 30,
        *,
        subject: Optional[str] = None,
        from_: Optional[str] = None,
        q: Optional[str] = None,
        include_existing: bool = True,
    ) -> Optional[OtpResult]:
        """Wait for a message and return its verification code in one step.

        This is the common case for a signup test: trigger the flow, then get
        the code. Returns None if no message arrived within the timeout.
        Requires a paid plan, since OTP extraction does.

        A message that arrives with no recognisable code is not None: you get
        an ``OtpResult`` whose ``code`` is None and whose ``candidates`` are
        worth looking at. Use ``wait_for_code`` if you would rather that were
        an error.
        """
        message = self.wait_for_message(
            address,
            timeout=timeout,
            subject=subject,
            from_=from_,
            q=q,
            include_existing=include_existing,
        )
        if message is None:
            return None
        return self.get_otp(address, message.id)

    def get_links(self, address: str, message_id: str) -> LinksResult:
        """Extract every link in a message and identify the one it wants
        clicked. Requires a paid plan.

        The counterpart to ``get_otp`` for emails that want a click rather than
        a code. ``action_url`` is rarely the first link in the body: that is
        almost always the logo. Links are scored on the wording around them,
        where they sit in the message, and whether the URL carries something
        that looks like a one-time token. Each is typed, so unsubscribe,
        social, legal, tracking and view-in-browser links are labelled instead
        of competing to be the answer.

        ``action_url`` is None rather than a guess when nothing scores highly
        enough. Following the wrong link in a test fails in a way that is very
        hard to trace, so no answer is safer than a wrong one.
        """
        r = self._request("GET", f"/inboxes/{_esc(address)}/messages/{quote(message_id)}/links")
        return LinksResult(
            message_id=r.get("message_id", message_id),
            action_url=r.get("action_url"),
            confidence=r.get("confidence", "none"),
            links=[
                Link(
                    url=link.get("url", ""),
                    text=link.get("text", ""),
                    type=link.get("type", "other"),
                    score=link.get("score", 0),
                    unwrapped_url=link.get("unwrapped_url"),
                )
                for link in r.get("links", [])
            ],
        )

    def wait_for_links(
        self,
        address: str,
        timeout: int = 30,
        *,
        subject: Optional[str] = None,
        from_: Optional[str] = None,
        q: Optional[str] = None,
        include_existing: bool = True,
    ) -> Optional[LinksResult]:
        """Wait for a message and extract its links. Requires a paid plan."""
        message = self.wait_for_message(
            address,
            timeout=timeout,
            subject=subject,
            from_=from_,
            q=q,
            include_existing=include_existing,
        )
        if message is None:
            return None
        return self.get_links(address, message.id)

    def wait_for_action_url(
        self,
        address: str,
        timeout: int = 30,
        *,
        subject: Optional[str] = None,
        from_: Optional[str] = None,
        q: Optional[str] = None,
        include_existing: bool = True,
    ) -> str:
        """Wait for a message and return the single link it wants clicked.

        Raises rather than returning None, for the same reason as
        ``wait_for_code``. Use ``wait_for_links`` to get it as data.
        """
        message = self.wait_for_message(
            address,
            timeout=timeout,
            subject=subject,
            from_=from_,
            q=q,
            include_existing=include_existing,
        )
        if message is None:
            raise TempMailError(
                f"No matching message arrived at {address} within {timeout}s"
            )

        links = self.get_links(address, message.id)
        if not links.action_url:
            raise TempMailError(
                f"Message {message.id} arrived but no clear call-to-action link was "
                f"found in it (confidence {links.confidence})"
            )
        return links.action_url

    # ------------------------------------------------------------------
    # account
    # ------------------------------------------------------------------

    def get_usage(self) -> Usage:
        """Plan, remaining quota and subscription.

        Works without an API key, which is the point of it: a keyless call
        reports the free tier including how many of today's three inboxes this
        IP has used, so you can see where you stand before paying for
        anything.

        Worth reading inside a long suite: check ``usage.hourly.remaining`` and
        slow down before hitting the limit rather than handling the 429 after.
        """
        r = self._request("GET", "/usage")
        hourly = r.get("hourly") or {}
        key = r.get("key")
        subscription = r.get("subscription")

        return Usage(
            plan=r.get("plan", "free"),
            paid=bool(r.get("paid")),
            hourly=HourlyQuota(
                limit=hourly.get("limit", 0),
                used=hourly.get("used", 0),
                remaining=hourly.get("remaining", 0),
                resets_at=hourly.get("resets_at"),
                resets_in_seconds=hourly.get("resets_in_seconds", 0),
            ),
            lifetime_requests=r.get("lifetime_requests", 0),
            key=KeyInfo(
                id=key.get("id", 0),
                last_four=key.get("last_four", ""),
                status=key.get("status", ""),
                created_at=key.get("created_at"),
                last_used_at=key.get("last_used_at"),
            )
            if key
            else None,
            subscription=SubscriptionInfo(
                status=subscription.get("status", ""),
                expires_at=subscription.get("expires_at"),
                days_remaining=subscription.get("days_remaining", 0),
                in_grace_period=bool(subscription.get("in_grace_period")),
            )
            if subscription
            else None,
            # None and 0 mean different things: None is "no daily cap applies",
            # 0 is "the cap applies and none have been used".
            inboxes_created_today=r.get("inboxes_created_today"),
            upgrade_url=r.get("upgrade"),
        )

    # ------------------------------------------------------------------
    # attachments
    # ------------------------------------------------------------------

    def download_attachment(
        self, address: str, message_id: str, index: int
    ) -> AttachmentFile:
        """Download an attachment. Requires the Pro plan.

        Addressed by index, matching the order in ``message.attachments``,
        because attachment ids are regenerated on every read.
        """
        response = self._request(
            "GET",
            f"/inboxes/{_esc(address)}/messages/{quote(message_id)}/attachments/{index}",
            raw=True,
        )
        disposition = response.headers.get("content-disposition", "")
        filename = "attachment"
        if 'filename="' in disposition:
            filename = disposition.split('filename="', 1)[1].split('"', 1)[0]
        content = response.content
        return AttachmentFile(
            filename=filename,
            mime=response.headers.get("content-type", "application/octet-stream"),
            size=len(content),
            content=content,
        )

    # ------------------------------------------------------------------
    # webhooks
    # ------------------------------------------------------------------

    def register_webhook(self, url: str) -> WebhookRegistration:
        """Register the URL mail should be POSTed to. Requires a paid plan.

        One URL per key; calling this again replaces it and issues a new
        secret, which is how the secret is rotated. The secret is returned
        once, here.
        """
        r = self._request("POST", "/webhooks", json_body={"url": url})
        return WebhookRegistration(url=r["url"], secret=r["secret"])

    def get_webhook(self) -> Optional[Dict[str, Any]]:
        """Current registration plus recent delivery attempts, for debugging."""
        r = self._request("GET", "/webhooks")
        if not r.get("webhook"):
            return None
        return {"webhook": r["webhook"], "recent_deliveries": r.get("recent_deliveries", [])}

    def delete_webhook(self) -> bool:
        return bool(self._request("DELETE", "/webhooks").get("removed"))

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> "TempMail":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
