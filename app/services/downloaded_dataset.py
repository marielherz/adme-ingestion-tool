"""Discover loadable parts of a downloaded OSDU dataset (TNO / Volve).

The Azure ``osdu-data-load-tno`` download lays pre-generated manifests out
under ``<root>/TNO/provided/{reference-data,master-data,work-products}/**``
with the actual file blobs under ``<root>/datasets/**``. This module
enumerates the loadable *parts* so the Bulk Load page can drive a load
directly from an operator-chosen folder, outside the bundled dataset
registry and its ``app/data/`` sandbox.

No network and no Streamlit — pure filesystem inspection returning frozen
dataclasses, mirroring the rest of :mod:`app.services`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DownloadedPart",
    "datasets_root_for",
    "discover_parts",
    "list_part_manifests",
]

_PROVIDED_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("TNO", "provided"),
    ("provided",),
    (),
)
_DATASETS_DIRNAME = "datasets"
_TIER_SECTION: dict[str, str] = {
    "reference-data": "ReferenceData",
    "master-data": "MasterData",
}


@dataclass(frozen=True, slots=True)
class DownloadedPart:
    """One loadable slice of a downloaded dataset.

    ``section`` is the list-manifest section for reference/master data and
    ``None`` for work-products (which use the upload-then-submit flow).
    ``manifest_dir`` holds the ``*.json`` manifests; ``datasets_root`` is the
    blob root passed to the work-product loader (unused for list tiers).
    """

    key: str
    label: str
    kind: str
    section: str | None
    is_work_product: bool
    manifest_dir: Path
    manifest_count: int
    datasets_root: Path


def _find_provided(root: Path) -> Path | None:
    for parts in _PROVIDED_CANDIDATES:
        candidate = root.joinpath(*parts) if parts else root
        if (candidate / "reference-data").is_dir() or (
            candidate / "work-products"
        ).is_dir() or (candidate / "master-data").is_dir():
            return candidate
    return None


def datasets_root_for(root: Path) -> Path:
    """Return the blob ``datasets/`` root for a download ``root``."""
    direct = root / _DATASETS_DIRNAME
    if direct.is_dir():
        return direct
    provided = _find_provided(root)
    if provided is not None:
        # provided is typically <root>/TNO/provided; datasets sits at <root>.
        for ancestor in (provided.parent, provided.parent.parent, root):
            candidate = ancestor / _DATASETS_DIRNAME
            if candidate.is_dir():
                return candidate
    return direct


def _count_manifests(directory: Path) -> int:
    return sum(1 for _ in directory.glob("*.json"))


def discover_parts(root: Path) -> list[DownloadedPart]:
    """Enumerate the loadable parts under a download ``root``.

    Returns an ordered list: reference-data first, then each master-data
    subfolder, then each work-products subfolder. Empty when ``root`` is not
    a recognizable download layout.
    """
    provided = _find_provided(root)
    if provided is None:
        return []
    ds_root = datasets_root_for(root)
    parts: list[DownloadedPart] = []

    ref_dir = provided / "reference-data"
    if ref_dir.is_dir():
        count = _count_manifests(ref_dir)
        if count:
            parts.append(
                DownloadedPart(
                    key="reference-data",
                    label=f"reference-data ({count})",
                    kind="reference-data",
                    section=_TIER_SECTION["reference-data"],
                    is_work_product=False,
                    manifest_dir=ref_dir,
                    manifest_count=count,
                    datasets_root=ds_root,
                )
            )

    for kind, is_wp in (("master-data", False), ("work-products", True)):
        base = provided / kind
        if not base.is_dir():
            continue
        for sub in sorted(p for p in base.iterdir() if p.is_dir()):
            count = _count_manifests(sub)
            if not count:
                continue
            parts.append(
                DownloadedPart(
                    key=f"{kind}/{sub.name}",
                    label=f"{kind} / {sub.name} ({count})",
                    kind=kind,
                    section=_TIER_SECTION.get(kind),
                    is_work_product=is_wp,
                    manifest_dir=sub,
                    manifest_count=count,
                    datasets_root=ds_root,
                )
            )

    return parts


def list_part_manifests(part: DownloadedPart, *, limit: int = 0) -> list[Path]:
    """Return the sorted ``*.json`` manifest paths for a part.

    ``limit`` caps the count (``0`` = all) so the page can run a small smoke
    batch before turning loose the full set.
    """
    manifests = sorted(part.manifest_dir.glob("*.json"))
    if limit > 0:
        return manifests[:limit]
    return manifests
