"""Aggregate stratigraphic marker vocabulary from OSDU WellboreMarkerSet data.

The WellboreMarkerSet work-product-component is the one WPC kind in the
Volve/TNO data that carries genuine domain vocabulary: geological marker
(formation/member) names such as ``Bentheim Sandstone Member``. This module
turns raw ``Markers[]`` picks into a deduplicated vocabulary with per-term
statistics suitable for embedding and indexing in Azure AI Search.

Pure functions only — no network or SDK dependencies — so the aggregation is
unit-testable against fixtures. Network extraction lives in the indexer script.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

# Names that are structural placeholders, not real stratigraphic vocabulary.
_NOISE_NAMES = frozenset({"fault", "unknown", "missing", "n/a", "na", ""})

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Return a normalized key for a marker name (case/space-insensitive)."""
    collapsed = _WHITESPACE_RE.sub(" ", name).strip()
    return collapsed.casefold()


def _stable_id(normalized: str) -> str:
    """Deterministic, index-safe document id for a vocabulary term."""
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
    return f"marker-{digest[:20]}"


@dataclass
class MarkerVocabularyEntry:
    """One distinct marker name with aggregated cross-well statistics."""

    id: str
    marker_name: str
    normalized_name: str
    occurrence_count: int = 0
    wellbore_count: int = 0
    geological_ages: list[str] = field(default_factory=list)
    marker_interpreters: list[str] = field(default_factory=list)
    depth_min: float | None = None
    depth_max: float | None = None
    example_wellbores: list[str] = field(default_factory=list)

    def to_search_text(self) -> str:
        """Build the text that gets embedded for semantic search."""
        parts = [self.marker_name]
        if self.geological_ages:
            parts.append(f"Geological age: {', '.join(self.geological_ages)}")
        return " | ".join(parts)


@dataclass
class _Accumulator:
    """Mutable per-term accumulator used during aggregation."""

    marker_name: str
    normalized_name: str
    occurrences: int = 0
    wellbores: set[str] = field(default_factory=set)
    ages: set[str] = field(default_factory=set)
    interpreters: set[str] = field(default_factory=set)
    depth_min: float | None = None
    depth_max: float | None = None


def _coerce_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _iter_markers(record: dict) -> Iterable[tuple[dict, str | None]]:
    """Yield ``(marker, wellbore_id)`` for each marker in a record."""
    data = record.get("data")
    if not isinstance(data, dict):
        return
    wellbore_id = data.get("WellboreID")
    wellbore = wellbore_id if isinstance(wellbore_id, str) else None
    markers = data.get("Markers")
    if not isinstance(markers, list):
        return
    for marker in markers:
        if isinstance(marker, dict):
            yield marker, wellbore


def aggregate_markers(
    records: Iterable[dict],
    *,
    max_examples: int = 5,
) -> list[MarkerVocabularyEntry]:
    """Aggregate WellboreMarkerSet records into a distinct-name vocabulary.

    Args:
        records: OSDU WellboreMarkerSet records (each with a ``data`` object).
        max_examples: How many example wellbore ids to retain per term.

    Returns:
        Vocabulary entries sorted by descending occurrence count. Structural
        placeholder names (``FAULT``, ``UNKNOWN``, empty) are excluded.
    """
    accumulators: dict[str, _Accumulator] = {}

    for record in records:
        for marker, wellbore in _iter_markers(record):
            raw_name = marker.get("MarkerName")
            if not isinstance(raw_name, str) or not raw_name.strip():
                continue
            normalized = normalize_name(raw_name)
            if normalized in _NOISE_NAMES:
                continue

            acc = accumulators.get(normalized)
            if acc is None:
                acc = _Accumulator(
                    marker_name=_WHITESPACE_RE.sub(" ", raw_name).strip(),
                    normalized_name=normalized,
                )
                accumulators[normalized] = acc

            acc.occurrences += 1
            if wellbore:
                acc.wellbores.add(wellbore)

            age = marker.get("GeologicalAge")
            if isinstance(age, str) and age.strip():
                acc.ages.add(age.strip())

            interpreter = marker.get("MarkerInterpreter")
            if isinstance(interpreter, str) and interpreter.strip():
                acc.interpreters.add(interpreter.strip())

            depth = _coerce_float(marker.get("MarkerMeasuredDepth"))
            if depth is not None:
                acc.depth_min = depth if acc.depth_min is None else min(acc.depth_min, depth)
                acc.depth_max = depth if acc.depth_max is None else max(acc.depth_max, depth)

    entries = [
        MarkerVocabularyEntry(
            id=_stable_id(acc.normalized_name),
            marker_name=acc.marker_name,
            normalized_name=acc.normalized_name,
            occurrence_count=acc.occurrences,
            wellbore_count=len(acc.wellbores),
            geological_ages=sorted(acc.ages),
            marker_interpreters=sorted(acc.interpreters),
            depth_min=acc.depth_min,
            depth_max=acc.depth_max,
            example_wellbores=sorted(acc.wellbores)[:max_examples],
        )
        for acc in accumulators.values()
    ]
    entries.sort(key=lambda e: e.occurrence_count, reverse=True)
    return entries
