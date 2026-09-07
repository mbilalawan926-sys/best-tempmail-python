"""Stand-in for the API so the SDK can be tested offline.
Mirrors the real response shapes exactly, including snake_case fields."""
import json, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, unquote

_wait_calls = {"n": 0}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _plan(self):
        k = self.headers.get("x-api-key")
        return "pro" if k == "PRO" else "developer" if k == "DEV" else None

    def _send(self, code, obj, extra=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-RateLimit-Limit", "2000" if self._plan() else "150")
        self.send_header("X-RateLimit-Remaining", "149")
        self.send_header("X-RateLimit-Reset", "1788000000")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = unquote(urlparse(self.path).path); plan = self._plan()
        A = "test1234@dextde.site"
        if p == "/v1/domains":
            return self._send(200, {"domains": ["dextde.site","linsal.site","martaz.site","simtim.site","alagen.site"]})
        if p == "/v1/health":
            return self._send(200, {"status":"ok","version":"1","time":"2026-09-07T13:00:00Z"})
        if p == f"/v1/inboxes/{A}":
            return self._send(200, {"success":True,"address":A,"created_at":"2026-09-07 13:00:00","expires_at":"2026-09-07 15:00:00"})
        if p == "/v1/inboxes/nobody@dextde.site/messages":
            return self._send(404, {"error":"Inbox not found or expired.","request_id":"req_404"})
        if p == f"/v1/inboxes/{A}/messages":
            return self._send(200, {"success":True,"address":A,"messages":[
                {"id":"msg1","from":'"S" <a@b.com>',"subject":"Your code 889231","date":"2026-09-07T13:00:00Z","has_attachments":True}]})
        if p == f"/v1/inboxes/{A}/wait":
            _wait_calls["n"] += 1
            if _wait_calls["n"] == 1:
                return self._send(200, {"success":True,"address":A,"message":None,"timed_out":True,"waited_seconds":5})
            return self._send(200, {"success":True,"address":A,"message":{
                "id":"msg1","from":'"S" <a@b.com>',"subject":"Your code 889231","date":"2026-09-07T13:00:00Z","has_attachments":True}})
        if p == f"/v1/inboxes/{A}/messages/msg1/otp":
            if not plan:
                return self._send(402, {"error":"OTP extraction requires a paid plan.","plan":"Free","upgrade":"https://best-tempmail.com/api/pricing","request_id":"req_402"})
            return self._send(200, {"success":True,"message_id":"msg1","code":"889231","confidence":"high","candidates":[{"code":"889231","score":8}]})
        if p == f"/v1/inboxes/{A}/messages/msg1":
            can = plan == "pro"
            att = {"index":0,"filename":"invoice.pdf","mime":"application/pdf","size":1024,"downloadable":can}
            att.update({"download_url":"/v1/x"} if can else {"requires_plan":"pro"})
            return self._send(200, {"success":True,"message":{"id":"msg1","from":"a@b.com","to":A,
                "subject":"Your code 889231","date":"2026-09-07T13:00:00Z","text":"code 889231","html":"<p>889231</p>","attachments":[att]}})
        if p == f"/v1/inboxes/{A}/messages/msg1/attachments/0":
            if plan != "pro":
                return self._send(402, {"error":"Attachment downloads require the Pro plan.","plan":plan or "Free","request_id":"req_402"})
            data = b"%PDF-1.4 fake pdf bytes"
            self.send_response(200)
            self.send_header("Content-Type","application/pdf")
            self.send_header("Content-Disposition",'attachment; filename="invoice.pdf"')
            self.send_header("Content-Length",str(len(data)))
            self.end_headers(); return self.wfile.write(data)
        if p == "/v1/webhooks":
            if not plan: return self._send(402, {"error":"Webhooks require a paid plan.","plan":"Free"})
            return self._send(200, {"success":True,"webhook":{"url":"https://x.com/h","status":"active","created_at":1788000000,
                "last_success_at":1788000100,"last_failure_at":None,"consecutive_failures":0},
                "recent_deliveries":[{"attempt":1,"status_code":200,"error":None,"address":A,"message_id":"msg1","delivered_at":1788000100}]})
        self._send(404, {"error":"Not found","request_id":"req_x"})

    def do_POST(self):
        p = unquote(urlparse(self.path).path); plan = self._plan()
        length = int(self.headers.get("Content-Length", 0))
        if length: self.rfile.read(length)
        if p == "/v1/inboxes":
            exp = "2026-09-08 13:00:00" if plan == "pro" else "2026-09-07 15:00:00"
            return self._send(200, {"success":True,"address":"test1234@dextde.site","created_at":"2026-09-07 13:00:00","expires_at":exp})
        if p == "/v1/webhooks":
            if not plan: return self._send(402, {"error":"Webhooks require a paid plan.","plan":"Free","request_id":"req_402"})
            return self._send(200, {"success":True,"url":"https://x.com/h","secret":"whsec_abc123"})
        self._send(404, {"error":"Not found"})

    def do_DELETE(self):
        p = unquote(urlparse(self.path).path)
        if p == "/v1/webhooks": return self._send(200, {"success":True,"removed":True})
        if p.startswith("/v1/inboxes/"): return self._send(200, {"success":True})
        self._send(404, {"error":"Not found"})

def start(port=4990):
    s = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s
