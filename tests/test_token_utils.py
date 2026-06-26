"""Tests for ``app.services.token_utils.extract_object_id``.

These tests construct hand-crafted JWTs (header.payload.signature) and feed
them to ``extract_object_id``.  Signatures are bogus — the helper does not
verify signatures; the trust boundary is MSAL.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from app.services.token_utils import (
    extract_expiry,
    extract_first_string_claim,
    extract_object_id,
)


def _b64url_no_pad(data: bytes) -> str:
    """Base64url-encode bytes with the trailing ``=`` padding stripped.

    ADME / Entra access tokens are emitted in this canonical JWT format
    (segments base64url-encoded, no padding).  ``extract_object_id``
    re-pads internally before decoding.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_jwt(payload: dict[str, Any]) -> str:
    """Return a JWT-looking string with the given payload claim set."""
    header = {"alg": "RS256", "typ": "JWT", "kid": "test-key"}
    header_segment = _b64url_no_pad(json.dumps(header).encode("utf-8"))
    payload_segment = _b64url_no_pad(json.dumps(payload).encode("utf-8"))
    signature_segment = _b64url_no_pad(b"not-a-real-signature")
    return f"{header_segment}.{payload_segment}.{signature_segment}"


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_extract_object_id_returns_oid_claim() -> None:
    token = _make_jwt(
        {
            "oid": "00000000-1111-2222-3333-444444444444",
            "tid": "tenant-id",
            "upn": "operator@example.com",
        }
    )

    assert extract_object_id(token) == "00000000-1111-2222-3333-444444444444"


@pytest.mark.parametrize(
    "oid",
    [
        "11111111-2222-3333-4444-555555555555",
        "abcdef01-2345-6789-abcd-ef0123456789",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "12345678-90ab-cdef-1234-567890abcdef",
    ],
)
def test_extract_object_id_returns_realistic_uuids(oid: str) -> None:
    token = _make_jwt({"oid": oid, "aud": "https://energy.azure.com"})

    assert extract_object_id(token) == oid


def test_extract_first_string_claim_returns_first_available_claim() -> None:
    token = _make_jwt({"appid": "app-id", "azp": "authorized-party"})

    assert extract_first_string_claim(token, ("azp", "appid")) == "authorized-party"


def test_extract_first_string_claim_skips_missing_empty_and_non_string() -> None:
    token = _make_jwt({"appid": "", "azp": 12345, "oid": "object-id"})

    assert extract_first_string_claim(token, ("missing", "appid", "azp", "oid")) == (
        "object-id"
    )


def test_extract_first_string_claim_returns_none_for_malformed_token() -> None:
    assert extract_first_string_claim("not-a-jwt", ("appid", "azp")) is None


# ---------------------------------------------------------------------------
# Padding edge cases — payload byte length determines required padding
# ---------------------------------------------------------------------------


def test_extract_object_id_handles_zero_padding() -> None:
    """Payload whose byte length is divisible by 3 needs 0 ``=`` to decode."""
    payload = {"oid": "ab"}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    assert len(raw) % 3 == 0  # safety: confirm we actually exercise 0 padding

    token = _make_jwt(payload)

    assert extract_object_id(token) == "ab"


def test_extract_object_id_handles_one_padding_char() -> None:
    """Payload whose JSON length % 3 == 2 requires 1 ``=`` after base64url."""
    payload = {"oid": "abcd"}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    assert len(raw) % 3 == 2

    token = _make_jwt(payload)

    assert extract_object_id(token) == "abcd"


def test_extract_object_id_handles_two_padding_chars() -> None:
    """Payload whose JSON length % 3 == 1 requires 2 ``=`` after base64url."""
    payload = {"oid": "abc"}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    assert len(raw) % 3 == 1

    token = _make_jwt(payload)

    assert extract_object_id(token) == "abc"


# ---------------------------------------------------------------------------
# Missing / empty inputs
# ---------------------------------------------------------------------------


def test_extract_object_id_returns_none_when_oid_claim_missing() -> None:
    token = _make_jwt({"tid": "tenant", "upn": "operator@example.com"})

    assert extract_object_id(token) is None


def test_extract_object_id_returns_none_for_empty_string() -> None:
    assert extract_object_id("") is None


def test_extract_object_id_returns_none_for_single_segment() -> None:
    """A bare segment with no dots cannot be a JWT."""
    assert extract_object_id("not-a-jwt-just-one-segment") is None


# ---------------------------------------------------------------------------
# Malformed segments
# ---------------------------------------------------------------------------


def test_extract_object_id_returns_none_for_invalid_base64_payload() -> None:
    """The middle segment is not a valid base64url string."""
    token = "header.@@@not-base64@@@.signature"

    assert extract_object_id(token) is None


def test_extract_object_id_returns_none_for_non_json_payload() -> None:
    """Middle segment decodes to bytes that are not valid JSON."""
    payload_segment = _b64url_no_pad(b"this is not json at all")
    token = f"header.{payload_segment}.signature"

    assert extract_object_id(token) is None


def test_extract_object_id_returns_none_for_non_dict_json_payload() -> None:
    """Payload that decodes to a JSON value that isn't an object."""
    payload_segment = _b64url_no_pad(b"[1, 2, 3]")
    token = f"header.{payload_segment}.signature"

    assert extract_object_id(token) is None


# ---------------------------------------------------------------------------
# Non-string oid claim — current behaviour is to filter to None
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "non_string_oid",
    [12345, 3.14, True, None, ["a", "b"], {"nested": "x"}],
)
def test_extract_object_id_returns_none_for_non_string_oid_claim(
    non_string_oid: Any,
) -> None:
    """Document the helper's filter: only str-typed, non-empty ``oid`` wins.

    The contract is "return the oid claim or None"; for safety against
    surprising downstream usage (URL building, history labels), the helper
    deliberately returns None when the claim is present but not a string.
    """
    token = _make_jwt({"oid": non_string_oid})

    assert extract_object_id(token) is None


def test_extract_object_id_returns_none_for_empty_string_oid_claim() -> None:
    """An ``oid`` claim that is the empty string is also treated as missing."""
    token = _make_jwt({"oid": ""})

    assert extract_object_id(token) is None


# ---------------------------------------------------------------------------
# extract_expiry
# ---------------------------------------------------------------------------


def test_extract_expiry_returns_exp_claim() -> None:
    token = _make_jwt({"oid": "x", "exp": 1_782_506_941})

    assert extract_expiry(token) == 1_782_506_941


def test_extract_expiry_coerces_float_exp_to_int() -> None:
    token = _make_jwt({"exp": 1_782_506_941.0})

    assert extract_expiry(token) == 1_782_506_941


def test_extract_expiry_returns_none_when_exp_missing() -> None:
    token = _make_jwt({"oid": "x"})

    assert extract_expiry(token) is None


@pytest.mark.parametrize("non_numeric_exp", [True, False, "soon", None, [1], {"a": 1}])
def test_extract_expiry_returns_none_for_non_numeric_exp(
    non_numeric_exp: Any,
) -> None:
    token = _make_jwt({"exp": non_numeric_exp})

    assert extract_expiry(token) is None


def test_extract_expiry_returns_none_for_malformed_token() -> None:
    assert extract_expiry("") is None
    assert extract_expiry("only-one-segment") is None
    assert extract_expiry("header.@@@not-base64@@@.sig") is None
