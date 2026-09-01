"""Prepare Volve Open Test Data manifests and referenced bulk files.

Downloads the Open Group GitLab Volve provided subtree, converts flattened CSV
provided templates to JSON manifests, and optionally downloads every S3 file
referenced by generated work-product Dataset FileSource fields.

Run from repo root::

    .venv/Scripts/python.exe scripts/prepare_volve_data.py --download-files
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.services.provided_csv_converter import convert_provided_csv_tree  # noqa: E402

ARCHIVE_URL = (
    "https://community.opengroup.org/osdu/data/open-test-data/-/archive/"
    "master/open-test-data-master.zip?path=rc--3.0.0/1-data/3-provided/Volve"
)
DEFAULT_ROOT = Path.home() / "osdu-data" / "volve"
CHUNK_SIZE = 1024 * 1024


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Volve data for loading")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--download-files", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--limit-files", type=int, default=0)
    args = parser.parse_args()

    root: Path = args.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    archive = root / "volve-provided.zip"

    if args.overwrite or not archive.is_file():
        print(f"Downloading Volve provided archive to {archive}")
        _download(ARCHIVE_URL, archive)
    else:
        print(f"Using existing archive {archive}")

    print("Extracting Volve provided subtree")
    _extract_volve(archive, root, overwrite=args.overwrite)

    generated_root = convert_provided_csv_tree(root)
    print(f"Generated JSON manifests under {generated_root}")

    sources = sorted(_collect_file_sources(generated_root))
    print(f"Referenced bulk files: {len(sources)}")
    if args.download_files:
        count = args.limit_files if args.limit_files > 0 else len(sources)
        for index, source in enumerate(sources[:count], start=1):
            target = _target_for_source(root / "datasets", source)
            if target.is_file() and not args.overwrite:
                print(f"[{index}/{count}] exists {target}")
                continue
            print(f"[{index}/{count}] downloading {source}")
            target.parent.mkdir(parents=True, exist_ok=True)
            _download(_s3_to_https(source), target)
    else:
        print("Pass --download-files to fetch referenced S3 bulk files.")
    return 0


def _download(url: str, target: Path) -> None:
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    handle.write(chunk)


def _extract_volve(archive: Path, root: Path, *, overwrite: bool) -> None:
    target_root = root / "Volve"
    if overwrite and target_root.exists():
        for path in sorted(target_root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            parts = Path(info.filename).parts
            if "Volve" not in parts:
                continue
            idx = parts.index("Volve")
            rel_parts = parts[idx:]
            if not rel_parts or info.is_dir():
                continue
            out_path = root.joinpath(*rel_parts)
            if out_path.exists() and not overwrite:
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, out_path.open("wb") as dst:
                while True:
                    chunk = src.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    dst.write(chunk)


def _collect_file_sources(generated_root: Path) -> set[str]:
    sources: set[str] = set()
    for path in generated_root.rglob("*.json"):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        _walk_sources(body, sources)
    return sources


def _walk_sources(value: object, sources: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "FileSource" and isinstance(item, str) and item.startswith("s3://"):
                sources.add(item)
            else:
                _walk_sources(item, sources)
    elif isinstance(value, list):
        for item in value:
            _walk_sources(item, sources)


def _s3_to_https(source: str) -> str:
    parsed = urlparse(source)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Not an s3 URL: {source}")
    path = quote(parsed.path.lstrip("/"), safe="/")
    return f"https://{parsed.netloc}.s3.amazonaws.com/{path}"


def _target_for_source(datasets_root: Path, source: str) -> Path:
    parsed = urlparse(source)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0].lower() == "volve":
        return datasets_root.joinpath(*parts[1:])
    if parts:
        return datasets_root.joinpath(*parts)
    raise ValueError(f"Could not derive target path from {source}")


if __name__ == "__main__":
    raise SystemExit(main())
