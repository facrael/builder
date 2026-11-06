#!/usr/bin/env python3
"""OpenAI-compatible bridge for the construction bot.

Listens on 0.0.0.0:8000, accepts POST /v1/chat/completions and forwards the
request to OpenRouter using the server's OPENROUTER_API_KEY. Model defaults
to z-ai/glm-5.3-flash unless the caller specifies another OpenRouter model.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request

ENV_PATH = "/root/.hermes/.env"
DEFAULT_MODEL = os.getenv("BRIDGE_MODEL", "z-ai/glm-5.3-flash")
UPSTREAM = "https://openrouter.ai/api/v1/chat/completions"


def load_key() -> str:
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("OPENROUTER_API_KEY not found in /root/.hermes/.env")


API_KEY = load_key()


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def do_GET(self):
        if self.path.rstrip("/") in ("/v1/models", "/health"):
            body = json.dumps({
                "object": "list",
                "data": [{"id": DEFAULT_MODEL, "object": "model"}],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if "/v1/chat/completions" not in self.path:
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            # Honor explicit OpenRouter models (e.g. vision models contain "/"):
            # they pass through. Bare placeholder names like "hermes-agent" are
            # replaced with the server default, OpenRouter rejects them.
            requested = str(payload.get("model") or "")
            if "/" not in requested:
                payload["model"] = DEFAULT_MODEL
            payload.pop("stream_options", None)
            data = json.dumps(payload).encode()
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
                self._cors()
                self.end_headers()
                self.wfile.write(out)
        except urllib.error.HTTPError as e:
            err = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(err)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, fmt, *args):
        print("[bridge]", fmt % args, flush=True)


if __name__ == "__main__":
    port = int(os.getenv("BRIDGE_PORT", "8000"))
    print(f"LLM bridge listening on 0.0.0.0:{port} -> OpenRouter ({DEFAULT_MODEL})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
