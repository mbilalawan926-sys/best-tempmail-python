"""Verify that a webhook really came from Best Temp Mail.

This matters more than it might seem. A webhook endpoint is a public URL that
receives verification codes. Without a signature check, anyone who learns the
URL can POST fabricated mail to it, and an application that trusts the payload
would act on a code an attacker chose.

Two details are easy to get wrong by hand, so these helpers handle both:

1. The signature covers the RAW body, exactly as sent. Re-serialising a parsed
   object (``json.dumps(request.json)``) can reorder keys or alter spacing, and
   the signature will then never match. In Flask use ``request.get_data()``; in
   Django use ``request.body``; in FastAPI use ``await request.body()``.

2. Comparison must be timing-safe. A plain ``==`` leaks, through how long it
   takes to fail, how much of a guessed signature was correct, which is enough
   to reconstruct a valid one given enough attempts. ``hmac.compare_digest``
   takes the same time regardless.
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256
from typing import Any, Dict, Optional, Tuple, Union

#: How old a delivery may be before it is rejected, in seconds.
#: This is replay protection: without it, a captured request could be replayed
#: indefinitely. Retries arrive within a few minutes, so five is comfortable.
DEFAULT_TOLERANCE_SECONDS = 300


def verify_webhook_signature(
    payload: Union[str, bytes],
    signature: str,
    timestamp: Union[str, int],
    secret: str,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> Tuple[bool, Optional[str]]:
    """Check a webhook signature.

    :param payload: Raw request body, exactly as received. Bytes or str, not a
        parsed dict.
    :param signature: The ``X-BTM-Signature`` header, in the form ``v1,<base64>``.
    :param timestamp: The ``X-BTM-Timestamp`` header.
    :param secret: The secret returned when the webhook was registered.
    :returns: ``(valid, reason)``. ``reason`` is None when valid, otherwise one
        of ``"bad_signature"``, ``"expired"`` or ``"malformed"``.
    """
    if not payload or not signature or not timestamp or not secret:
        return False, "malformed"

    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False, "malformed"

    if abs(int(time.time()) - ts) > tolerance_seconds:
        return False, "expired"

    body = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), f"{ts}.{body}".encode("utf-8"), sha256).digest()
    ).decode("ascii")

    # The header may carry one or several space-separated signatures, each
    # prefixed with a version: "v1,<sig>".
    for part in str(signature).split(" "):
        candidate = part.split(",", 1)[1] if "," in part else part
        if candidate and hmac.compare_digest(expected, candidate):
            return True, None

    return False, "bad_signature"


def parse_webhook(
    payload: Union[str, bytes],
    signature: str,
    timestamp: Union[str, int],
    secret: str,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> Dict[str, Any]:
    """Verify and parse in one step.

    Raises ``ValueError`` when the signature does not check out, so an
    unverified payload cannot be used by accident: the common mistake is to
    verify, ignore the result, and carry on with the parsed body regardless.

    ::

        @app.route("/hooks/mail", methods=["POST"])
        def mail_hook():
            event = parse_webhook(
                payload=request.get_data(),            # raw bytes, not request.json
                signature=request.headers["X-BTM-Signature"],
                timestamp=request.headers["X-BTM-Timestamp"],
                secret=os.environ["BTM_WEBHOOK_SECRET"],
            )
            print(event["message"]["subject"])
            return "", 200
    """
    valid, reason = verify_webhook_signature(
        payload, signature, timestamp, secret, tolerance_seconds
    )
    if not valid:
        raise ValueError(f"Webhook signature verification failed: {reason}")

    body = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    return json.loads(body)
