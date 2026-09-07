"""Best Temp Mail: disposable email inboxes for automated testing.

https://best-tempmail.com/api
"""

from .client import TempMail
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
from .webhooks import parse_webhook, verify_webhook_signature

__version__ = "1.0.0"

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
    "Attachment",
    "AttachmentFile",
    "OtpResult",
    "WebhookRegistration",
    "RateLimit",
    # webhooks
    "verify_webhook_signature",
    "parse_webhook",
]
