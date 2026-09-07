"""Client for the Best Temp Mail API."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from .models import (
    Attachment,
    AttachmentFile,
    AuthenticationError,
    Inbox,
    Message,
    MessageSummary,
    NotFoundError,
    OtpResult,
    PaymentRequiredError,
    RateLimit,
    RateLimitError,
    TempMailError,
    TimeoutError,
    WebhookRegistration,
)

DEFAULT_BASE_URL = "https://api.best-tempmail.com/v1"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2

# The server clamps waits to 55 seconds; sending more just wastes a round trip.
MAX_WAIT_SECONDS = 55


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

    def create_inbox(
        self, username: Optional[str] = None, domain: Optional[str] = None
    ) -> Inbox:
        """Create a disposable inbox. Both arguments may be omitted."""
        body: Dict[str, Any] = {}
        if username:
            body["username"] = username
        if domain:
            body["domain"] = domain
        r = self._request("POST", "/inboxes", json_body=body)
        return Inbox(address=r["address"], created_at=r["created_at"], expires_at=r["expires_at"])

    def get_inbox(self, address: str) -> Inbox:
        r = self._request("GET", f"/inboxes/{address}")
        return Inbox(address=r["address"], created_at=r["created_at"], expires_at=r["expires_at"])

    def delete_inbox(self, address: str) -> bool:
        return bool(self._request("DELETE", f"/inboxes/{address}").get("success"))

    # ------------------------------------------------------------------
    # messages
    # ------------------------------------------------------------------

    def get_messages(self, address: str, limit: int = 100) -> List[MessageSummary]:
        r = self._request("GET", f"/inboxes/{address}/messages?limit={limit}")
        return [
            MessageSummary(
                id=m["id"],
                from_=m["from"],
                subject=m["subject"],
                date=m.get("date"),
                has_attachments=m.get("has_attachments", False),
            )
            for m in r.get("messages", [])
        ]

    def get_message(self, address: str, message_id: str) -> Message:
        r = self._request("GET", f"/inboxes/{address}/messages/{message_id}")
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

    def wait_for_message(
        self, address: str, timeout: int = 30, since: Optional[str] = None
    ) -> Optional[MessageSummary]:
        """Wait for the next message, replacing a polling loop with one call.

        Returns None on timeout rather than raising, because nothing has gone
        wrong: the mail simply has not arrived. That lets a caller loop or
        assert without wrapping this in a try/except.

        Without ``since``, messages already present when the call starts are
        treated as seen, so only genuinely new mail is returned.
        """
        wait = min(timeout, MAX_WAIT_SECONDS)
        path = f"/inboxes/{address}/wait?timeout={wait}"
        if since:
            path += f"&since={since}"

        # The HTTP timeout sits above the wait window so the connection is not
        # cut while the server is still legitimately holding it open.
        r = self._request("GET", path, timeout=wait + 10)

        m = r.get("message")
        if not m:
            return None
        return MessageSummary(
            id=m["id"],
            from_=m["from"],
            subject=m["subject"],
            date=m.get("date"),
            has_attachments=m.get("has_attachments", False),
        )

    def get_otp(self, address: str, message_id: str) -> OtpResult:
        """Extract the verification code from a message. Requires a paid plan.

        ``code`` is None when nothing scored highly enough to be trusted;
        check ``candidates`` in that case rather than assuming there was no
        code at all.
        """
        r = self._request("GET", f"/inboxes/{address}/messages/{message_id}/otp")
        return OtpResult(
            message_id=r["message_id"],
            code=r.get("code"),
            confidence=r.get("confidence", "none"),
            candidates=r.get("candidates", []),
        )

    def wait_for_otp(
        self, address: str, timeout: int = 30
    ) -> Optional[OtpResult]:
        """Wait for a message and return its verification code in one step.

        This is the common case for a signup test: trigger the flow, then get
        the code. Returns None if no message arrived within the timeout.
        Requires a paid plan, since OTP extraction does.
        """
        message = self.wait_for_message(address, timeout=timeout)
        if message is None:
            return None
        return self.get_otp(address, message.id)

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
            f"/inboxes/{address}/messages/{message_id}/attachments/{index}",
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
