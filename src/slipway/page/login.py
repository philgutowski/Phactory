"""The operator's login for the status page (U20, KTD13).

The page signs the operator in through the identity provider the operator
file names, with the OAuth authorization code flow and PKCE:

1. The page sends the browser to the provider's authorization endpoint with
   a random state, a nonce, and a PKCE challenge. PKCE (proof key for code
   exchange) is a random secret, the verifier, whose hash goes out first and
   whose plain value goes out only with the code, so a code stolen on the
   way back is useless without it.
2. The provider sends the browser back to /callback with a code. The page
   checks the state against the one it sent, then trades the code and the
   verifier for tokens at the provider's token endpoint.
3. The ID token names the user's OAuth subject (the stable user id the
   provider assigns). The page reads it from the token endpoint's own reply,
   over TLS, so the TLS check stands in for the token's signature, as OpenID
   Connect Core 3.1.3.7 allows; the issuer, audience, expiry, and nonce are
   still checked.

Between requests the page keeps two cookies, each a JSON payload signed
with HMAC (a keyed hash: only the holder of the key can make a valid one):
a short login cookie holding the state, nonce, and verifier while the
browser is at the provider, and the session cookie holding the subject.

The page admits the operator's subject only (app.py checks it on every
request). The shared token verifier the plan names auth.py for is not built
yet; when U18 builds it, the provider calls below can move there.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Protocol

from slipway.config.operator import IdentityProvider

DISCOVERY = "/.well-known/openid-configuration"
TIMEOUT = 10  # seconds for one call to the provider


class LoginFailed(Exception):
    """The provider's answer could not be trusted or read; the message says why."""


def same(a, b) -> bool:
    """A constant time comparison of two values as UTF 8 bytes.

    hmac.compare_digest raises TypeError on a str holding a non ASCII
    character, and any of these values can come from a visitor.
    """
    return hmac.compare_digest(str(a).encode(), str(b).encode())


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def pkce_pair() -> tuple[str, str]:
    """A fresh PKCE verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    return verifier, b64url(hashlib.sha256(verifier.encode()).digest())


class Signer:
    """Signs a JSON payload with an expiry, and reads it back only when the signature holds."""

    def __init__(self, key: bytes, clock: Callable[[], float] = time.time):
        if len(key) < 32:
            raise ValueError("the session key must be at least 32 bytes")
        self.key = key
        self.clock = clock

    def _mac(self, body: str) -> str:
        return b64url(hmac.new(self.key, body.encode(), hashlib.sha256).digest())

    def dumps(self, payload: dict, max_age: int) -> str:
        body = b64url(json.dumps({**payload, "exp": int(self.clock()) + max_age},
                                 separators=(",", ":"), sort_keys=True).encode())
        return f"{body}.{self._mac(body)}"

    def loads(self, token: str | None) -> dict | None:
        """The payload, or None for a missing, forged, malformed, or expired token."""
        if not token or token.count(".") != 1:
            return None
        body, mac = token.split(".")
        if not same(mac, self._mac(body)):
            return None
        try:
            payload = json.loads(unb64url(body))
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("exp", 0) <= self.clock():
            return None
        return payload


class Identity(Protocol):
    """The identity provider as the page uses it: one call out, one call back."""

    def start(self, redirect_uri: str, state: str, challenge: str, nonce: str) -> str:
        """The authorization endpoint URL to send the browser to."""

    def finish(self, code: str, verifier: str, redirect_uri: str, nonce: str) -> str:
        """The signed in user's OAuth subject; raises LoginFailed."""


def fetch_json(url: str, form: dict | None = None) -> dict:
    """GET url, or POST form to it, and return the JSON reply."""
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    request = urllib.request.Request(url, data=data, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
        return json.loads(reply.read())


class OidcIdentity:
    """The identity provider from the operator file, spoken to over OpenID Connect.

    OpenID Connect is the login layer on top of OAuth: the provider publishes
    its endpoints at a discovery address and returns an ID token naming the
    user. client_secret is the value of the operator file's client_secret_env.
    """

    def __init__(self, provider: IdentityProvider, client_secret: str,
                 fetch: Callable[..., dict] = fetch_json,
                 clock: Callable[[], float] = time.time):
        if not provider.issuer.startswith("https://"):
            raise ValueError(f"the identity provider's issuer must be https: {provider.issuer}")
        self.provider = provider
        self.client_secret = client_secret
        self.fetch = fetch
        self.clock = clock
        self._endpoints: dict | None = None

    def endpoints(self) -> dict:
        if self._endpoints is None:
            found = self.fetch(self.provider.issuer.rstrip("/") + DISCOVERY)
            for name in ("authorization_endpoint", "token_endpoint"):
                if not str(found.get(name, "")).startswith("https://"):
                    raise LoginFailed(f"the provider's discovery document has no https {name}")
            self._endpoints = found
        return self._endpoints

    def start(self, redirect_uri: str, state: str, challenge: str, nonce: str) -> str:
        query = urllib.parse.urlencode({
            "response_type": "code", "client_id": self.provider.client_id,
            "redirect_uri": redirect_uri, "scope": "openid", "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        return f"{self.endpoints()['authorization_endpoint']}?{query}"

    def finish(self, code: str, verifier: str, redirect_uri: str, nonce: str) -> str:
        try:
            tokens = self.fetch(self.endpoints()["token_endpoint"], {
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "code_verifier": verifier, "client_id": self.provider.client_id,
                "client_secret": self.client_secret})
        except LoginFailed:
            raise
        except Exception as error:
            raise LoginFailed(f"the token endpoint refused the code: {error}") from error
        return self.subject(tokens.get("id_token"), nonce)

    def subject(self, id_token: str | None, nonce: str) -> str:
        """The subject of an ID token from the token endpoint, after its claims are checked."""
        try:
            claims = json.loads(unb64url(str(id_token).split(".")[1]))
        except (IndexError, ValueError) as error:
            raise LoginFailed("the token endpoint returned no readable ID token") from error
        if not isinstance(claims, dict):
            raise LoginFailed("the ID token's claims are not an object")
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if str(claims.get("iss", "")).rstrip("/") != self.provider.issuer.rstrip("/"):
            raise LoginFailed("the ID token names another issuer")
        if self.provider.client_id not in audiences:
            raise LoginFailed("the ID token is for another client")
        if not isinstance(claims.get("exp"), int | float) or claims["exp"] <= self.clock():
            raise LoginFailed("the ID token has expired")
        if not same(claims.get("nonce", ""), nonce):
            raise LoginFailed("the ID token's nonce is not the one this login sent")
        if not claims.get("sub"):
            raise LoginFailed("the ID token names no subject")
        return str(claims["sub"])


__all__ = ["Identity", "LoginFailed", "OidcIdentity", "Signer", "fetch_json", "pkce_pair",
           "same"]
