"""A tiny local stand-in for the Telegram Bot API (getMe, sendMessage) — tests only."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


class StubTelegram:
    """Records every sendMessage; `fail_texts` containing a substring get a 400 error back."""

    def __init__(self, token: str = "123:TEST", fail_substring: str | None = None):
        self.token, self.fail_substring = token, fail_substring
        self.sent: list[dict] = []
        self.calls: list[str] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                ctype = self.headers.get("Content-Type", "")
                if "json" in ctype:
                    params = json.loads(body or b"{}")
                else:
                    params = {k: v[0] for k, v in parse_qs(body.decode()).items()}
                method = self.path.rsplit("/", 1)[-1]
                stub.calls.append(method)
                if not self.path.startswith(f"/bot{stub.token}/"):
                    return self._reply(401, {"ok": False, "error_code": 401,
                                             "description": "Unauthorized"})
                if method == "getMe":
                    return self._reply(200, {"ok": True, "result": {
                        "id": 1, "is_bot": True, "first_name": "stub", "username": "stub_bot"}})
                if method == "sendMessage":
                    text = params.get("text", "")
                    if stub.fail_substring and stub.fail_substring in text:
                        return self._reply(400, {"ok": False, "error_code": 400,
                                                 "description": "Bad Request: stub refused"})
                    stub.sent.append(params)
                    return self._reply(200, {"ok": True, "result": {
                        "message_id": len(stub.sent), "date": int(time.time()),
                        "chat": {"id": int(params.get("chat_id", 0)), "type": "private"},
                        "text": text}})
                return self._reply(404, {"ok": False, "error_code": 404, "description": "nope"})

            def _reply(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/bot"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
