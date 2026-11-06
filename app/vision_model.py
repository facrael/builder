"""Автоподбор vision-модели (как auxiliary.vision: auto в Hermes).

BRIDGE_VISION_MODEL=auto -> спрашиваем у OpenRouter список моделей,
выбираем самую дешёвую с входом text+image и кэшируем на 24 часа.
BRIDGE_VISION_MODEL=<имя> -> используем как есть.
"""
import os
import json
import time
import urllib.request

_cache = {"model": None, "ts": 0.0}
_TTL = 24 * 3600
DEFAULT_VISION_MODEL = "google/gemini-2.5-flash-lite"


def get_vision_model() -> str:
    setting = (os.environ.get("BRIDGE_VISION_MODEL") or "").strip().lower()
    if setting not in ("", "auto"):
        return setting

    if _cache["model"] and (time.time() - _cache["ts"]) < _TTL:
        return _cache["model"]

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return DEFAULT_VISION_MODEL
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))["data"]

        def price_usd_per_m(p):
            try:
                return float(p.get("prompt") or 1e9) * 1e6 + float(p.get("completion") or 1e9) * 1e6
            except (TypeError, ValueError):
                return 1e18

        candidates = []
        for m in data:
            arch = m.get("architecture") or {}
            inp = arch.get("input") or []
            if "image" not in inp:
                continue
            candidates.append((price_usd_per_m(m.get("pricing") or {}), m["id"]))
        candidates.sort()
        if candidates:
            _cache["model"] = candidates[0][1]
            _cache["ts"] = time.time()
            print(f"[vision_model] auto-selected: {candidates[0][1]}", flush=True)
            return _cache["model"]
    except Exception as e:
        print(f"[vision_model] auto-select failed: {e}", flush=True)

    return DEFAULT_VISION_MODEL
