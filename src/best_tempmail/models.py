"""Errors and data models for the Best Temp Mail API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Errors
#
# Separate classes rather than one generic exception, because the right
# reaction differs sharply: a rate limit should be waited out, a payment error
# means the call will never succeed on this plan no matter how often it is
# retried, and a missing inbox usually means it expired.
#
#     try:
#         otp = client.get_otp(address, message_id)
#     except RateLimitError as e:
#         time.sleep(e.retry_after or 60)
#     except PaymentRequiredError as e:
#         print(f"Needs a higher plan than {e.plan}")
# ---------------------------------------------------------------------------


class TempMailError(Exception):
    """Base error. Everything raised by this library inherits from it."""

    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        request_id: Optional[str] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        #: Quote this when contacting support about a failed request.
        self.request_id = request_id
        self.body = body


class AuthenticationError(TempMailError):
    """401: the API key is missing, wrong, or revoked."""


class PaymentRequiredError(TempMailError):
    """402: the current plan does not include this capability.

    Retrying will not help; the caller needs a different plan.
    """

    def __init__(
        self,
        message: str,
        plan: Optional[str] = None,
        upgrade_url: Optional[str] = None,
        request_id: Optional[str] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, 402, request_id, body)
        #: Plan the caller is currently on.
        self.plan = plan
        self.upgrade_url = upgrade_url


class NotFoundError(TempMailError):
    """404: no such inbox, message or attachment, or it has expired."""


class RateLimitError(TempMailError):
    """429: hourly quota or burst limit hit. Worth waiting and retrying."""

    def __init__(
        self,
        message: str,
        retry_after: Optional[int] = None,
        request_id: Optional[str] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, 429, request_id, body)
        #: Seconds to wait before retrying, when the server suggested one.
        self.retry_after = retry_after


class TimeoutError(TempMailError):  # noqa: A001 - deliberately shadows the builtin
    """The request did not complete in time, or the network failed."""


# ---------------------------------------------------------------------------
# Models
#
# Dataclasses rather than raw dicts, so an editor can autocomplete and a typo
# fails loudly instead of silently returning None.
# ---------------------------------------------------------------------------


@dataclass
class Inbox:
    address: str
    created_at: str
    #: 2 hours after creation on Free and Developer, 24 hours on Pro.
    expires_at: str


@dataclass
class MessageSummary:
    id: str
    from_: str
    subject: str
    date: Optional[str]
    has_attachments: bool


@dataclass
class Attachment:
    #: Position in this message. Use this to download, not an id: attachment
    #: ids are regenerated on every read and do not survive a round trip.
    index: int
    filename: str
    mime: str
    #: Bytes.
    size: int
    #: Whether the current plan can download this particular attachment.
    downloadable: bool
    download_url: Optional[str] = None
    #: Plan needed, when the current one is too low.
    requires_plan: Optional[str] = None


@dataclass
class Message:
    id: str
    from_: str
    to: str
    subject: str
    date: str
    text: str
    html: str
    attachments: List[Attachment] = field(default_factory=list)


@dataclass
class OtpResult:
    message_id: str
    #: The extracted code, or None when nothing scored highly enough to be
    #: trusted. A None is deliberate: a wrong code fails a test in a way that
    #: is hard to trace back to its cause.
    code: Optional[str]
    confidence: str
    #: Runners-up, so a questionable pick can be inspected.
    candidates: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AttachmentFile:
    filename: str
    mime: str
    size: int
    #: Raw bytes.
    content: bytes


@dataclass
class WebhookRegistration:
    url: str
    #: Shown once, at registration. Store it to verify incoming signatures.
    secret: str


@dataclass
class RateLimit:
    limit: int
    remaining: int
    #: Unix seconds at which the hourly window resets.
    reset: int
