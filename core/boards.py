"""Article -> Pinterest board (config/pinterest_boards.json)."""

import json
import unicodedata
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "pinterest_boards.json"


def load(path=CONFIG_PATH):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _tokens(handle):
    return [t for t in handle.lower().split("-") if t]


def _has_phrase(tokens, phrase):
    words = phrase.lower().split()
    n = len(words)
    return any(tokens[i:i + n] == words for i in range(len(tokens) - n + 1))


def classify(article_id, handle, cfg=None):
    """Board NAME for an article. Overrides first, then the ordered rules, then the default."""
    cfg = cfg or load()
    override = cfg.get("overrides", {}).get(str(article_id))
    if override:
        return override
    tokens = _tokens(handle)
    for rule in cfg["rules"]:
        if any(_has_phrase(tokens, p) for p in rule["any"]) and not any(
                _has_phrase(tokens, p) for p in rule.get("exclude", [])):
            return rule["board"]
    return cfg["default"]


def validate(cfg=None):
    """Every board a rule/override/default can return must be a declared board."""
    cfg = cfg or load()
    declared = set(cfg["boards"])
    used = {cfg["default"], *(r["board"] for r in cfg["rules"]), *cfg.get("overrides", {}).values()}
    unknown = used - declared
    if unknown:
        raise ValueError(f"Boards used in rules but not declared: {sorted(unknown)}")
    return True


def norm(name):
    """Comparison form of a board name (NFC, trimmed)."""
    return unicodedata.normalize("NFC", name).strip()
