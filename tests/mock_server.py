"""A tiny fake of the Nukerhugo enterprise API for tests.

Serves: POST /v1/chat/completions (SSE, tool calls), GET /v1/models,
        POST /?endpoint=signup, GET /?endpoint=usage
Behaviour is scripted from the last message so tests are deterministic.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KEY = "nh_test_key_123456"


class State:
    def __init__(self):
        self.used = 0
        self.budget = 100_000
        self.signups = {}
        self.requests = []          # parsed chat payloads
        self.reject_tools = False   # simulate a server without tool support
        self.force_status = None    # e.g. 429 to simulate an exhausted budget


def make_server(state=None):
    state = state or State()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj, headers=None):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if q.get("endpoint") == ["usage"]:
                if self.headers.get("X-API-Key") != KEY:
                    return self._send(401, {"detail": "invalid key"})
                return self._send(200, {"plan": "free", "daily_budget": state.budget,
                                        "tokens_used": state.used,
                                        "tokens_remaining": state.budget - state.used})
            if u.path == "/v1/models":
                return self._send(200, {"data": [{"id": "deepseek-v4-flash"}, {"id": "other-model"}]})
            self._send(404, {"detail": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if q.get("endpoint") == ["signup"]:
                email = self._body().get("email", "")
                if email in state.signups:
                    return self._send(409, {"detail": "A key already exists for this email."})
                state.signups[email] = KEY
                return self._send(200, {"api_key": KEY, "plan": "free"})
            if u.path == "/v1/chat/completions":
                return self._chat()
            self._send(404, {"detail": "not found"})

        def _chat(self):
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                return self._send(401, {"detail": "invalid key"})
            payload = self._body()
            state.requests.append(payload)
            if state.force_status == 429:
                return self._send(429, {"detail": "Daily token budget used up"})
            if state.reject_tools and payload.get("tools"):
                return self._send(400, {"detail": "tools are not supported by this model"})
            msgs = payload["messages"]
            last = msgs[-1]
            has_tools = bool(payload.get("tools"))

            def sse(chunks):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for ch in chunks:
                    self.wfile.write(f"data: {json.dumps(ch)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")

            def delta(**d):
                return {"choices": [{"index": 0, "delta": d}]}

            def usage_chunk(p, c):
                state.used += p + c
                return {"choices": [], "usage": {"prompt_tokens": p, "completion_tokens": c,
                                                 "total_tokens": p + c}}

            text = last.get("content") or ""
            if last["role"] == "user" and "slow" in text.lower():
                import time
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for i in range(40):
                        self.wfile.write(f"data: {json.dumps(delta(content=f'word{i} '))}\n\n".encode())
                        self.wfile.flush()
                        time.sleep(0.25)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if last["role"] == "user" and "readme" in text.lower() and has_tools:
                args = json.dumps({"path": "README.md", "content": "# Hello\nmade by agent\n"})
                half = len(args) // 2
                return sse([
                    delta(reasoning_content="thinking about a readme"),
                    delta(content="Creating the file. "),
                    delta(tool_calls=[{"index": 0, "id": "call_a", "type": "function",
                                       "function": {"name": "write_file", "arguments": args[:half]}}]),
                    delta(tool_calls=[{"index": 0, "function": {"arguments": args[half:]}}]),
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                    usage_chunk(300, 60),
                ])
            if last["role"] == "user" and "readme" in text.lower() and not has_tools:
                block = ('```tool\n{"name": "write_file", "arguments": '
                         '{"path": "README.md", "content": "# Hello\\ntext mode\\n"}}\n```')
                return sse([delta(content="On it.\n" + block), usage_chunk(250, 40)])
            if last["role"] in ("tool",) or (last["role"] == "user" and text.startswith("Tool result")):
                return sse([delta(content="Done - "), delta(content="README written."),
                            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                            usage_chunk(400, 8)])
            return sse([delta(content="Hello from the mock."), usage_chunk(100, 5)])

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    server.state = state
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state
