"""U6 test scenarios for the redaction module (KTD24)."""

import base64
import dataclasses

import pytest

from slipway.redact import MARK, TOKEN_PATTERNS, find_token, refusal_record, scrub

INJECTED = "s3cr3t/value+with=symbols"


def test_scrub_removes_an_injected_value_inside_a_url():
    text = f"git push https://x-access:{INJECTED}@git.example.test/org/repo.git main"
    out = scrub(text, [INJECTED])
    assert INJECTED not in out
    assert out.startswith("git push https://")
    assert out.endswith("@git.example.test/org/repo.git main")


def test_scrub_removes_the_percent_encoded_form_inside_a_url():
    text = "GET https://api.example.test/?key=s3cr3t%2Fvalue%2Bwith%3Dsymbols&page=2"
    out = scrub(text, [INJECTED])
    assert "s3cr3t" not in out
    assert out.endswith("&page=2")


def test_scrub_removes_the_lowercase_percent_encoded_form():
    text = "GET https://api.example.test/?key=s3cr3t%2fvalue%2bwith%3dsymbols&page=2"
    out = scrub(text, [INJECTED])
    assert "s3cr3t" not in out
    assert out == f"GET https://api.example.test/?key={MARK}&page=2"


def test_scrub_removes_a_mixed_raw_and_encoded_form_in_either_case():
    # quote(safe="/") leaves the slash raw; hex case differs between escapes.
    text = "path s3cr3t/value%2bwith%3Dsymbols done"
    assert scrub(text, [INJECTED]) == f"path {MARK} done"


@pytest.mark.parametrize("lead", [b"", b"u", b"us", b"user:"])
def test_scrub_removes_the_base64_form_at_every_byte_offset(lead):
    # Base64 maps 3 bytes to 4 characters, so the value encodes differently
    # depending on how many bytes come before it. Each lead covers an offset.
    trail = b"@host"
    encoded = base64.b64encode(lead + INJECTED.encode() + trail).decode()
    out = scrub(f"header {encoded} end", [INJECTED])
    assert out.count(MARK) == 1
    left = out.index(MARK) - len("header ")
    right = len(out) - out.index(MARK) - len(MARK) - len(" end")
    # What stays is only the characters that carry a bit of lead or trail
    # (6 bits each, one of them shared with the value), plus "=" padding.
    assert left <= -(-8 * len(lead) // 6)
    assert right <= -(-8 * len(trail) // 6) + 1 + 2


def test_scrub_removes_the_url_safe_base64_form():
    encoded = base64.urlsafe_b64encode(b"\xff\xfe" + INJECTED.encode()).decode()
    assert "-" in encoded or "_" in encoded
    out = scrub(f"cursor={encoded}", [INJECTED])
    assert out.startswith("cursor=") and MARK in out
    assert len(out) <= len("cursor=") + 3 + len(MARK) + 1 + 2


def test_scrub_removes_a_jira_basic_authorization_header_whole():
    # Built the way trackers/jira.py builds it, scrubbed with what it passes.
    email, token = "builder@example.test", "ATAT-not-a-real-token-0123456789"
    header = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
    text = f"request failed, Authorization: {header}"
    assert scrub(text, (token, email)) == f"request failed, Authorization: Basic {MARK}"
    assert scrub(text, (email, token)) == f"request failed, Authorization: Basic {MARK}"


def test_scrub_removes_two_partly_overlapping_values_whole():
    out = scrub("key=abcdefgh12345678wxyz end", ["abcdefgh12345678", "12345678wxyz"])
    assert out == f"key={MARK} end"


def test_scrub_removes_an_injected_value_inside_a_command_line():
    text = f"curl -H 'Authorization: Bearer {INJECTED}' https://api.example.test"
    out = scrub(text, [INJECTED])
    assert INJECTED not in out
    assert out == f"curl -H 'Authorization: Bearer {MARK}' https://api.example.test"


def test_scrub_leaves_unrelated_text_intact():
    text = "ran 42 tests in 3.1s on main at 2eb5ee4, all green"
    assert scrub(text, [INJECTED]) == text


def test_scrub_ignores_very_short_injected_values():
    text = "branch main merged"
    assert scrub(text, ["main", ""]) == text


def test_scrub_removes_the_longer_of_two_overlapping_values_whole():
    out = scrub("token=abcdefgh12345678", ["abcdefgh", "abcdefgh12345678"])
    assert out == f"token={MARK}"


@pytest.mark.parametrize("sample", [
    "ghp_" + "A1b2C3d4" * 5,
    "github_pat_" + "A1b2C3d4_" * 5,
    "sk-ant-api03-" + "Zz9" * 10,
    "sk-lf-" + "abc123" * 5,
    "xoxb-1234567890-abcdefghij",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlX2hlcmU",
])
def test_scrub_removes_known_token_patterns(sample):
    out = scrub(f"log line: {sample} end", [])
    assert sample not in out
    assert out == f"log line: {MARK} end"


def test_scrub_removes_a_password_in_a_url_without_being_told_it():
    out = scrub("cloning https://bot:hunter2hunter2@git.example.test/r.git", [])
    assert "hunter2" not in out
    assert "@git.example.test/r.git" in out


def test_every_pattern_has_a_name_find_token_reports():
    assert find_token("nothing secret here") is None
    assert find_token("ghp_" + "x" * 36) == "github_token"
    assert set(TOKEN_PATTERNS) >= {"github_token", "anthropic_key", "jwt"}


def test_refusal_record_never_returns_the_value():
    value = "the refused answer 12345"
    record = refusal_record(value, "looks like a secret")
    assert value not in repr(record)
    assert all(value not in str(v) for v in dataclasses.asdict(record).values())
    assert record.reason == "looks like a secret"
    assert record.length == len(value)


def test_refusal_record_hash_is_stable_and_distinct():
    first = refusal_record("same value", "r")
    assert first.sha256 == refusal_record("same value", "other reason").sha256
    assert first.sha256 != refusal_record("same valuf", "r").sha256
    assert len(first.sha256) == 64
