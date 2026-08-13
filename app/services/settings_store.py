"""Local SQLite-backed store for persisted ADME connection settings.

This module owns the durable on-disk record of operator-entered ADME
connection configuration.  It is intentionally a thin layer over stdlib
``sqlite3``: each public function opens a short-lived connection, runs a
single logical operation, and closes the connection.  No module-global
cursor, no Streamlit caching, no ORM.

Auth/session material (access tokens, MSAL pending flows, user auth state)
is **never** written here.  ``client_secret`` is dropped from every SQLite
insert/update and stored separately in the OS credential store — see
:func:`save_connection`.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.models.connection import ADME_RESOURCE_SCOPE, ADMEConnection, AuthMethod

__all__ = [
    "DEFAULT_DB_PATH",
    "SettingsStoreError",
    "clear_active_connection",
    "clear_user_token",
    "delete_connection",
    "get_active_connection_name",
    "get_db_path",
    "initialize_store",
    "list_connections",
    "load_connection",
    "load_user_token",
    "save_connection",
    "save_user_token",
    "set_active_connection",
]

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH: Path = Path.home() / ".adme-ingestion-tool" / "settings.db"
ENV_DB_PATH = "ADME_SETTINGS_DB"
KEYRING_SERVICE_NAME = "adme-ingestion-tool"


def _store_secret(name: str, secret: str | None) -> None:
    """Persist *secret* under *name* in the OS keyring.

    A ``None`` or empty *secret* deletes any existing entry.  All keyring
    interaction is best-effort: if the backend is unavailable or the
    ``keyring`` package is not installed we log and re-raise as
    :class:`SettingsStoreError` so callers can surface a clear "secret was
    not persisted" message to the operator.
    """
    try:
        import keyring  # noqa: PLC0415 — lazy so module import never breaks
        from keyring.errors import (  # noqa: PLC0415
            PasswordDeleteError,
        )
    except ImportError as exc:
        msg = (
            "The 'keyring' package is not installed; client_secret cannot be "
            "persisted in the OS credential store."
        )
        logger.warning("settings_store: %s (%s)", msg, exc)
        raise SettingsStoreError(msg) from exc

    if secret:
        try:
            keyring.set_password(KEYRING_SERVICE_NAME, name, secret)
        except Exception as exc:  # noqa: BLE001 — keyring backends raise broadly
            msg = (
                f"Could not write client_secret for connection {name!r} to "
                "the OS keyring; secret was not persisted."
            )
            logger.warning("settings_store: %s (%s)", msg, exc)
            raise SettingsStoreError(msg) from exc
        return

    try:
        keyring.delete_password(KEYRING_SERVICE_NAME, name)
    except PasswordDeleteError:
        # "No password to delete" is a valid no-op — the secret was already
        # absent (e.g. user-impersonation connection, or first-time save).
        pass
    except Exception as exc:  # noqa: BLE001
        msg = (
            f"Could not clear client_secret for connection {name!r} from "
            "the OS keyring."
        )
        logger.warning("settings_store: %s (%s)", msg, exc)
        raise SettingsStoreError(msg) from exc


def _load_secret(name: str) -> str | None:
    """Return the stored client_secret for *name*, or ``None``.

    Returns ``None`` if the keyring package is missing, the backend is
    unavailable, or no entry exists.  Hydration is best-effort by design —
    callers fall back to an empty secret and the operator can re-enter it.
    """
    try:
        import keyring  # noqa: PLC0415
    except ImportError as exc:
        logger.warning(
            "settings_store: 'keyring' package missing; cannot load "
            "client_secret for %r (%s)",
            name,
            exc,
        )
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE_NAME, name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "settings_store: could not read client_secret for %r from "
            "the OS keyring (%s)",
            name,
            exc,
        )
        return None


_USER_TOKEN_SUFFIX = "::user-token"

# Windows Credential Manager caps a single credential blob at 2560 bytes
# (~1280 UTF-16 chars). A JWT bearer token is ~2 KB+, so the payload is split
# into chunks that comfortably fit and reassembled on load. A separate
# ``::count`` entry records how many chunks to read back.
_USER_TOKEN_CHUNK_SIZE = 1000
_MAX_TOKEN_CHUNKS = 64


def _user_token_count_key(name: str) -> str:
    return f"{name}{_USER_TOKEN_SUFFIX}::count"


def _user_token_chunk_key(name: str, index: int) -> str:
    return f"{name}{_USER_TOKEN_SUFFIX}::{index}"


def save_user_token(
    access_token: str,
    expires_at: int | None,
    name: str | None = None,
) -> None:
    """Persist a user bearer token + expiry in the OS keyring.

    Stored alongside (but separate from) the client secret, never in SQLite.
    Scoped to the active connection name so switching connections does not
    leak a token across instances. The JSON payload is chunked to stay under
    the OS credential blob size limit. Raises :class:`SettingsStoreError` when
    the keyring backend is unavailable so callers can keep it best-effort.
    """
    target = name or get_active_connection_name()
    if not target or not access_token:
        return
    payload = json.dumps(
        {"access_token": access_token, "expires_at": expires_at}
    )
    chunks = [
        payload[i : i + _USER_TOKEN_CHUNK_SIZE]
        for i in range(0, len(payload), _USER_TOKEN_CHUNK_SIZE)
    ] or [""]

    # Clear any prior chunks first so a shorter token can't leave stale tails.
    _clear_user_token(target)
    for index, chunk in enumerate(chunks):
        _store_secret(_user_token_chunk_key(target, index), chunk)
    _store_secret(_user_token_count_key(target), str(len(chunks)))


def load_user_token(
    name: str | None = None,
) -> tuple[str, int | None] | None:
    """Return the persisted ``(access_token, expires_at)`` for the connection.

    Returns ``None`` when no token is stored, the keyring is unavailable, or
    the stored payload is malformed. Hydration is best-effort by design.
    """
    target = name or get_active_connection_name()
    if not target:
        return None
    count_raw = _load_secret(_user_token_count_key(target))
    if not count_raw:
        return None
    try:
        count = int(count_raw)
    except (ValueError, TypeError):
        return None
    if count < 1 or count > _MAX_TOKEN_CHUNKS:
        return None

    parts: list[str] = []
    for index in range(count):
        chunk = _load_secret(_user_token_chunk_key(target, index))
        if chunk is None:
            return None
        parts.append(chunk)
    raw = "".join(parts)

    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        return None
    expires_at = data.get("expires_at")
    if isinstance(expires_at, bool):
        expires_at = None
    elif isinstance(expires_at, float):
        expires_at = int(expires_at)
    elif not isinstance(expires_at, int):
        expires_at = None
    return token, expires_at


def clear_user_token(name: str | None = None) -> None:
    """Delete any persisted user token for the active (or named) connection."""
    target = name or get_active_connection_name()
    if not target:
        return
    _clear_user_token(target)


def _clear_user_token(target: str) -> None:
    """Delete the chunk-count entry and every chunk for ``target``."""
    count_raw = _load_secret(_user_token_count_key(target))
    count = 0
    if count_raw:
        try:
            count = int(count_raw)
        except (ValueError, TypeError):
            count = 0
    _store_secret(_user_token_count_key(target), None)
    for index in range(max(count, _MAX_TOKEN_CHUNKS)):
        _store_secret(_user_token_chunk_key(target, index), None)


_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS connections (
        name              TEXT PRIMARY KEY,
        endpoint          TEXT NOT NULL,
        tenant_id         TEXT NOT NULL,
        client_id         TEXT NOT NULL,
        data_partition_id TEXT NOT NULL,
        token_scope       TEXT NOT NULL DEFAULT 'https://energy.azure.com/.default',
        auth_method       TEXT NOT NULL CHECK (auth_method IN
                              ('user_impersonation', 'service_principal')),
        is_active         INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_connections_active
        ON connections(is_active) WHERE is_active = 1
    """,
)


class SettingsStoreError(Exception):
    """Raised when the local settings store cannot be read or written."""


def get_db_path() -> Path:
    """Return the resolved SQLite path, honoring ``ADME_SETTINGS_DB``."""
    override = os.environ.get(ENV_DB_PATH)
    if override:
        return Path(override).expanduser()
    return DEFAULT_DB_PATH


def _resolve_path(db_path: Path | None) -> Path:
    return Path(db_path) if db_path is not None else get_db_path()


@contextmanager
def _connect(db_path: Path | None) -> Iterator[sqlite3.Connection]:
    """Open a short-lived sqlite3 connection with foreign keys enabled."""
    resolved = _resolve_path(db_path)
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"Could not create settings directory at {resolved.parent}."
        logger.error("settings_store: %s (%s)", msg, exc)
        raise SettingsStoreError(msg) from exc
    try:
        connection = sqlite3.connect(resolved, isolation_level=None)
    except sqlite3.Error as exc:
        msg = f"Could not open settings database at {resolved}."
        logger.error("settings_store: %s (%s)", msg, exc)
        raise SettingsStoreError(msg) from exc
    connection.row_factory = sqlite3.Row
    try:
        with closing(connection):
            yield connection
    except sqlite3.Error as exc:
        msg = "Settings database operation failed."
        logger.error("settings_store: %s (%s)", msg, exc)
        raise SettingsStoreError(msg) from exc


def initialize_store(db_path: Path | None = None) -> None:
    """Create the DB file and schema if missing.  Idempotent."""
    with _connect(db_path) as conn:
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)


def _row_to_connection(row: sqlite3.Row) -> ADMEConnection:
    try:
        auth_method = AuthMethod(row["auth_method"])
    except ValueError as exc:
        msg = (
            f"Stored connection {row['name']!r} has an unknown auth_method "
            f"{row['auth_method']!r}."
        )
        logger.error("settings_store: %s", msg)
        raise SettingsStoreError(msg) from exc
    token_scope = row["token_scope"] or ADME_RESOURCE_SCOPE
    secret = _load_secret(row["name"]) or ""
    return ADMEConnection(
        endpoint=row["endpoint"],
        tenant_id=row["tenant_id"],
        client_id=row["client_id"],
        data_partition_id=row["data_partition_id"],
        token_scope=token_scope,
        auth_method=auth_method,
        client_secret=secret,
    )


def list_connections(
    db_path: Path | None = None,
) -> list[tuple[str, ADMEConnection]]:
    """Return saved ``(name, connection)`` pairs, ordered by name."""
    initialize_store(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM connections ORDER BY name ASC"
        ).fetchall()
    return [(row["name"], _row_to_connection(row)) for row in rows]


def load_connection(
    name: str,
    db_path: Path | None = None,
) -> ADMEConnection | None:
    """Return the saved connection for ``name``, or ``None`` if missing."""
    if not name:
        return None
    initialize_store(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM connections WHERE name = ?",
            (name,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_connection(row)


def save_connection(
    name: str,
    connection: ADMEConnection,
    db_path: Path | None = None,
) -> None:
    """Insert or update a saved connection by name.

    The SQLite row never contains ``client_secret`` (schema unchanged); the
    secret is written separately to the OS keyring after the DB write
    succeeds.  If keyring persistence fails the DB row remains, but we raise
    :class:`SettingsStoreError` so the operator knows the secret was not
    saved and must be re-entered next session.
    """
    if not name:
        msg = "Cannot save a connection without a name."
        logger.error("settings_store: %s", msg)
        raise SettingsStoreError(msg)

    now = datetime.now(UTC).isoformat()
    initialize_store(db_path)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO connections (
                    name, endpoint, tenant_id, client_id, data_partition_id,
                    token_scope, auth_method, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    endpoint          = excluded.endpoint,
                    tenant_id         = excluded.tenant_id,
                    client_id         = excluded.client_id,
                    data_partition_id = excluded.data_partition_id,
                    token_scope       = excluded.token_scope,
                    auth_method       = excluded.auth_method,
                    updated_at        = excluded.updated_at
                """,
                (
                    name,
                    connection.endpoint,
                    connection.tenant_id,
                    connection.client_id,
                    connection.data_partition_id,
                    connection.token_scope,
                    connection.auth_method.value,
                    now,
                    now,
                ),
            )
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    # Persist (or clear) the client_secret in the OS keyring AFTER the DB
    # write so we never leave an orphan secret pointing at a connection that
    # failed to save.
    _store_secret(name, connection.client_secret or None)


def delete_connection(
    name: str,
    db_path: Path | None = None,
) -> None:
    """Remove a saved connection by name.  No error if missing."""
    if not name:
        return
    initialize_store(db_path)
    # Clear the keyring entry FIRST so a DB failure does not leave an
    # orphan secret pointing at a now-missing row.  ``_store_secret`` with
    # ``None`` swallows "no password to delete".
    try:
        _store_secret(name, None)
    except SettingsStoreError as exc:
        # Keyring failure should not block DB cleanup; log and continue.
        logger.warning(
            "settings_store: keyring cleanup for %r failed (%s); proceeding "
            "with DB delete",
            name,
            exc,
        )
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM connections WHERE name = ?", (name,))


def get_active_connection_name(
    db_path: Path | None = None,
) -> str | None:
    """Return the currently active connection name, or ``None``."""
    initialize_store(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM connections WHERE is_active = 1 LIMIT 1"
        ).fetchone()
    return row["name"] if row is not None else None


def set_active_connection(
    name: str,
    db_path: Path | None = None,
) -> None:
    """Mark ``name`` as active and clear the active flag on all other rows.

    Raises :class:`SettingsStoreError` if ``name`` does not exist.
    """
    if not name:
        msg = "Cannot activate a connection without a name."
        logger.error("settings_store: %s", msg)
        raise SettingsStoreError(msg)

    initialize_store(db_path)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT 1 FROM connections WHERE name = ?",
                (name,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                msg = f"Cannot activate unknown connection {name!r}."
                logger.error("settings_store: %s", msg)
                raise SettingsStoreError(msg)
            # Clear all active flags first to keep the partial unique index
            # honest, then activate the requested row.
            conn.execute(
                "UPDATE connections SET is_active = 0 WHERE is_active = 1"
            )
            conn.execute(
                "UPDATE connections SET is_active = 1, updated_at = ? "
                "WHERE name = ?",
                (datetime.now(UTC).isoformat(), name),
            )
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def clear_active_connection(db_path: Path | None = None) -> None:
    """Clear the active flag on all rows.  Used on Sign Out / reset."""
    initialize_store(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE connections SET is_active = 0 WHERE is_active = 1"
        )
