"""Resumable per-key completion tracking for long bulk loads.

A big load (thousands of manifests) needs to survive interruption without
re-doing — or worse, duplicating — work already done. This tracks, per
logical ``key`` (a load tier / part), the set of item names that have
completed successfully, persisted to a JSON file so a restart resumes from
exactly where it left off.

Names are used (not indexes) so out-of-order concurrent completions are
recorded correctly. Saves are explicit (call :meth:`save`) or throttled via
:meth:`mark_and_maybe_save`; the caller controls IO frequency.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["ResumableProgress"]


class ResumableProgress:
    """Per-key set of completed item names, backed by a JSON file.

    File shape: ``{"<key>": ["name1", "name2", ...], ...}``. A legacy
    integer value (``{"<key>": N}``) is accepted and interpreted via
    ``migrate_int`` when provided, so older progress files still resume.
    """

    def __init__(self, path: Path, *, save_every: int = 25) -> None:
        self._path = Path(path)
        self._save_every = max(1, save_every)
        self._completed: dict[str, set[str]] = {}
        self._raw_ints: dict[str, int] = {}
        self._since_save = 0
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "Progress file %s unreadable; starting fresh.", self._path
            )
            return
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            if isinstance(value, list):
                self._completed[key] = {str(v) for v in value}
            elif isinstance(value, int):
                # Legacy integer index — resolved lazily by completed().
                self._raw_ints[key] = value

    def completed(
        self,
        key: str,
        *,
        migrate_int: Iterable[str] | None = None,
    ) -> set[str]:
        """Return the completed-name set for ``key``.

        If the stored value is a legacy integer ``N`` and ``migrate_int`` (an
        ordered iterable of this key's item names) is given, the first ``N``
        names are treated as completed and the entry is upgraded in place.
        """
        if key in self._completed:
            return self._completed[key]
        n = self._raw_ints.pop(key, None)
        if n and migrate_int is not None:
            ordered = list(migrate_int)
            self._completed[key] = set(ordered[:n])
        else:
            self._completed[key] = set()
        return self._completed[key]

    def is_done(self, key: str, name: str) -> bool:
        return name in self.completed(key)

    def remaining(self, key: str, names: Iterable[str]) -> list[str]:
        """Return the subset of ``names`` not yet completed for ``key``."""
        done = self.completed(key)
        return [n for n in names if n not in done]

    def mark(self, key: str, name: str) -> None:
        """Record ``name`` as completed for ``key`` (in memory)."""
        self.completed(key).add(name)

    def mark_and_maybe_save(self, key: str, name: str) -> None:
        """Mark ``name`` done and persist every ``save_every`` marks."""
        self.mark(key, name)
        self._since_save += 1
        if self._since_save >= self._save_every:
            self.save()

    def count(self, key: str) -> int:
        return len(self.completed(key))

    def save(self) -> None:
        """Persist all keys to disk (atomic replace)."""
        self._since_save = 0
        payload = {k: sorted(v) for k, v in self._completed.items()}
        # Preserve any not-yet-migrated legacy ints so we never lose progress.
        for k, n in self._raw_ints.items():
            payload.setdefault(k, n)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(self._path)
