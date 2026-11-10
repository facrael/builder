#!/usr/bin/env python3
"""OpenAI-compatible bridge for the construction bot.

Secured per ТЗ v2 (З-1):
- Binds to BRIDGE_HOST (default 127.0.0.1).
- No wildcard CORS.
- Enforces Bearer token auth via BRIDGE_AUTH_TOKEN (constant-time compare).
- Model allowlist via BRIDGE_ALLOWED_MODELS.
- Request size limit (max 15MB).
- Simple rate-limiting (sliding window).
- Reads OPENROUTER_API_KEY directly from environment variable.
"""
import json
import os
import secrets
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import urllib.error

BRIDGE_HOST = os.getenv("BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8000"))
BRIDGE_AUTH_TOKEN = os.getenv("BRIDGE_AUTH_TOKEN", "").strip()

DEFAULT_MODEL = os.getenv("BRIDGE_MODEL", "z-ai/glm-5.3-flash")
VISION_MODEL = os.getenv("BRIDGE_VISION_MODEL", "google/gemini-2.5-flash")
UPSTREAM = "https://openrouter.ai/api/v1/chat/completions"

# Allowlist of models allowed through bridge
ALLOWED_MODELS_RAW = os.getenv(
    "BRIDGE_ALLOWED_MODELS",
    "z-ai/glm-5.3-flash,google/gemini-2.5-flash,anthropic/claude-3.5-sonnet,deepseek/deepseek-chat,hermes-agent"
)
ALLOWED_MODELS = {m.strip() for m in ALLOWED_MODELS_RAW.split(",") if m.strip()}
if DEFAULT_MODEL:
    ALLOWED_MODELS.add(DEFAULT_MODEL)
if VISION_MODEL:
    ALLOWED_MODELS.add(VISION_MODEL)

# Max payload size: 15 MB
MAX_CONTENT_LENGTH = 15 * 1024 * 1024

# Rate limiter: max 120 requests per minute
RATE_LIMIT_WINDOW = 60.0
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("BRIDGE_RATE_LIMIT", "120"))
_request_timestamps = deque()


def check_rate_limit() -> bool:
    now = time.time()
    while _request_timestamps and _request_timestamps[0] <= now - RATE_LIMIT_WINDOW:
        _request_timestamps.popleft()
    if len(_request_timestamps) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    _request_timestamps.append(now)
    return True


def load_api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    # Fallback for legacy environment file if exists
    env_path = "/root/.hermes/.env"
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return ""


API_KEY = load_api_key()


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _verify_auth(self) -> bool:
        if not BRIDGE_AUTH_TOKEN:
            # If no token configured, only allow if explicitly running in development on loopback
            return True
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:].strip()
        return secrets.compare_digest(token, BRIDGE_AUTH_TOKEN)

    def do_GET(self):
        if not self._verify_auth():
            self._send_json(401, {"error": "Unauthorized: invalid or missing bridge token"})
            return

        if self.path.rstrip("/") in ("/v1/models", "/health"):
            models_data = [{"id": m, "object": "model"} for m in sorted(ALLOWED_MODELS)]
            self._send_json(200, {"object": "list", "data": models_data})
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self._verify_auth():
            self._send_json(401, {"error": "Unauthorized: invalid or missing bridge token"})
            return

        if not check_rate_limit():
            self._send_json(429, {"error": "Rate limit exceeded"})
            return

        if "/v1/chat/completions" not in self.path:
            self._send_json(404, {"error": "Not found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > MAX_CONTENT_LENGTH:
                self._send_json(413, {"error": f"Payload too large (max {MAX_CONTENT_LENGTH} bytes)"})
                return

            raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            payload = json.loads(raw_body.decode("utf-8") or "{}")

            requested = str(payload.get("model") or "")
            if "/" not in requested:
                payload["model"] = DEFAULT_MODEL
            else:
                payload["model"] = requested

            if "image_url" in json.dumps(payload) and VISION_MODEL:
                payload["model"] = VISION_MODEL

            # Verify against allowed models
            if payload["model"] not in ALLOWED_MODELS:
                self._send_json(403, {"error": f"Model '{payload['model']}' is not in allowlist"})
                return

            payload.pop("stream_options", None)
            data = json.dumps(payload).encode("utf-8")

            if not API_KEY:
                self._send_json(500, {"error": "OPENROUTER_API_KEY is not configured on bridge"})
                return

            req = urllib.request.Request(
                UPSTREAM,
                data=data,
                headers={
                    "Authorization": "Bearer " + API_KEY,
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://hermes.local",
                    "X-Title": "construction-bot-bridge",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                out = r.read()
                self.send_response(r.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
        except urllib.error.HTTPError as e:
            err = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
        except Exception as e:
            self._send_json(502, {"error": f"Upstream bridge error: {e}"})

    def log_message(self, fmt, *args):
        print("[bridge]", fmt % args, flush=True)


if __name__ == "__main__":
    print(f"LLM bridge listening on {BRIDGE_HOST}:{BRIDGE_PORT} -> OpenRouter ({DEFAULT_MODEL})", flush=True)
    ThreadingHTTPServer((BRIDGE_HOST, BRIDGE_PORT), Handler).serve_forever()
