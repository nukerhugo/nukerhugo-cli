"""OpenAI-compatible API client over urllib (streaming, signup, usage)."""
from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request

from . import __version__, tls
from .config import Config


class ApiError(Exception):
    """kind: auth | budget | rate | model | context | tools_unsupported |
    not_found | bad_request | server | network"""

    def __init__(self, kind: str, message: str, status: int | None = None,
                 retry_after: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.retry_after = retry_after


def _message_from_body(body: str) -> str:
    try:
        obj = json.loads(body)
    except ValueError:
        return body.strip()[:300]
    if isinstance(obj, dict):
        for key in ("detail", "error", "message"):
            val = obj.get(key)
            if isinstance(val, str):
                return val
            if isinstance(val, dict) and isinstance(val.get("message"), str):
                return val["message"]
            if isinstance(val, list) and val:
                first = val[0]
                if isinstance(first, dict):
                    return str(first.get("msg") or first)
                return str(first)
    return body.strip()[:300]


def find_value(obj, names):
    """Depth-first search for the first key in `names` (case-insensitive)."""
    wanted = {n.lower() for n in names}
    stack = [obj]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if str(k).lower() in wanted and not isinstance(v, (dict, list)):
                    return v
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(v for v in cur if isinstance(v, (dict, list)))
    return None


class Client:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.send_stream_usage = True

    # ---- URLs -----------------------------------------------------------
    def api_root(self) -> str:
        base = self.cfg.base_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return base + "/"

    # ---- low level ------------------------------------------------------
    def _headers(self, key_header: str = "bearer") -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"nukerhugo-code/{__version__}",
        }
        if self.cfg.api_key:
            if key_header == "x-api-key":
                h["X-API-Key"] = self.cfg.api_key
            else:
                h["Authorization"] = f"Bearer {self.cfg.api_key}"
        h.update({str(k): str(v) for k, v in (self.cfg.headers or {}).items()})
        return h

    def _open(self, url, body=None, method=None, key_header="bearer", timeout=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, headers=self._headers(key_header),
            method=method or ("POST" if data is not None else "GET"),
        )
        try:
            return self._urlopen(req, timeout or self.cfg.timeout)
        except urllib.error.HTTPError as e:
            raise self._http_error(e) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise ApiError(
                    "tls",
                    f"The server's TLS certificate could not be verified ({reason.verify_message}). "
                    "Check your PC's date and time. On Windows, expired certificates in the "
                    "system certificate store can cause this; see the README "
                    "('certificate has expired'). Certificate checking is never turned off.",
                ) from None
            raise ApiError(
                "network",
                f"Could not reach {self.cfg.base_url} ({reason}). "
                "Check your connection and base_url.",
            ) from None

    @staticmethod
    def _urlopen(req, timeout):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.URLError as e:
            # HTTPError is a URLError subclass: real HTTP answers must pass through untouched.
            if isinstance(e, urllib.error.HTTPError) or not tls.is_expired_error(e):
                raise
            ctx = tls.fallback_context()  # Windows only; skips expired store certificates
            if ctx is None:
                raise
            return urllib.request.urlopen(req, timeout=timeout, context=ctx)

    def _http_error(self, e: urllib.error.HTTPError) -> ApiError:
        try:
            body = e.read(4096).decode("utf-8", "replace")
        except Exception:
            body = ""
        msg = _message_from_body(body) or e.reason or "request failed"
        low = msg.lower()
        s = e.code
        if s in (401, 403):
            return ApiError("auth", f"API key rejected ({s}). {msg}", s)
        if s == 429:
            ra = e.headers.get("Retry-After") if e.headers else None
            try:
                wait = int(ra) if ra else None
            except ValueError:
                wait = None
            if wait is not None and wait <= 60:
                return ApiError("rate", f"Rate limited, retrying in {wait}s", s, wait)
            return ApiError("budget", msg or "Daily token budget used up", s)
        if s == 404:
            if "model" in low:
                return ApiError("model", msg, s)
            return ApiError("not_found", f"Endpoint not found ({e.geturl()}). {msg}", s)
        if s == 409:
            return ApiError("conflict", msg, s)
        if s in (400, 413, 422):
            if "tool" in low or "function" in low:
                return ApiError("tools_unsupported", msg, s)
            if "context" in low or "too long" in low or "maximum" in low or s == 413:
                return ApiError("context", msg, s)
            return ApiError("bad_request", f"Bad request ({s}): {msg}", s)
        if s == 500:
            return ApiError(
                "server",
                f"Server error (500): {msg}. If the conversation is very long, "
                "try /compact or /clear.", s)
        return ApiError("server", f"Server error ({s}): {msg}", s)

    # ---- chat -----------------------------------------------------------
    def chat_stream(self, messages, tools=None, model=None):
        """Yield ("text"|"reasoning"|"retry", str) events, then ("done", dict)."""
        payload = {
            "model": model or self.cfg.model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        url = self.cfg.base_url + "/chat/completions"
        delays = [1, 3, 8]
        attempt = 0
        while True:
            if self.send_stream_usage:
                payload["stream_options"] = {"include_usage": True}
            else:
                payload.pop("stream_options", None)
            try:
                resp = self._open(url, payload)
            except ApiError as e:
                if e.kind == "bad_request" and "stream_options" in e.message \
                        and self.send_stream_usage:
                    self.send_stream_usage = False
                    continue
                retryable = e.kind in ("rate", "network") or (
                    e.kind == "server" and e.status in (502, 503, 504))
                if retryable and attempt < len(delays):
                    wait = e.retry_after or delays[attempt]
                    attempt += 1
                    yield ("retry", f"{e.message.splitlines()[0]} - retry {attempt}/{len(delays)} in {wait}s")
                    time.sleep(wait)
                    continue
                raise
            break

        text, reasoning, calls = [], [], {}
        usage, finish = None, None
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("error"):
                    raise ApiError("server", _message_from_body(json.dumps(obj)))
                if "token" in obj and isinstance(obj["token"], str):  # Nukerhugo open-API chunks
                    text.append(obj["token"])
                    yield ("text", obj["token"])
                    continue
                if isinstance(obj.get("usage"), dict):
                    usage = obj["usage"]
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    c = d.get("content")
                    if c:
                        text.append(c)
                        yield ("text", c)
                    r = d.get("reasoning_content") or d.get("reasoning")
                    if r:
                        reasoning.append(r)
                        yield ("reasoning", r)
                    for tc in d.get("tool_calls") or []:
                        i = tc.get("index", 0)
                        slot = calls.setdefault(i, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name") and not slot["name"]:
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
        except (socket.timeout, TimeoutError) as e:
            raise ApiError("network", "The response timed out while streaming.") from e
        finally:
            try:
                resp.close()
            except Exception:
                pass

        tool_calls = []
        for n, i in enumerate(sorted(calls)):
            c = calls[i]
            if c["name"]:
                tool_calls.append({
                    "id": c["id"] or f"call_{n}",
                    "name": c["name"],
                    "arguments": c["arguments"] or "{}",
                })
        yield ("done", {
            "content": "".join(text),
            "reasoning": "".join(reasoning),
            "tool_calls": tool_calls,
            "usage": usage,
            "finish_reason": finish,
        })

    # ---- account endpoints (Nukerhugo enterprise API) --------------------
    def _json(self, resp):
        try:
            return json.loads(resp.read().decode("utf-8", "replace"))
        except ValueError:
            return {}
        finally:
            resp.close()

    def signup(self, email: str) -> dict:
        resp = self._open(self.api_root() + "?endpoint=signup", {"email": email}, timeout=60)
        return self._json(resp)

    def usage(self) -> dict:
        resp = self._open(self.api_root() + "?endpoint=usage", key_header="x-api-key", timeout=20)
        return self._json(resp)

    def list_models(self) -> list:
        resp = self._open(self.cfg.base_url + "/models", timeout=20)
        obj = self._json(resp)
        items = obj.get("data") if isinstance(obj, dict) else obj
        out = []
        for it in items or []:
            out.append(it.get("id") if isinstance(it, dict) else str(it))
        return [m for m in out if m]
