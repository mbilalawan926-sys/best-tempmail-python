"""Best Temp Mail: disposable email inboxes for automated testing.

https://best-tempmail.com/api
"""

from .client import TempMail
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
from .webhooks import parse_webhook, verify_webhook_signature

__version__ = "2.0.0"

__all__ = [
    "TempMail",
    # errors
    "TempMailError",
    "AuthenticationError",
    "PaymentRequiredError",
    "NotFoundError",
    "RateLimitError",
    "TimeoutError",
    # models
    "Inbox",
    "Message",
    "MessageSummary",
    "MessageList",
    "Attachment",
    "AttachmentFile",
    "OtpResult",
    "Link",
    "LinksResult",
    "Usage",
    "HourlyQuota",
    "KeyInfo",
    "SubscriptionInfo",
    "BulkResult",
    "WebhookRegistration",
    "RateLimit",
    # webhooks
    "verify_webhook_signature",
    "parse_webhook",
]
