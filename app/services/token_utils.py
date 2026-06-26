"""JWT inspection helpers for ADME access tokens.

This module reads claims out of access tokens that *we* just received
from MSAL.  It is **not** a security boundary: there is no signature
verification, no issuer/audience check, and no expiry enforcement.
The trust boundary is MSAL itself — by the time a token reaches this
helper, it has already been issued to us and we are simply reading our
own ``oid`` claim to discover our own Entra Object ID so we can call
ADME's per-user entitlements endpoint.

Stdlib only (``base64``, ``json``).  Never use these helpers to validate
tokens received from anywhere else.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence


def extract_object_id(token: str) -> str | None:
    """Return the ``oid`` claim from a JWT, or ``None`` on any failure.

    Splits the token on ``.``, base64url-decodes the payload segment
    (padding it with ``=`` until length % 4 == 0), parses the JSON, and
    returns ``payload.get("oid")``.  Any malformed input — empty token,
    wrong number of segments, bad base64, non-UTF-8 bytes, non-JSON
    payload, missing ``oid`` claim — yields ``None`` rather than raising.
    """
    return extract_first_string_claim(token, ("oid",))


def extract_first_string_claim(
    token: str,
    claim_names: Sequence[str],
) -> str | None:
    """Return the first non-empty string claim from ``claim_names``.

    The JWT payload is decoded without validation for local inspection only;
    see the module docstring for the trust boundary.
    """
    payload = _decode_payload(token)
    if payload is None:
        return None
    for claim_name in claim_names:
        value = payload.get(claim_name)
        if isinstance(value, str) and value:
            return value
    return None


def extract_expiry(token: str) -> int | None:
    """Return the ``exp`` claim (Unix seconds) from a JWT, or ``None``.

    The JWT payload is decoded without validation for local inspection only;
    see the module docstring for the trust boundary. Any malformed input or a
    missing/non-numeric ``exp`` claim yields ``None`` rather than raising.
    """
    payload = _decode_payload(token)
    if payload is None:
        return None
    value = payload.get("exp")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _decode_payload(token: str) -> dict[str, object] | None:
    if not token:
        return None
    try:
        segments = token.split(".")
        if len(segments) < 2:
            return None
        payload_segment = segments[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        decoded_bytes = base64.urlsafe_b64decode(padded)
        payload = json.loads(decoded_bytes.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError, IndexError):
        return None
    if isinstance(payload, dict):
        return payload
    return None
