"""The one redaction module (KTD24).

Every path that carries text out of Slipway calls scrub first: the
transcript before push, the trace before export, audit arguments before
write, transition evidence before record, and a finding's excerpt before
the finding is written.

scrub removes two things and nothing more:

1. The exact credential values Slipway injected into a session, which the
   caller passes in, in three forms:
   a. The raw value and its percent encoded form (how a value looks inside
      a URL, each unsafe byte written as % and two hex digits). A match may
      mix raw and encoded characters and write the hex in either case, so
      %2F, %2f, and a bare / all match.
   b. Its base64 form. Base64 writes every 3 bytes as 4 characters, so the
      characters a value becomes depend on where it starts inside the
      encoded text. scrub removes the characters that depend only on the
      value, at each of the three possible starting positions, in the
      standard and the URL safe alphabet. A value encoded inside a longer
      string, such as "x-access-token:<token>", is caught this way, but the
      one or two characters at each edge that also carry a neighbouring
      byte stay.
   c. The whole base64 form, with padding, of the value and of every
      "a:b" pair of two injected values. HTTP Basic authorization sends
      base64(user:secret), and the Jira adapter passes both its email and
      its token, so its header value is removed whole, edges included.
2. Text matching one of TOKEN_PATTERNS, the token shapes the configuration
   validator also refuses in a project file.

A customer secret that matches neither passes through, and so does any other
encoding of an injected value: hex, JSON escapes, base64 wrapped across
lines, or a Basic pair whose user part was not passed in or is shorter than
MIN_INJECTED_LENGTH (the secret's own base64 characters are still removed,
less its edges). That limit is deliberate and is why a project has its trace
detail switch.

refusal_record is how a refused value is kept: the reason, a SHA 256 hash
(a fixed length fingerprint that cannot be turned back into the value), and
the length. Never the value itself.
"""

import base64
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


_URLSAFE = str.maketrans("+/", "-_")


def _percent_pattern(value: str) -> str:
    """A regex matching value with any of its characters percent encoded, hex in either case.

    quote leaves letters, digits, and "_.-~" alone, so those match only
    themselves. Every other character matches itself or its UTF 8 bytes
    written as %XX, each X upper or lower case.
    """
    parts = []
    for char in value:
        encoded = quote(char, safe="")
        if encoded == char:
            parts.append(re.escape(char))
            continue
        hex_digits = encoded.replace("%", "")
        escapes = "".join(
            "%" + "".join(f"[{d.upper()}{d.lower()}]" if d.isalpha() else d
                          for d in hex_digits[i:i + 2])
            for i in range(0, len(hex_digits), 2)
        )
        parts.append(f"(?:{re.escape(char)}|{escapes})")
    return "".join(parts)


def _base64_forms(value: str) -> set[str]:
    """value's whole base64 encoding, and the characters that depend on value alone at each
    of the three byte offsets it can start at, in both alphabets.

    Each base64 character carries 6 bits. With value starting `offset` bytes
    into a 3 byte group, character i depends only on value when its bits,
    6i to 6i + 6, fall inside value's bits, 8 * offset to 8 * (offset + n).
    The filler bytes in front never reach those characters, so their
    content does not matter.
    """
    data = value.encode("utf-8")
    forms = {base64.b64encode(data).decode("ascii")}
    for offset in range(3):
        encoded = base64.b64encode(b"\0" * offset + data).decode("ascii")
        start = -(-8 * offset // 6)  # ceiling division
        end = 8 * (offset + len(data)) // 6
        forms.add(encoded[start:end])
    return forms | {form.translate(_URLSAFE) for form in forms}


def scrub(text: str, injected_values=()) -> str:
    """Return text with every injected value, in the forms the module docstring lists, and
    every known token shape removed."""
    values = list(dict.fromkeys(
        v for v in injected_values if v and len(v) >= MIN_INJECTED_LENGTH))
    literals = set()
    for value in values:
        literals |= _base64_forms(value)
        for other in values:
            if other != value:
                literals |= _base64_forms(f"{value}:{other}")
    patterns = [_percent_pattern(v) for v in values] + [re.escape(f) for f in literals]

    # Find every match in the original text first, then merge spans that
    # overlap or touch, so removing one form never breaks the match of
    # another and a value that contains another is removed whole.
    spans = sorted((m.start(), m.end()) for p in patterns for m in re.finditer(p, text))
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        text = text[:start] + MARK + text[end:]

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
