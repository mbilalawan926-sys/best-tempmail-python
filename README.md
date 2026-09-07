# best-tempmail

Disposable email inboxes for automated testing. Create an inbox, wait for mail, pull out the verification code.

Built for signup flows, password resets, and anything else where a test needs to receive a real email.

```bash
pip install best-tempmail
```

## Quick start

No API key needed to try it. The free tier is keyless.

```python
from best_tempmail import TempMail

client = TempMail()

inbox = client.create_inbox()
print(inbox.address)   # abc1234@dextde.site

# trigger your signup flow with that address, then:
message = client.wait_for_message(inbox.address, timeout=55)
print(message.subject)
```

## Getting the verification code

The usual reason to receive email in a test is to read a code out of it. Rather than writing a regex for every service's format, ask for the code:

```python
client = TempMail(api_key=os.environ["BTM_API_KEY"])

inbox = client.create_inbox()
sign_up_with_email(inbox.address)

otp = client.wait_for_otp(inbox.address, timeout=55)
enter_code(otp.code)
```

`code` is `None` when nothing scored highly enough to be trusted. That is deliberate: a wrong code fails a test in a way that is hard to trace, so the API returns nothing rather than a guess. Check `otp.candidates` if you want to see what else was considered.

## pytest example

```python
import os
import pytest
from best_tempmail import TempMail

@pytest.fixture
def mail():
    with TempMail(api_key=os.environ["BTM_API_KEY"]) as client:
        yield client

def test_signup_sends_verification_code(mail, browser):
    inbox = mail.create_inbox()

    browser.goto("/signup")
    browser.fill("#email", inbox.address)
    browser.click("#submit")

    otp = mail.wait_for_otp(inbox.address, timeout=55)
    assert otp is not None and otp.code

    browser.fill("#code", otp.code)
    browser.click("#verify")
    assert browser.is_visible("#welcome")
```

## Waiting for mail

`wait_for_message` holds one connection open until mail arrives, instead of polling in a loop. It returns `None` on timeout rather than raising, because nothing has gone wrong: the mail just has not arrived yet.

```python
message = client.wait_for_message(
    inbox.address,
    timeout=55,          # seconds, capped at 55 by the server
    since=last_seen_id,  # optional: return the first message that is not this one
)

if message is None:
    ...  # nothing arrived in time
```

Without `since`, anything already in the inbox when the call starts is treated as seen, so you only get genuinely new mail.

## Reading messages

```python
messages = client.get_messages(inbox.address)          # newest first
full = client.get_message(inbox.address, messages[0].id)

full.subject
full.text
full.html
full.attachments
```

Note `message.from_` rather than `from`: the latter is a reserved word in Python.

## Attachments

Metadata is available on every plan. Downloading the bytes needs Pro.

```python
message = client.get_message(inbox.address, message_id)

for att in message.attachments:
    print(att.filename, att.size, att.downloadable)

    if att.downloadable:
        file = client.download_attachment(inbox.address, message_id, att.index)
        with open(file.filename, "wb") as f:
            f.write(file.content)
```

Attachments are addressed by `index`, not by id: ids are regenerated on every read and do not survive a round trip.

## Webhooks

Rather than asking for mail, have it pushed to you.

```python
reg = client.register_webhook("https://your-server.com/hooks/mail")
# store reg.secret: it is shown once, and you need it to verify deliveries
```

Then verify what arrives. **Verify before trusting it.** A webhook endpoint is a public URL that receives verification codes, and without a signature check anyone who learns the URL can post fabricated mail to it.

```python
from flask import Flask, request
from best_tempmail import parse_webhook

app = Flask(__name__)

@app.route("/hooks/mail", methods=["POST"])
def mail_hook():
    event = parse_webhook(
        payload=request.get_data(),        # raw bytes, not request.json
        signature=request.headers["X-BTM-Signature"],
        timestamp=request.headers["X-BTM-Timestamp"],
        secret=os.environ["BTM_WEBHOOK_SECRET"],
    )
    print(event["address"], event["message"]["subject"])
    return "", 200
```

The raw body matters: the signature covers the exact bytes that were sent, so re-serialising a parsed object will never match. Use `request.get_data()` in Flask, `request.body` in Django, `await request.body()` in FastAPI.

`parse_webhook` raises `ValueError` when verification fails, so an unverified payload cannot be used by accident. Use `verify_webhook_signature` instead if you would rather handle the failure yourself.

## Errors

Errors are typed, because the right reaction differs.

```python
from best_tempmail import (
    RateLimitError, PaymentRequiredError,
    NotFoundError, AuthenticationError, TimeoutError,
)

try:
    otp = client.get_otp(address, message_id)
except RateLimitError as e:
    time.sleep(e.retry_after or 60)        # worth retrying
except PaymentRequiredError as e:
    print(f"Needs a higher plan than {e.plan}")   # retrying will not help
except NotFoundError:
    ...  # inbox or message is gone, or expired
```

Network errors, timeouts, 429 and 5xx are retried automatically with backoff. Refusals (401, 402, 404) are not: the request was understood, and repeating it only wastes quota.

## Rate limits

The most recent response's limits are always available:

```python
client.get_domains()
print(client.rate_limit)
# RateLimit(limit=2000, remaining=1996, reset=1788000000)
```

## Plans

| | Free | Founders / Developer | Pro |
| --- | :-: | :-: | :-: |
| Requests/hour | 150 | 2,000 | 5,000 |
| Inbox creation | 3/day per IP | unlimited | unlimited |
| Inbox lifetime | 2 hours | 2 hours | 24 hours |
| Polling, wait, WebSocket | yes | yes | yes |
| Webhooks | no | yes | yes |
| OTP extraction | no | yes | yes |
| Attachment downloads | no | no | yes |
| Concurrent waits | 5 | 5 | 20 |
| Commercial use | no | yes | yes |

The free tier needs no key at all. See [pricing](https://best-tempmail.com/api/pricing).

## Configuration

```python
client = TempMail(
    api_key="btm_sk_live_...",   # omit for the free tier
    timeout=30.0,                # per request, seconds
    max_retries=2,               # 0 disables retrying
    headers={},                  # sent with every request
)
```

The client can be used as a context manager, which closes the underlying HTTP session when done:

```python
with TempMail(api_key=key) as client:
    inbox = client.create_inbox()
```

## API reference

| Method | Plan |
| --- | --- |
| `get_domains()` | any |
| `health()` | any |
| `create_inbox(username=None, domain=None)` | any |
| `get_inbox(address)` | any |
| `delete_inbox(address)` | any |
| `get_messages(address, limit=100)` | any |
| `get_message(address, message_id)` | any |
| `wait_for_message(address, timeout=30, since=None)` | any |
| `get_otp(address, message_id)` | paid |
| `wait_for_otp(address, timeout=30)` | paid |
| `download_attachment(address, message_id, index)` | Pro |
| `register_webhook(url)` | paid |
| `get_webhook()` | paid |
| `delete_webhook()` | paid |

Full API documentation: [best-tempmail.com/api](https://best-tempmail.com/api)
OpenAPI spec: [api.best-tempmail.com/v1/openapi.json](https://api.best-tempmail.com/v1/openapi.json)

## Requirements

Python 3.8 or later. Depends on `requests`.

## License

MIT
