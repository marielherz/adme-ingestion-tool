#!/usr/bin/env python3
"""Complete TNO dataset ingestion orchestrator.

Runs the full TNO pipeline:
1. Generate master-data manifests from CSVs
2. Load master-data to Storage
3. Load work-products via DAG (async, resumable)

Usage:
    python scripts/ingest_tno_complete.py

Options:
    --skip-generate         Skip master-data generation
    --skip-master-data      Skip master-data ingestion
    --skip-work-products    Skip work-product ingestion
    --max-concurrency N     Max concurrent work-product submissions (default 8)
    --include-v110          Include v1.1.0 schema variants
    --dry-run               Validate config without running
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.services.tno_ingestion import TNOIngestionConfig, TNOIngestionOrchestrator  # noqa: E402


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
        description="Complete TNO dataset ingestion (generation, master-data, work-products)",
    )
    parser.add_argument(
        "--skip-generate",
        action="store_true",
        help="Skip master-data generation from CSVs",
    )
    parser.add_argument(
        "--skip-master-data",
        action="store_true",
        help="Skip master-data ingestion",
    )
    parser.add_argument(
        "--skip-work-products",
        action="store_true",
        help="Skip work-product ingestion",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=8,
        help="Max concurrent work-product submissions (default 8)",
    )
    parser.add_argument(
        "--include-v110",
        action="store_true",
        help="Include v1.1.0 schema variants",
    )
    parser.add_argument(
        "--tno-root",
        type=Path,
        default=Path.home() / "osdu-data" / "tno",
        help="TNO root path (contains TNO/provided and datasets)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show configuration without running ingestion",
    )

    args = parser.parse_args()

    # Setup logging
    log_file = Path(__file__).resolve().parent / "tno_ingest_complete.log"
    _configure_logging(log_file)

    logger = logging.getLogger("tno_ingest_main")
    logger.info(f"Logs: {log_file}")

    # Build configuration
    config = TNOIngestionConfig(
        skip_generate=args.skip_generate,
        skip_master_data=args.skip_master_data,
        skip_work_products=args.skip_work_products,
        max_concurrency=args.max_concurrency,
        include_v110=args.include_v110,
        tno_root=args.tno_root.expanduser().resolve(),
    )

    # Validate configuration
    if not config.tno_root.is_dir():
        logger.error(f"TNO root not found: {config.tno_root}")
        return 1

    # Show configuration
    logger.info("=" * 60)
    logger.info("TNO INGESTION CONFIGURATION")
    logger.info("=" * 60)
    logger.info(f"Endpoint:              {config.endpoint}")
    logger.info(f"Data Partition:        {config.data_partition_id}")
    logger.info(f"TNO Root:              {config.tno_root}")
    logger.info(f"Generation:            {'DISABLED' if args.skip_generate else 'ENABLED'}")
    logger.info(f"Master-Data:           {'DISABLED' if args.skip_master_data else 'ENABLED'}")
    logger.info(f"Work-Products:         {'DISABLED' if args.skip_work_products else 'ENABLED'}")
    logger.info(f"Max Concurrency:       {config.max_concurrency}")
    logger.info(f"Include v1.1.0:        {config.include_v110}")
    logger.info("=" * 60)

    if args.dry_run:
        logger.info("Dry run mode - configuration validated, no ingestion performed")
        return 0

    # Run orchestrator
    orchestrator = TNOIngestionOrchestrator(config)
    success = orchestrator.run()

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
