"""Secret scanning and redaction (TR-070, TR-083).

Fake secrets are assembled at run time from pieces and random characters, so nothing in this file looks like
a credential to a scanner (or to a push-protection check).
"""

import logging
import random
import re
import string

import pytest

from robin.redact.regex import go_to_python
from robin.redact.scanner import Match, load

rng = random.Random(7)
ALNUM = string.ascii_letters + string.digits
KEY_HEADER = "-----BEGIN " + "RSA PRIVATE " + "KEY-----"
KEY_FOOTER = "-----END " + "RSA PRIVATE " + "KEY-----"
ASSIGN = "api" + "_key" + " = "


def rand(alphabet: str, n: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


@pytest.fixture(scope="module")
def rules():
    return load()


def test_vendored_rules_all_load(rules):
    assert rules.version == "gitleaks v8.30.1" and rules.skipped == [] and len(rules.rules) > 200


@pytest.mark.parametrize(
    "secret, rule",
    [
        ("gh" + "p_" + rand(ALNUM, 36), "github-pat"),
        ("AK" + "IA" + rand(string.ascii_uppercase + "234567", 16), "aws-access-token"),
        (
            "xo" + "xb-" + rand(string.digits, 12) + "-" + rand(string.digits, 12) + "-" + rand(ALNUM, 24),
            "slack-bot-token",
        ),
    ],
    ids=["github", "aws", "slack"],  # never print the fake tokens
)
def test_known_token_formats_are_found_and_redacted(rules, secret, rule):
    text = f"config:\n  token: {secret}\n  other: fine\n"
    redacted, matches = rules.redact(text)
    assert [m.rule_id for m in matches] == [rule]
    assert secret not in redacted and f"[REDACTED:{rule}]" in redacted and "other: fine" in redacted
    assert text[matches[0].start : matches[0].end] == secret


def test_private_keys_and_generic_assignments(rules):
    body = "\n".join(rand(ALNUM + "+/", 64) for _ in range(6))
    redacted, matches = rules.redact(f"here:\n{KEY_HEADER}\n{body}\n{KEY_FOOTER}\nafter")
    assert [m.rule_id for m in matches] == ["private-key"] and body.splitlines()[0] not in redacted
    _, matches = rules.redact(f'{ASSIGN}"{rand(ALNUM, 32)}"')
    assert matches and matches[0].rule_id == "generic-api-key"


def test_allowlisted_examples_and_low_entropy_values_pass(rules):
    documented_example = "AK" + "IA" + "IOSFODNN7" + "EXAMPLE"
    assert rules.scan(f"aws_key = {documented_example}") == []
    assert rules.scan(f'{ASSIGN}"{"a" * 32}"') == []
    assert rules.scan("nothing secret here, just prose about tokens and keys") == []


def test_custom_patterns_from_the_config_file(tmp_path):
    custom = tmp_path / "redact.toml"
    custom.write_text(
        '[[rules]]\nid = "acme-ticket-secret"\n'
        "regex = '''ACME-(?i)sec-[0-9]{6}'''\n"
        'keywords = ["acme-"]\n'
    )
    rs = load(custom)
    redacted, matches = rs.redact("see ACME-SEC-123456 and ACME-sec-654321")
    assert [(m.rule_id, m.source) for m in matches] == [("acme-ticket-secret", "custom")] * 2
    assert "123456" not in redacted


def test_matches_never_carry_or_log_the_secret(rules, caplog):
    secret = "gh" + "p_" + rand(ALNUM, 36)
    with caplog.at_level(logging.DEBUG):
        matches = rules.scan(f"token {secret}")
    assert matches and secret not in repr(matches) and secret not in caplog.text
    assert set(Match.__dataclass_fields__) == {"rule_id", "start", "end", "source"}


def test_go_inline_flags_are_scoped_like_go():
    assert re.fullmatch(go_to_python("ab(?i)cd"), "abCD")
    assert not re.fullmatch(go_to_python("ab(?i)cd"), "ABcd")
    p = go_to_python("(x(?i)y)z")
    assert re.fullmatch(p, "xYz") and not re.fullmatch(p, "xyZ")
    assert go_to_python("(?i)abc") == "(?i)abc"
    assert re.search(go_to_python(r"abc\z"), "abc") and not re.search(go_to_python(r"abc\z"), "abc\n")
    assert go_to_python("[(?i)]") == "[(?i)]"  # inside a class it's literal


def test_inline_flags_carry_across_alternation_like_go():
    p = go_to_python("(?:a(?i)b|c)")  # Go: the flag lasts to the group's end, through the |
    assert re.fullmatch(p, "aB") and re.fullmatch(p, "C") and not re.fullmatch(p, "AB")
    q = go_to_python("x(?i)y|z")
    assert re.fullmatch(q, "xY") and re.fullmatch(q, "Z") and not re.fullmatch(q, "XY")
    r = go_to_python("(?:\"(?i)ab\"|'(?i)ab')")
    assert re.fullmatch(r, '"AB"') and re.fullmatch(r, "'Ab'") and not re.fullmatch(r, "\"ab'")


def test_single_and_double_quoted_curl_auth_headers_are_both_found(rules):
    token = "Bearer " + rand(ALNUM, 24)
    for quote in ('"', "'"):
        cmd = f"curl https://api.example.org -H {quote}Authorization: {token}{quote}"
        assert [m.rule_id for m in rules.scan(cmd)] == ["curl-auth-header"], quote


def test_robins_own_agent_credentials_are_redacted(store, session_id):
    from robin import retrieval

    token = retrieval.mint(store, session_id)
    clean, matches = load().redact(f"ROBIN_AGENT_TOKEN={token}\n")
    assert token not in clean and [m.rule_id for m in matches] == ["robin-agent-credential"]
