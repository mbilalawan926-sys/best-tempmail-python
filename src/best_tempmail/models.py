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
    #: How long the inbox was actually given, which may be less than asked for.
    lifetime_minutes: Optional[int] = None
    #: Set when the server could not honour the request exactly, almost always
    #: because the requested lifetime exceeded the plan's maximum. The inbox is
    #: created at the maximum rather than failing, so this is the only warning
    #: that it will expire sooner than asked.
    note: Optional[str] = None


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


@dataclass
class MessageList:
    """A page of messages, plus what is needed to interpret it.

    ``get_messages`` returns a plain list for convenience. This is what
    ``list_messages`` returns, and it is the one to use when the inbox is busy
    enough that paging matters.
    """

    address: str
    messages: List[MessageSummary]
    limit: int = 0
    offset: int = 0
    returned: int = 0
    has_more: bool = False
    #: How many messages were examined to build this page.
    scanned: int = 0
    #: True when the search stopped at its ceiling with the page unfilled.
    #:
    #: This is what makes an empty result interpretable. Empty with this False
    #: means nothing in the inbox matches, definitively. Empty with it True
    #: means the message may exist further back, so narrow the filter or set
    #: ``since`` rather than paging blindly.
    scan_limit_reached: bool = False

    def __len__(self) -> int:
        return len(self.messages)

    def __iter__(self) -> Any:
        return iter(self.messages)

    @property
    def first(self) -> Optional[MessageSummary]:
        return self.messages[0] if self.messages else None

    @property
    def conclusively_empty(self) -> bool:
        """Empty, and the search was not cut short."""
        return not self.messages and not self.scan_limit_reached


@dataclass
class Link:
    """One link found in a message."""

    url: str
    #: The visible anchor text.
    text: str
    #: ``action``, ``unsubscribe``, ``social``, ``legal``, ``tracking``,
    #: ``view_in_browser`` or ``other``. Typed so the footer furniture cannot
    #: be mistaken for the call to action.
    type: str
    score: int
    #: The real destination when ``url`` points at a click tracker.
    unwrapped_url: Optional[str] = None

    @property
    def destination(self) -> str:
        """The unwrapped URL where there is one, otherwise the URL itself."""
        return self.unwrapped_url or self.url


@dataclass
class LinksResult:
    """Every link in a message, and the one it is asking the reader to click."""

    message_id: str
    #: The call to action, or None when nothing scored highly enough. Rarely
    #: the first link in the body: that is almost always the logo.
    action_url: Optional[str]
    confidence: str
    links: List[Link] = field(default_factory=list)

    @property
    def action_links(self) -> List[Link]:
        return [link for link in self.links if link.type == "action"]

    def of_type(self, type_: str) -> List[Link]:
        """Links of one type, for when the footer is what you are testing."""
        return [link for link in self.links if link.type == type_]


@dataclass
class HourlyQuota:
    limit: int
    used: int
    remaining: int
    resets_at: Optional[str] = None
    resets_in_seconds: int = 0

    def nearly_exhausted(self, threshold: int = 50) -> bool:
        """For slowing down before a 429 rather than handling one after."""
        return self.remaining < threshold


@dataclass
class KeyInfo:
    """The key in use. The key itself is never returned, only its last four
    characters, which is enough to tell two apart in a log without the log
    becoming somewhere a key leaks from."""

    id: int
    last_four: str
    status: str
    created_at: Optional[str] = None
    last_used_at: Optional[str] = None


@dataclass
class SubscriptionInfo:
    status: str
    expires_at: Optional[str] = None
    days_remaining: int = 0
    in_grace_period: bool = False


@dataclass
class Usage:
    """Plan, quota and subscription.

    Available without an API key, which is the point of it: a keyless call
    reports the free tier including ``inboxes_created_today`` against the
    3-per-day cap.
    """

    plan: str
    paid: bool
    hourly: HourlyQuota
    lifetime_requests: int = 0
    #: None for a keyless caller.
    key: Optional[KeyInfo] = None
    #: None for a keyless caller.
    subscription: Optional[SubscriptionInfo] = None
    #: Set for keyless callers only. None means no daily cap applies, which is
    #: a different thing from zero.
    inboxes_created_today: Optional[int] = None
    upgrade_url: Optional[str] = None


@dataclass
class BulkResult:
    """Several inboxes created in one call."""

    count: int
    #: One request of quota per inbox, so 25 charges 25. Reported by the server
    #: rather than left for the caller to work out.
    quota_charged: int
    inboxes: List[Inbox] = field(default_factory=list)
    lifetime_minutes: Optional[int] = None

    def __len__(self) -> int:
        return len(self.inboxes)

    @property
    def addresses(self) -> List[str]:
        """Just the addresses, which is usually what a fixture wants."""
        return [inbox.address for inbox in self.inboxes]
