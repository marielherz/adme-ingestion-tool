#!/usr/bin/env python3
"""Complete Volve dataset ingestion orchestrator.

Runs all three ingestion tracks in parallel and validates results:
1. Seismic data → SDMS
2. Core metadata → Storage
3. Wellbore/Well → DDMS

Usage:
    python scripts/ingest_volve_complete.py

Options:
    --skip-seismic          Skip seismic ingestion
    --skip-wellbore         Skip Wellbore DDMS ingestion
    --skip-workproducts     Skip work-product DAG workflows
    --metadata-only         Skip DAG workflows for core metadata
    --dry-run               Show what would be ingested without running
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.services.volve_ingestion import VolveIngestionConfig, VolveIngestionOrchestrator  # noqa: E402


def _configure_logging(log_file: Path) -> None:
    """Configure logging to console and file."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    # File handler
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Reduce noise from libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Complete Volve dataset ingestion (seismic, metadata, wellbores)",
    )
    parser.add_argument(
        "--skip-seismic",
        action="store_true",
        help="Skip seismic ingestion",
    )
    parser.add_argument(
        "--skip-wellbore",
        action="store_true",
        help="Skip Wellbore DDMS ingestion",
    )
    parser.add_argument(
        "--skip-workproducts",
        action="store_true",
        help="Skip work-product DAG workflows",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Skip DAG workflows for core metadata",
    )
    parser.add_argument(
        "--generated-data-root",
        type=Path,
        default=Path.home() / "osdu-data" / "volve" / "generated-json",
        help="Root path for generated Volve manifests",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show configuration without running ingestion",
    )

    args = parser.parse_args()

    # Setup logging
    log_file = Path(__file__).resolve().parent / "volve_ingest_complete.log"
    _configure_logging(log_file)

    logger = logging.getLogger("volve_ingest_main")
    logger.info(f"Logs: {log_file}")

    # Build configuration
    config = VolveIngestionConfig(
        skip_seismic=args.skip_seismic,
        skip_wellbore=args.skip_wellbore,
        skip_workproducts=args.skip_workproducts,
        metadata_only=args.metadata_only or args.skip_workproducts,
        generated_data_root=args.generated_data_root.expanduser().resolve(),
    )

    # Validate configuration
    if not config.generated_data_root.is_dir():
        logger.error(f"Generated data root not found: {config.generated_data_root}")
        logger.error("Please prepare Volve data with: python scripts/prepare_volve_data.py")
        return 1

    if not config.sdutil_path.exists():
        logger.error(f"sdutil not found: {config.sdutil_path}")
        logger.error("Please install seismic-store-sdutil")
        return 1

    # Show configuration
    logger.info("=" * 60)
    logger.info("VOLVE INGESTION CONFIGURATION")
    logger.info("=" * 60)
    logger.info(f"Endpoint:              {config.endpoint}")
    logger.info(f"Data Partition:        {config.data_partition_id}")
    logger.info(f"Generated Data:        {config.generated_data_root}")
    logger.info(f"Seismic:               {'DISABLED' if args.skip_seismic else 'ENABLED'}")
    logger.info(f"Wellbore DDMS:         {'DISABLED' if args.skip_wellbore else 'ENABLED'}")
    logger.info(f"Metadata Only:         {config.metadata_only}")
    logger.info("=" * 60)

    if args.dry_run:
        logger.info("Dry run mode - configuration validated, no ingestion performed")
        return 0

    # Run orchestrator
    orchestrator = VolveIngestionOrchestrator(config)
    success = orchestrator.run()

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
