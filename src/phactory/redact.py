"""The one redaction module (KTD24).

Every path that carries text out of Phactory calls scrub first: the
transcript before push, the trace before export, audit arguments before
write, transition evidence before record, and a finding's excerpt before
the finding is written.

scrub removes two things and nothing more:

1. The exact credential values Phactory injected into a session, which the
   caller passes in. Their percent encoded form is removed too, because a
   value placed inside a URL is often encoded.
2. Text matching one of TOKEN_PATTERNS, the token shapes the configuration
   validator also refuses in a project file.

A customer secret that matches neither passes through. That limit is
deliberate and is why a project has its trace detail switch.

refusal_record is how a refused value is kept: the reason, a SHA 256 hash
(a fixed length fingerprint that cannot be turned back into the value), and
the length. Never the value itself.
"""

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import quote

MARK = "[redacted]"

# An injected value shorter than this is not scrubbed by exact match, because
# replacing every "1" or "main" in a transcript would destroy the text and
# protect nothing. Real credentials are far longer.
MIN_INJECTED_LENGTH = 8

# Token shapes, by issuer. Each is a known prefix followed by enough
# characters that ordinary prose never matches.
TOKEN_PATTERNS: dict[str, re.Pattern[str]] = {
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "github_fine_grained_token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    "langfuse_key": re.compile(r"\b[ps]k-lf-[A-Za-z0-9\-]{20,}"),
    "openai_style_key": re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    "atlassian_token": re.compile(r"\bATATT[A-Za-z0-9_\-=]{20,}"),
    "bitbucket_token": re.compile(r"\bAT(?:BB|CTT)[A-Za-z0-9_\-=]{20,}"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "slack_token": re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    "private_key": re.compile(
        r"-{5}BEGIN [A-Z ]*PRIVATE KEY-{5}.*?-{5}END [A-Z ]*PRIVATE KEY-{5}", re.S
    ),
    "url_password": re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
}


def find_token(text: str) -> str | None:
    """Return the name of the first token pattern text matches, or None."""
    for name, pattern in TOKEN_PATTERNS.items():
        if pattern.search(text):
            return name
    return None


def scrub(text: str, injected_values=()) -> str:
    """Return text with every injected value and every known token shape removed."""
    forms = set()
    for value in injected_values:
        if value and len(value) >= MIN_INJECTED_LENGTH:
            forms.add(value)
            forms.add(quote(value, safe=""))
    # Longest first, so a value that contains another is removed whole.
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, MARK)
    for pattern in TOKEN_PATTERNS.values():
        text = pattern.sub(MARK, text)
    return text


@dataclass(frozen=True)
class RefusalRecord:
    """What is kept of a refused value: why, its fingerprint, and its length."""

    reason: str
    sha256: str
    length: int


def refusal_record(value: str, reason: str) -> RefusalRecord:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return RefusalRecord(reason=reason, sha256=digest, length=len(value))
