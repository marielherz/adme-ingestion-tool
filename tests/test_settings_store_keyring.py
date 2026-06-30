"""Keyring integration tests for ``app.services.settings_store``.

These tests NEVER touch the real OS credential store.  A fixture installs
a fake ``keyring`` module into ``sys.modules`` so the lazy ``import keyring``
calls inside ``_store_secret`` / ``_load_secret`` resolve to an in-memory
stub whose calls we can assert against.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.models.connection import ADMEConnection, AuthMethod
from app.services.settings_store import (
    KEYRING_SERVICE_NAME,
    SettingsStoreError,
    _load_secret,
    clear_user_token,
    delete_connection,
    list_connections,
    load_connection,
    load_user_token,
    save_connection,
    save_user_token,
)


class _FakeKeyring:
    """In-memory stand-in for the ``keyring`` package."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.set_password = MagicMock(side_effect=self._set)
        self.get_password = MagicMock(side_effect=self._get)
        self.delete_password = MagicMock(side_effect=self._delete)

    def _set(self, service: str, name: str, secret: str) -> None:
        self.store[(service, name)] = secret

    def _get(self, service: str, name: str) -> str | None:
        return self.store.get((service, name))

    def _delete(self, service: str, name: str) -> None:
        try:
            del self.store[(service, name)]
        except KeyError as exc:
            from keyring.errors import PasswordDeleteError

            raise PasswordDeleteError("no such password") from exc


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    """Install a fake ``keyring`` module so tests never touch the OS vault."""
    fake = _FakeKeyring()

    fake_module = types.ModuleType("keyring")
    fake_module.set_password = fake.set_password  # type: ignore[attr-defined]
    fake_module.get_password = fake.get_password  # type: ignore[attr-defined]
    fake_module.delete_password = fake.delete_password  # type: ignore[attr-defined]

    errors_module = types.ModuleType("keyring.errors")

    class PasswordDeleteError(Exception):
        pass

    errors_module.PasswordDeleteError = PasswordDeleteError  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "keyring", fake_module)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors_module)
    return fake


def _service_principal_connection(secret: str = "super-secret") -> ADMEConnection:
    return ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
        auth_method=AuthMethod.SERVICE_PRINCIPAL,
        client_secret=secret,
    )


def test_user_token_roundtrip_persists_via_keyring(
    fake_keyring: _FakeKeyring,
) -> None:
    save_user_token("aa.bb.cc", 1700000000, name="default")

    assert load_user_token(name="default") == ("aa.bb.cc", 1700000000)

    clear_user_token(name="default")
    assert load_user_token(name="default") is None


def test_user_token_load_returns_none_when_absent(
    fake_keyring: _FakeKeyring,
) -> None:
    assert load_user_token(name="missing") is None


def test_user_token_load_handles_malformed_payload(
    fake_keyring: _FakeKeyring,
) -> None:
    fake_keyring.store[(KEYRING_SERVICE_NAME, "default::user-token::count")] = (
        "1"
    )
    fake_keyring.store[(KEYRING_SERVICE_NAME, "default::user-token::0")] = (
        "not-json"
    )

    assert load_user_token(name="default") is None


def test_user_token_persists_none_expiry(
    fake_keyring: _FakeKeyring,
) -> None:
    save_user_token("aa.bb.cc", None, name="default")

    assert load_user_token(name="default") == ("aa.bb.cc", None)


def test_user_token_roundtrips_large_token_via_chunks(
    fake_keyring: _FakeKeyring,
) -> None:
    # A ~3 KB token exceeds the Windows credential blob limit; it must be
    # chunked across multiple entries and reassembled intact.
    big_token = "h." + ("x" * 3000) + ".sig"

    save_user_token(big_token, 1700000000, name="default")

    # Stored as multiple chunk entries plus a count entry.
    count_raw = fake_keyring.store[
        (KEYRING_SERVICE_NAME, "default::user-token::count")
    ]
    assert int(count_raw) >= 3
    assert load_user_token(name="default") == (big_token, 1700000000)

    clear_user_token(name="default")
    assert load_user_token(name="default") is None


def test_save_connection_writes_secret_to_keyring(
    fake_keyring: _FakeKeyring,
) -> None:
    conn = _service_principal_connection("rotate-me")
    save_connection("primary", conn)

    fake_keyring.set_password.assert_called_once_with(
        KEYRING_SERVICE_NAME, "primary", "rotate-me"
    )
    assert fake_keyring.store[(KEYRING_SERVICE_NAME, "primary")] == "rotate-me"


def test_load_connection_hydrates_secret_from_keyring(
    fake_keyring: _FakeKeyring,
) -> None:
    save_connection("primary", _service_principal_connection("rotate-me"))
    fake_keyring.set_password.reset_mock()

    loaded = load_connection("primary")

    assert loaded is not None
    assert loaded.client_secret == "rotate-me"
    fake_keyring.get_password.assert_any_call(KEYRING_SERVICE_NAME, "primary")


def test_list_connections_hydrates_secret_from_keyring(
    fake_keyring: _FakeKeyring,
) -> None:
    save_connection("primary", _service_principal_connection("rotate-me"))

    rows = list_connections()

    assert len(rows) == 1
    name, conn = rows[0]
    assert name == "primary"
    assert conn.client_secret == "rotate-me"


def test_save_connection_with_empty_secret_deletes_keyring_entry(
    fake_keyring: _FakeKeyring,
) -> None:
    # Pre-populate a secret to simulate a switch from service-principal to
    # user-impersonation auth on the same connection name.
    save_connection("primary", _service_principal_connection("old-secret"))
    fake_keyring.set_password.reset_mock()
    fake_keyring.delete_password.reset_mock()

    user_conn = ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
        auth_method=AuthMethod.USER_IMPERSONATION,
        client_secret="",
    )
    save_connection("primary", user_conn)

    fake_keyring.delete_password.assert_called_once_with(
        KEYRING_SERVICE_NAME, "primary"
    )
    fake_keyring.set_password.assert_not_called()
    assert (KEYRING_SERVICE_NAME, "primary") not in fake_keyring.store


def test_save_connection_empty_secret_when_no_entry_exists_is_noop(
    fake_keyring: _FakeKeyring,
) -> None:
    user_conn = ADMEConnection(
        endpoint="https://example.energy.azure.com",
        tenant_id="11111111-1111-1111-1111-111111111111",
        client_id="22222222-2222-2222-2222-222222222222",
        data_partition_id="example-opendes",
        auth_method=AuthMethod.USER_IMPERSONATION,
    )

    # Should not raise even though there is no existing keyring entry to
    # delete (PasswordDeleteError is swallowed).
    save_connection("primary", user_conn)


def test_delete_connection_clears_keyring_and_db_row(
    fake_keyring: _FakeKeyring,
) -> None:
    save_connection("primary", _service_principal_connection("rotate-me"))
    fake_keyring.delete_password.reset_mock()

    delete_connection("primary")

    fake_keyring.delete_password.assert_called_once_with(
        KEYRING_SERVICE_NAME, "primary"
    )
    assert load_connection("primary") is None
    assert (KEYRING_SERVICE_NAME, "primary") not in fake_keyring.store


def test_load_secret_returns_none_when_keyring_backend_raises(
    fake_keyring: _FakeKeyring,
) -> None:
    fake_keyring.get_password.side_effect = RuntimeError("no backend")

    assert _load_secret("anything") is None


def test_load_secret_returns_none_when_keyring_package_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate ``import keyring`` failing — _load_secret must degrade
    # gracefully (returns None) rather than crash module import paths.
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "keyring":
            raise ImportError("simulated missing keyring")
        return real_import(name, *args, **kwargs)

    # Remove the cached fake so the import statement actually runs the loader.
    monkeypatch.delitem(sys.modules, "keyring", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert _load_secret("anything") is None


def test_save_connection_raises_when_keyring_set_fails(
    fake_keyring: _FakeKeyring,
) -> None:
    fake_keyring.set_password.side_effect = RuntimeError("locked vault")

    with pytest.raises(SettingsStoreError, match="not persisted"):
        save_connection("primary", _service_principal_connection("rotate-me"))

    # DB row was still written — operator can fix the keyring and retry.
    # We assert by calling list_connections with a working keyring.
    fake_keyring.set_password.side_effect = fake_keyring._set
    rows = list_connections()
    assert any(name == "primary" for name, _ in rows)


def test_default_connection_name_does_not_touch_real_keyring(
    fake_keyring: _FakeKeyring,
) -> None:
    """Sanity: every keyring call routes through the fake, never the OS."""
    conn = _service_principal_connection("rotate-me")
    save_connection("default", conn)
    load_connection("default")
    delete_connection("default")

    # Every recorded call used our fake's service name; nothing leaked.
    for call in (
        *fake_keyring.set_password.call_args_list,
        *fake_keyring.get_password.call_args_list,
        *fake_keyring.delete_password.call_args_list,
    ):
        assert call.args[0] == KEYRING_SERVICE_NAME
