"""Whether a converted session fits the target model, from cheap, local evidence.

Tokens are estimated at four bytes each, which overestimates for non-English text (the safe direction).
A session may use at most 60% of the target's context window, leaving room for the runtime's own prompt,
tools and the work ahead. The window comes from the runtime's own configuration when it says, else a
conservative default.
"""

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BYTES_PER_TOKEN = 4
SHARE = 0.6
DEFAULT_WINDOW = {"claude": 200_000, "codex": 272_000, "pi": 200_000}


@dataclass(frozen=True)
class Window:
    model: str | None
    tokens: int
    source: str  # where the number came from, shown in the preview


def estimate(data: bytes) -> int:
    return len(data) // BYTES_PER_TOKEN


def limit(window: Window) -> int:
    return int(window.tokens * SHARE)


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def codex_window(home: Path) -> Window:
    try:
        config = tomllib.loads((home / ".codex" / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        config = {}
    model = config.get("model") if isinstance(config.get("model"), str) else None
    if isinstance(config.get("model_context_window"), int):
        return Window(model, config["model_context_window"], "config.toml")
    cache = _json(home / ".codex" / "models_cache.json")
    models = cache.get("models") if isinstance(cache, dict) else None
    for m in models if isinstance(models, list) else []:
        if isinstance(m, dict) and m.get("slug") == model and isinstance(m.get("context_window"), int):
            return Window(model, m["context_window"], "models_cache.json")
    return Window(model, DEFAULT_WINDOW["codex"], "default")


def claude_window(home: Path) -> Window:
    settings = _json(home / ".claude" / "settings.json")
    model = (
        settings.get("model")
        if isinstance(settings, dict) and isinstance(settings.get("model"), str)
        else None
    )
    if model and re.search(r"\[1m\]$", model):
        return Window(model, 1_000_000, "settings.json")
    return Window(model, DEFAULT_WINDOW["claude"], "default")


def pi_window(home: Path) -> Window:
    settings = _json(home / ".pi" / "agent" / "settings.json")
    settings = settings if isinstance(settings, dict) else {}
    provider, model = settings.get("defaultProvider"), settings.get("defaultModel")
    store = _json(home / ".pi" / "agent" / "models-store.json")
    entry = store.get(provider) if isinstance(store, dict) and isinstance(provider, str) else None
    for m in entry.get("models", []) if isinstance(entry, dict) else []:
        if isinstance(m, dict) and m.get("id") == model and isinstance(m.get("contextWindow"), int):
            return Window(model, m["contextWindow"], "models-store.json")
    return Window(model if isinstance(model, str) else None, DEFAULT_WINDOW["pi"], "default")


WINDOWS = {"claude": claude_window, "codex": codex_window, "pi": pi_window}


def window(runtime: str, home: Path | None = None) -> Window:
    return WINDOWS[runtime](home or Path.home())
