"""Unified TNO dataset ingestion orchestrator.

Coordinates TNO ingestion pipeline:
1. Master-data generation from CSVs
2. Master-data loading to Storage
3. Work-product loading via DAG (async, resumable)

Supports resumable runs with checkpoint tracking.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TNOIngestionConfig:
    """Configuration for TNO ingestion."""

    endpoint: str = "https://marielsmrttier.energy.azure.com"
    tenant_id: str = "72f988bf-86f1-41af-91ab-2d7cd011db47"
    client_id: str = "ef3f6421-4b33-42b4-9184-d7c5cb2efcf2"
    data_partition_id: str = "opendes"
    token_scope: str = "ef3f6421-4b33-42b4-9184-d7c5cb2efcf2/.default"
    legal_tag: str = "opendes-referencedata-legal"
    acl_owners: list[str] = field(default_factory=lambda: ["data.default.owners@opendes.dataservices.energy"])
    acl_viewers: list[str] = field(default_factory=lambda: ["data.default.viewers@opendes.dataservices.energy"])
    
    # Paths
    tno_root: Path = field(default_factory=lambda: Path.home() / "osdu-data" / "tno")
    
    # Options
    skip_generate: bool = False
    skip_master_data: bool = False
    skip_work_products: bool = False
    max_concurrency: int = 8
    include_v110: bool = False


@dataclass
class TNOIngestionState:
    """Checkpoint state for resumable TNO ingestion."""

    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    
    generate_completed: bool = False
    generate_ok: int = 0  # Number of CSV files processed
    
    master_data_completed: bool = False
    master_data_ok: int = 0
    master_data_failed: int = 0
    
    work_products_completed: bool = False
    work_products_ok: int = 0
    work_products_failed: int = 0
    work_products_submitted: int = 0
    
    errors: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_file(cls, path: Path) -> TNOIngestionState:
        """Load state from checkpoint file."""
        if path.exists():
            with open(path) as f:
                return cls(**json.load(f))
        return cls()

    def to_file(self, path: Path) -> None:
        """Save state to checkpoint file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)


class TNOIngestionOrchestrator:
    """Orchestrates unified TNO dataset ingestion."""

    def __init__(self, config: TNOIngestionConfig, checkpoint_path: Optional[Path] = None):
        self.config = config
        self.checkpoint_path = checkpoint_path or Path(__file__).resolve().parent.parent.parent / "scripts" / ".tno_ingestion_checkpoint.json"
        self.state = TNOIngestionState.from_file(self.checkpoint_path)

    def generate_master_data(self) -> bool:
        """Generate TNO master-data manifests from CSVs."""
        if self.config.skip_generate or self.state.generate_completed:
            logger.info("Skipping master-data generation (already completed or disabled)")
            return True

        logger.info("Starting TNO master-data generation from CSVs...")
        try:
            script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "generate_tno_master_data.py"
            
            result = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                self.state.errors.append(f"Master-data generation failed: {result.stderr}")
                logger.error(f"Master-data generation failed: {result.stderr}")
                return False

            self.state.generate_completed = True
            self.state.generate_ok = 3  # 3 entity types: Organisation, Well, Wellbore
            logger.info(f"Master-data generation completed: {self.state.generate_ok} entity manifests")
            return True

        except Exception as e:
            self.state.errors.append(f"Master-data generation error: {str(e)}")
            logger.exception("Master-data generation error")
            return False

    def ingest_master_data(self) -> bool:
        """Ingest TNO master-data to Storage via bulk loader."""
        if self.config.skip_master_data or self.state.master_data_completed:
            logger.info("Skipping master-data ingestion (already completed or disabled)")
            return True

        logger.info("Starting TNO master-data ingestion...")
        try:
            script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "load_tno_work_products.py"
            
            # Note: load_tno_work_products also loads master-data first
            cmd = [
                sys.executable,
                str(script_path),
                "--root", str(self.config.tno_root),
                "--max-concurrency", str(self.config.max_concurrency),
            ]
            if self.config.include_v110:
                cmd.append("--include-v110")

            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                self.state.errors.append(f"Master-data ingestion failed: {result.stderr}")
                logger.error(f"Master-data ingestion failed: {result.stderr}")
                return False

            # Parse results from output
            output = result.stdout + result.stderr
            self.state.master_data_ok = output.count("ok=")
            self.state.master_data_failed = output.count("failed=")

            self.state.master_data_completed = True
            logger.info(f"Master-data ingestion completed: ok={self.state.master_data_ok}, failed={self.state.master_data_failed}")
            return self.state.master_data_failed == 0

        except Exception as e:
            self.state.errors.append(f"Master-data ingestion error: {str(e)}")
            logger.exception("Master-data ingestion error")
            return False

    def ingest_work_products(self) -> bool:
        """Ingest TNO work-products via DAG (async, resumable)."""
        if self.config.skip_work_products or self.state.work_products_completed:
            logger.info("Skipping work-product ingestion (already completed or disabled)")
            return True

        logger.info("Starting TNO work-product ingestion...")
        try:
            script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "load_tno_work_products.py"
            
            cmd = [
                sys.executable,
                str(script_path),
                "--root", str(self.config.tno_root),
                "--max-concurrency", str(self.config.max_concurrency),
            ]
            if self.config.include_v110:
                cmd.append("--include-v110")

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

            if result.returncode != 0:
                self.state.errors.append(f"Work-product ingestion failed: {result.stderr}")
                logger.error(f"Work-product ingestion failed: {result.stderr}")
                return False

            # Parse results
            output = result.stdout + result.stderr
            self.state.work_products_submitted = output.count("submitted=")
            self.state.work_products_ok = output.count("ok=")
            self.state.work_products_failed = output.count("failed=")

            self.state.work_products_completed = True
            logger.info(f"Work-product ingestion completed: submitted={self.state.work_products_submitted}, ok={self.state.work_products_ok}, failed={self.state.work_products_failed}")
            return True

        except Exception as e:
            self.state.errors.append(f"Work-product ingestion error: {str(e)}")
            logger.exception("Work-product ingestion error")
            return False

    def run(self) -> bool:
        """Execute full TNO ingestion pipeline."""
        logger.info("=" * 60)
        logger.info("TNO DATASET INGESTION STARTED")
        logger.info("=" * 60)

        try:
            # Run ingestion pipeline
            generate_ok = self.generate_master_data()
            master_ok = self.ingest_master_data()
            workproduct_ok = self.ingest_work_products()

            # Save checkpoint
            self.state.to_file(self.checkpoint_path)

            # Summary
            logger.info("=" * 60)
            logger.info("TNO DATASET INGESTION SUMMARY")
            logger.info("=" * 60)
            logger.info(f"Generation:     {'✓' if generate_ok else '✗'} ({self.state.generate_ok} manifests)")
            logger.info(f"Master-Data:    {'✓' if master_ok else '✗'} ({self.state.master_data_ok} ok, {self.state.master_data_failed} failed)")
            logger.info(f"Work-Products:  {'✓' if workproduct_ok else '✗'} ({self.state.work_products_submitted} submitted)")
            
            if self.state.errors:
                logger.error("Errors encountered:")
                for err in self.state.errors:
                    logger.error(f"  - {err}")

            overall_ok = generate_ok and master_ok and workproduct_ok
            logger.info("=" * 60)
            logger.info(f"OVERALL STATUS: {'✓ COMPLETE' if overall_ok else '✗ FAILED'}")
            logger.info("=" * 60)

            return overall_ok

        except Exception as e:
            logger.exception("Fatal TNO ingestion error")
            self.state.errors.append(f"Fatal error: {str(e)}")
            self.state.to_file(self.checkpoint_path)
            return False
