"""Tests for the ResumableProgress state service."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.load_progress import ResumableProgress


def test_mark_and_completed(tmp_path: Path) -> None:
    p = ResumableProgress(tmp_path / "state.json")
    p.mark("well", "a")
    p.mark("well", "b")
    assert p.completed("well") == {"a", "b"}
    assert p.is_done("well", "a")
    assert not p.is_done("well", "z")
    assert p.count("well") == 2


def test_remaining_filters_completed(tmp_path: Path) -> None:
    p = ResumableProgress(tmp_path / "state.json")
    p.mark("t", "a")
    p.mark("t", "c")
    assert p.remaining("t", ["a", "b", "c", "d"]) == ["b", "d"]


def test_save_and_reload_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    p = ResumableProgress(path)
    p.mark("well", "w1")
    p.mark("bore", "b1")
    p.save()

    p2 = ResumableProgress(path)
    assert p2.completed("well") == {"w1"}
    assert p2.completed("bore") == {"b1"}


def test_periodic_save(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    p = ResumableProgress(path, save_every=2)
    p.mark_and_maybe_save("t", "a")
    assert not path.exists()  # not yet
    p.mark_and_maybe_save("t", "b")  # hits save_every -> persists
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["t"] == ["a", "b"]


def test_legacy_int_migration(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"well": 3}), encoding="utf-8")
    p = ResumableProgress(path)
    ordered = ["n0", "n1", "n2", "n3", "n4"]
    # First 3 (the legacy count) are treated as completed.
    assert p.completed("well", migrate_int=ordered) == {"n0", "n1", "n2"}
    assert p.remaining("well", ordered) == ["n3", "n4"]


def test_unreadable_file_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{ not json", encoding="utf-8")
    p = ResumableProgress(path)
    assert p.completed("anything") == set()
