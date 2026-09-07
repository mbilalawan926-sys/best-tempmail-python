import base64, hmac, json, sys, time
from hashlib import sha256
sys.path.insert(0, "src"); sys.path.insert(0, "tests")

from best_tempmail import (TempMail, PaymentRequiredError, NotFoundError,
                           verify_webhook_signature, parse_webhook)
import mock_server

BASE = "http://127.0.0.1:4990/v1"
srv = mock_server.start(4990)
time.sleep(0.4)

passed = failed = 0
def check(ok, label, detail=""):
    global passed, failed
    print(f"{'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if ok: passed += 1
    else: failed += 1

free = TempMail(base_url=BASE)
dev  = TempMail(base_url=BASE, api_key="DEV")
pro  = TempMail(base_url=BASE, api_key="PRO")

check(len(free.get_domains()) == 5, "get_domains")
check(free.health()["status"] == "ok", "health")

inbox = free.create_inbox()
check(inbox.address == "test1234@dextde.site", "create_inbox", inbox.address)
check("15:00" in inbox.expires_at, "  free inbox expires in 2h", inbox.expires_at)
check("09-08" in pro.create_inbox().expires_at, "  pro inbox expires in 24h")

check(free.get_inbox(inbox.address).address == inbox.address, "get_inbox")

msgs = free.get_messages(inbox.address)
check(len(msgs) == 1 and msgs[0].has_attachments, "get_messages maps has_attachments")
check(msgs[0].from_ == '"S" <a@b.com>', "  'from' mapped to from_ (reserved word)")

check(free.rate_limit is not None and free.rate_limit.limit == 150,
      "rate limit captured", f"{free.rate_limit.remaining}/{free.rate_limit.limit}")

m = free.get_message(inbox.address, "msg1")
check(m.attachments[0].requires_plan == "pro", "attachment metadata maps requires_plan")
check(pro.get_message(inbox.address, "msg1").attachments[0].downloadable, "pro sees downloadable True")

check(free.wait_for_message(inbox.address, timeout=5) is None, "wait_for_message returns None on timeout")
got = free.wait_for_message(inbox.address, timeout=5)
check(got is not None and got.id == "msg1", "wait_for_message returns the message")

try:
    free.get_otp(inbox.address, "msg1"); check(False, "free blocked from OTP")
except PaymentRequiredError as e:
    check(e.plan == "Free", "free -> PaymentRequiredError", f"plan={e.plan}")

otp = dev.get_otp(inbox.address, "msg1")
check(otp.code == "889231" and otp.confidence == "high", "get_otp on paid plan", otp.code)

try:
    free.get_messages("nobody@dextde.site"); check(False, "unknown inbox")
except NotFoundError:
    check(True, "unknown inbox -> NotFoundError")

try:
    dev.download_attachment(inbox.address, "msg1", 0); check(False, "developer blocked from attachments")
except PaymentRequiredError:
    check(True, "developer -> PaymentRequiredError on attachment")

f = pro.download_attachment(inbox.address, "msg1", 0)
check(f.filename == "invoice.pdf" and len(f.content) > 0, "download_attachment", f"{f.filename} {f.size}b")

reg = pro.register_webhook("https://x.com/h")
check(reg.secret == "whsec_abc123", "register_webhook returns secret")
info = pro.get_webhook()
check(info["recent_deliveries"][0]["status_code"] == 200, "get_webhook maps deliveries")
check(pro.delete_webhook() is True, "delete_webhook")

secret, body = "s3cret", json.dumps({"event": "message.received"})
ts = int(time.time())
sig = base64.b64encode(hmac.new(secret.encode(), f"{ts}.{body}".encode(), sha256).digest()).decode()
check(verify_webhook_signature(body, f"v1,{sig}", ts, secret)[0], "verify accepts valid")
check(not verify_webhook_signature(body, "v1,ZmFrZQ==", ts, secret)[0], "verify rejects forgery")
check(verify_webhook_signature(body, f"v1,{sig}", ts - 9999, secret)[1] == "expired", "verify rejects replay")
check(parse_webhook(body, f"v1,{sig}", ts, secret)["event"] == "message.received", "parse_webhook returns payload")
try:
    parse_webhook(body, "v1,bad", ts, secret); check(False, "parse_webhook raises on bad sig")
except ValueError:
    check(True, "parse_webhook raises on bad signature")

check(free.delete_inbox(inbox.address) is True, "delete_inbox")

with TempMail(base_url=BASE) as c:
    check(len(c.get_domains()) == 5, "context manager works")

print(f"\n{passed} passed, {failed} failed")
srv.shutdown()
sys.exit(1 if failed else 0)
