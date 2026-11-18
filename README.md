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

`wait_for_message` returns as soon as a matching message is in the inbox. It returns `None` on timeout rather than raising, because nothing has gone wrong: the mail just has not arrived yet.

```python
message = client.wait_for_message(
    inbox.address,
    timeout=120,        # honoured in full, across as many requests as it takes
    subject="verify",   # optional, and worth setting: see below
)

if message is None:
    ...  # nothing matching arrived in time
```

**Pass `subject` or `from_` when you can.** Without a filter the first message to arrive wins, and in a real signup a welcome email often beats the verification email by a second. An unfiltered wait then hands you the wrong message and the test fails a long way from the cause.

### What changed in 2.0.0, and why

Two fixes, both for the same silent failure.

**It now looks at mail that has already been delivered.** The API's long-poll endpoint signals mail *arriving*, so a message that landed before the call started was invisible to it. In a test that is the normal case rather than the edge case: you create the inbox, trigger the signup, and the email frequently wins the race against your next line of code. 1.x then burned the whole timeout and returned `None` while the message sat in the inbox unread.

This is not hypothetical. The GitHub Action for this same API shipped with 21 passing tests and failed on the first real email for exactly this reason, because its mock answered the long poll whenever asked and so could not express the difference.

**`timeout` is honoured in full.** 1.x clamped it to a single 55-second request, so `timeout=120` quietly waited 55 seconds and reported no mail, with nothing telling the caller it had given up early.

If you were relying on the old semantics, which is reasonable when you are watching one inbox for successive messages, ask for them:

```python
message = client.wait_for_message(address, timeout=30, include_existing=False)
```

### Getting the code or the link in one call

```python
code = client.wait_for_code(inbox.address, subject="verify")   # a str, raises if none
url = client.wait_for_action_url(inbox.address)                # a str, raises if none
```

These raise rather than returning `None`, because a caller asking for a bare string has nowhere to put "the mail came but held no code" and would otherwise type an empty string into the form under test. Use `wait_for_otp` or `wait_for_links` when you would rather have it as data and inspect the candidates yourself.

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
| `get_usage()` | any, and informative without a key |
| `create_inbox(username=None, domain=None, lifetime_minutes=None)` | any |
| `create_inboxes(count, domain=None, lifetime_minutes=None)` | paid |
| `get_inbox(address)` | any |
| `delete_inbox(address, missing_ok=True)` | any |
| `get_messages(address, limit=100, **filters)` | any |
| `list_messages(address, **filters)` | any |
| `get_message(address, message_id)` | any |
| `wait_for_message(address, timeout=30, **filters)` | any |
| `get_otp(address, message_id)` | paid |
| `wait_for_otp(address, timeout=30, **filters)` | paid |
| `wait_for_code(address, timeout=30, **filters)` | paid |
| `get_links(address, message_id)` | paid |
| `wait_for_links(address, timeout=30, **filters)` | paid |
| `wait_for_action_url(address, timeout=30, **filters)` | paid |
| `download_attachment(address, message_id, index)` | Pro |
| `register_webhook(url)` | paid |
| `get_webhook()` | paid |
| `delete_webhook()` | paid |

`**filters` means `subject`, `from_`, `q`, and for the listing methods also `limit`, `offset`, `since`, `until` and `has_attachments`.

### Searching a busy inbox

`list_messages` returns the paging and search metadata that a plain list cannot carry:

```python
page = client.list_messages(address, q="ORD-48812", since="2026-07-04T00:00:00Z", limit=20)

if not page.messages:
    if page.conclusively_empty:
        ...  # nothing in the inbox matches, definitively
    else:
        ...  # the search hit its ceiling: narrow it or move `since`
```

`q` searches the sender, the subject and the decoded body, ignoring text inside script and style blocks so a tracking script cannot produce a match.

Full API documentation: [best-tempmail.com/api](https://best-tempmail.com/api)
OpenAPI spec: [api.best-tempmail.com/v1/openapi.json](https://api.best-tempmail.com/v1/openapi.json)

## Requirements

Python 3.8 or later. Depends on `requests`.

## License

MIT
