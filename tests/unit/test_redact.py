"""U6 test scenarios for the redaction module (KTD24)."""

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
