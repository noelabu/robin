"""Find and redact secrets in text (PRD §12, TR-070, TR-083).

Rules are gitleaks' default set, vendored as data (gitleaks/), plus the user's own patterns from
`<data dir>/redact.toml` in the same format. Semantics follow gitleaks: a keyword prefilter, the rule's
regex, its secret group (or the first non-empty group), a Shannon-entropy floor, and global and per-rule
allowlists (regexes and stopwords). Path-only rules don't apply to text.

Matches are reported as rule IDs and offsets, never as the matched text, and nothing here logs a match.
"""

import json
import math
import re
import tomllib
import warnings
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from robin.redact.regex import go_to_python

VENDORED = Path(__file__).with_name("gitleaks")
# Robin's own credentials: an agent that echoes its token must not leak it into shared excerpts or logs.
ROBIN_RULES: list[dict[str, Any]] = [
    {
        "id": "robin-agent-credential",
        "regex": r"robin_agent_[A-Za-z0-9_-]{43}",
        "keywords": ["robin_agent_"],
    },
]


@dataclass(frozen=True)
class Allowlist:
    regexes: tuple[re.Pattern[str], ...] = ()
    stopwords: tuple[str, ...] = ()
    target: str = "secret"  # secret | match | line
    all_required: bool = False  # condition = "AND"

    def allows(self, secret: str, match: str, line: str) -> bool:
        text = {"match": match, "line": line}.get(self.target, secret)
        checks = []
        if self.regexes:
            checks.append(any(r.search(text) for r in self.regexes))
        if self.stopwords:
            low = secret.lower()
            checks.append(any(w in low for w in self.stopwords))
        if not checks:
            return False
        return all(checks) if self.all_required else any(checks)


@dataclass(frozen=True)
class Rule:
    id: str
    regex: re.Pattern[str]
    keywords: tuple[str, ...] = ()
    secret_group: int = 0
    entropy: float = 0.0
    allowlists: tuple[Allowlist, ...] = ()
    source: str = "gitleaks"


@dataclass(frozen=True)
class Match:
    """Where a secret is. Deliberately carries no text, so it can be stored, shown and logged."""

    rule_id: str
    start: int
    end: int
    source: str = "gitleaks"


@dataclass
class RuleSet:
    rules: list[Rule]
    allowlist: Allowlist
    version: str
    skipped: list[str] = field(default_factory=list)  # rule IDs that couldn't be used, with no pattern text

    def scan(self, text: str) -> list[Match]:
        low = text.lower()
        found: list[Match] = []
        for rule in self.rules:
            if rule.keywords and not any(k in low for k in rule.keywords):
                continue
            for m in rule.regex.finditer(text):
                start, end = _secret_span(m, rule.secret_group)
                if start == end:
                    continue
                secret = text[start:end]
                if rule.entropy and _entropy(secret) < rule.entropy:
                    continue
                line = _line(text, m.start(), m.end())
                if self.allowlist.allows(secret, m.group(0), line):
                    continue
                if any(a.allows(secret, m.group(0), line) for a in rule.allowlists):
                    continue
                found.append(Match(rule.id, start, end, rule.source))
        return _merge(found)

    def redact(self, text: str) -> tuple[str, list[Match]]:
        """Text with each secret replaced by `[REDACTED:<rule>]`, and where they were (in the original)."""
        matches = self.scan(text)
        out, pos = [], 0
        for m in matches:
            out.append(text[pos : m.start])
            out.append(f"[REDACTED:{m.rule_id}]")
            pos = m.end
        out.append(text[pos:])
        return "".join(out), matches


def _secret_span(m: re.Match[str], group: int) -> tuple[int, int]:
    if group and group <= (m.re.groups or 0) and m.group(group) is not None:
        return m.span(group)
    if not group:
        for g in range(1, (m.re.groups or 0) + 1):  # gitleaks: the first non-empty group
            if m.group(g):
                return m.span(g)
    return m.span(0)


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _line(text: str, start: int, end: int) -> str:
    a = text.rfind("\n", 0, start) + 1
    b = text.find("\n", end)
    return text[a : b if b != -1 else len(text)]


def _generic(m: Match) -> bool:
    return m.rule_id.startswith("generic")


def _merge(matches: list[Match]) -> list[Match]:
    """Overlapping matches become one span, labelled by the most specific rule that found it (a provider's
    own rule over gitleaks' catch-all generic ones)."""
    out: list[Match] = []
    for m in sorted(matches, key=lambda x: (x.start, -x.end, _generic(x))):
        if out and m.start < out[-1].end:
            last = out[-1]
            label = m if _generic(last) and not _generic(m) else last
            out[-1] = Match(label.rule_id, last.start, max(last.end, m.end), label.source)
        else:
            out.append(m)
    return out


def _allowlist(raw: dict[str, Any]) -> Allowlist:
    return Allowlist(
        regexes=tuple(re.compile(go_to_python(r)) for r in raw.get("regexes", [])),
        stopwords=tuple(w.lower() for w in raw.get("stopwords", [])),
        target=raw.get("regexTarget", "secret"),
        all_required=str(raw.get("condition", "OR")).upper() == "AND",
    )


def _rules(raw: list[dict[str, Any]], source: str, skipped: list[str]) -> list[Rule]:
    out = []
    for r in raw:
        if "regex" not in r:  # path-only rules (e.g. a .p12 file) don't apply to text
            continue
        try:
            out.append(
                Rule(
                    id=str(r["id"]),
                    regex=re.compile(go_to_python(r["regex"])),
                    keywords=tuple(k.lower() for k in r.get("keywords", [])),
                    secret_group=int(r.get("secretGroup", 0)),
                    entropy=float(r.get("entropy", 0)),
                    allowlists=tuple(_allowlist(a) for a in r.get("allowlists", [])),
                    source=source,
                )
            )
        except (re.error, KeyError, ValueError, TypeError):
            skipped.append(f"{source}:{r.get('id', '?')}")
    return out


def load(custom: Path | None = None) -> RuleSet:
    """The vendored gitleaks rules, plus custom rules from a TOML file in the same format, if it exists."""
    with warnings.catch_warnings():  # "possible nested set": valid in Go and Python alike
        warnings.simplefilter("ignore", FutureWarning)
        return _load(custom)


def _load(custom: Path | None) -> RuleSet:
    data = tomllib.loads((VENDORED / "gitleaks.toml").read_text())
    version = json.loads((VENDORED / "source.json").read_text())["version"]
    skipped: list[str] = []
    rules = _rules(data["rules"], "gitleaks", skipped) + _rules(ROBIN_RULES, "robin", skipped)
    allowlist = _allowlist(data.get("allowlist", {}))
    if custom is not None and custom.is_file():
        extra = tomllib.loads(custom.read_text())
        rules += _rules(extra.get("rules", []), "custom", skipped)
    return RuleSet(rules=rules, allowlist=allowlist, version=f"gitleaks {version}", skipped=skipped)


@lru_cache(maxsize=4)
def _cached(custom: str | None, mtime: float) -> RuleSet:
    return load(Path(custom) if custom else None)


def for_data_dir(data_dir: Path) -> RuleSet:
    """The rule set for this Robin, reloaded when the custom pattern file changes."""
    custom = data_dir / "redact.toml"
    mtime = custom.stat().st_mtime if custom.exists() else 0.0
    return _cached(str(custom), mtime)
