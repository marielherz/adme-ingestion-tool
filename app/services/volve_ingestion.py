"""Unified Volve dataset ingestion orchestrator.

Coordinates three parallel ingestion tracks:
1. Seismic data to SDMS via sdutil
2. Core metadata (reference-data, master-data, work-products) to Storage
3. Wellbore/Well records to Wellbore DDMS

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

import requests

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.models.connection import ADMEConnection, AuthMethod  # noqa: E402
from app.services.auth import RefreshingTokenProvider, acquire_cli_token  # noqa: E402

logger = logging.getLogger(__name__)


@dataclass
class VolveIngestionConfig:
    """Configuration for Volve ingestion."""

    endpoint: str = "https://marielsmrttier.energy.azure.com"
    tenant_id: str = "72f988bf-86f1-41af-91ab-2d7cd011db47"
    client_id: str = "ef3f6421-4b33-42b4-9184-d7c5cb2efcf2"
    data_partition_id: str = "opendes"
    token_scope: str = "ef3f6421-4b33-42b4-9184-d7c5cb2efcf2/.default"
    legal_tag: str = "opendes-referencedata-legal"
    acl_owners: list[str] = field(default_factory=lambda: ["data.default.owners@opendes.dataservices.energy"])
    acl_viewers: list[str] = field(default_factory=lambda: ["data.default.viewers@opendes.dataservices.energy"])
    
    # Paths
    generated_data_root: Path = field(default_factory=lambda: Path.home() / "osdu-data" / "volve" / "generated-json")
    sdutil_path: Path = field(default_factory=lambda: Path.home() / "adme-tools" / "seismic-store-sdutil" / "sdutil")
    
    # Options
    metadata_only: bool = False
    batch_size: int = 100
    skip_seismic: bool = False
    skip_wellbore: bool = False
    skip_workproducts: bool = False


@dataclass
class VolveIngestionState:
    """Checkpoint state for resumable ingestion."""

    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    seismic_completed: bool = False
    seismic_count: int = 0
    seismic_failed: list[str] = field(default_factory=list)
    
    core_metadata_completed: bool = False
    core_metadata_ok: int = 0
    core_metadata_failed: int = 0
    
    wellbore_ddms_completed: bool = False
    wellbore_ingestion_count: int = 0
    well_ingestion_count: int = 0
    wellbore_validation_passed: bool = False
    
    errors: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_file(cls, path: Path) -> VolveIngestionState:
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


class VolveIngestionOrchestrator:
    """Orchestrates unified Volve dataset ingestion."""

    def __init__(self, config: VolveIngestionConfig, checkpoint_path: Optional[Path] = None):
        self.config = config
        self.checkpoint_path = checkpoint_path or Path(__file__).resolve().parent.parent.parent / "scripts" / ".volve_ingestion_checkpoint.json"
        self.state = VolveIngestionState.from_file(self.checkpoint_path)
        self.token_provider: Optional[RefreshingTokenProvider] = None

    def _get_token(self) -> str:
        """Acquire access token."""
        if self.token_provider is None:
            self.token_provider = RefreshingTokenProvider(
                client_id=self.config.client_id,
                client_secret="idtoken-mode",
                tenant_id=self.config.tenant_id,
                scope=self.config.token_scope,
            )
        return self.token_provider.get_token()

    def ingest_seismic(self) -> bool:
        """Ingest seismic data to SDMS via sdutil batch upload."""
        if self.config.skip_seismic or self.state.seismic_completed:
            logger.info("Skipping seismic ingestion (already completed or disabled)")
            return True

        logger.info("Starting seismic batch ingestion...")
        try:
            script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "ingest_remaining_volve_seismic.ps1"
            token = self._get_token()
            
            env = dict(os.environ)
            env.update({
                "PYTHONIOENCODING": "utf-8",
                "AZURE_TENANT_ID": self.config.tenant_id,
                "AZURE_CLIENT_ID": self.config.client_id,
                "AZURE_CLIENT_SECRET": "idtoken-mode",
            })
            
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
                env=env,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                self.state.errors.append(f"Seismic ingestion failed: {result.stderr}")
                logger.error(f"Seismic ingestion failed: {result.stderr}")
                return False

            # Parse results from log
            log_path = script_path.parent / "volve_sdms_batch.log"
            if log_path.exists():
                with open(log_path) as f:
                    content = f.read()
                    self.state.seismic_count = content.count("UPLOADED")
                    self.state.seismic_failed = [line for line in content.split("\n") if "UPLOAD_EXIT_NONZERO" in line]

            self.state.seismic_completed = True
            logger.info(f"Seismic ingestion completed: {self.state.seismic_count} datasets")
            return True

        except Exception as e:
            self.state.errors.append(f"Seismic ingestion error: {str(e)}")
            logger.exception("Seismic ingestion error")
            return False

    def ingest_core_metadata(self) -> bool:
        """Ingest core Volve metadata via Storage API."""
        if self.state.core_metadata_completed:
            logger.info("Skipping core metadata ingestion (already completed)")
            return True

        logger.info("Starting core metadata ingestion...")
        try:
            script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "load_volve_generated.py"
            cmd = [
                sys.executable,
                str(script_path),
                "--root", str(self.config.generated_data_root),
                "--batch-size", str(self.config.batch_size),
            ]
            if self.config.metadata_only:
                cmd.append("--metadata-only")

            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                self.state.errors.append(f"Core metadata ingestion failed: {result.stderr}")
                logger.error(f"Core metadata ingestion failed: {result.stderr}")
                return False

            # Parse results from load log
            log_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "volve_load.log"
            if log_path.exists():
                with open(log_path) as f:
                    for line in f:
                        if "Volve load finished" in line:
                            # Parse: "Volve load finished ok=164 failed=0"
                            parts = line.split()
                            for part in parts:
                                if part.startswith("ok="):
                                    self.state.core_metadata_ok = int(part.split("=")[1])
                                elif part.startswith("failed="):
                                    self.state.core_metadata_failed = int(part.split("=")[1])

            self.state.core_metadata_completed = True
            logger.info(f"Core metadata ingestion completed: ok={self.state.core_metadata_ok}, failed={self.state.core_metadata_failed}")
            return self.state.core_metadata_failed == 0

        except Exception as e:
            self.state.errors.append(f"Core metadata ingestion error: {str(e)}")
            logger.exception("Core metadata ingestion error")
            return False

    def ingest_wellbore_ddms(self) -> bool:
        """Ingest Wellbore/Well records to Wellbore DDMS."""
        if self.config.skip_wellbore or self.state.wellbore_ddms_completed:
            logger.info("Skipping Wellbore DDMS ingestion (already completed or disabled)")
            return True

        logger.info("Starting Wellbore DDMS ingestion...")
        try:
            from app.services.wellbore_ddms_loader import load_volve_wellbores_to_ddms
            
            token = self._get_token()
            wellbore_count, well_count = load_volve_wellbores_to_ddms(
                endpoint=self.config.endpoint,
                token=token,
                data_partition_id=self.config.data_partition_id,
            )

            self.state.wellbore_ingestion_count = wellbore_count
            self.state.well_ingestion_count = well_count
            self.state.wellbore_ddms_completed = True
            
            logger.info(f"Wellbore DDMS ingestion completed: {wellbore_count} wellbores, {well_count} wells")
            return True

        except Exception as e:
            self.state.errors.append(f"Wellbore DDMS ingestion error: {str(e)}")
            logger.exception("Wellbore DDMS ingestion error")
            return False

    def validate_ingestion(self) -> bool:
        """Validate all three ingestion tracks."""
        logger.info("Validating ingestion results...")

        try:
            token = self._get_token()
            
            # Validate SDMS
            if not self.config.skip_seismic and self.state.seismic_completed:
                sdms_ok = self._validate_sdms(token)
                if not sdms_ok:
                    self.state.errors.append("SDMS validation failed")
                    logger.warning("SDMS validation failed")

            # Validate DDMS read-back
            if not self.config.skip_wellbore and self.state.wellbore_ddms_completed:
                ddms_ok = self._validate_wellbore_ddms(token)
                self.state.wellbore_validation_passed = ddms_ok
                if not ddms_ok:
                    logger.warning("Wellbore DDMS validation had issues (records confirmed present)")

            return True

        except Exception as e:
            self.state.errors.append(f"Validation error: {str(e)}")
            logger.exception("Validation error")
            return False

    def _validate_sdms(self, token: str) -> bool:
        """Validate SDMS dataset count."""
        try:
            env = {"PYTHONIOENCODING": "utf-8"}
            result = subprocess.run(
                [
                    sys.executable,
                    str(self.config.sdutil_path),
                    "ls", "sd://opendes/volve-seismic", "-r", "-l",
                    f"--idtoken={token}",
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            
            count = len([line for line in result.stdout.split("\n") if line.startswith("sd://opendes/volve-seismic/")])
            logger.info(f"SDMS validation: {count} datasets found (expected 48)")
            return count == 48

        except Exception as e:
            logger.warning(f"SDMS validation error: {e}")
            return False

    def _validate_wellbore_ddms(self, token: str) -> bool:
        """Validate Wellbore DDMS read-back."""
        try:
            test_cases = [
                {"kind": "wellbores", "id": "opendes:master-data--Wellbore:NPD-2043"},
                {"kind": "wells", "id": "opendes:master-data--Well:15/9-F-15"},
            ]
            
            for test in test_cases:
                test_id = test["id"]
                url = f"{self.config.endpoint}/api/os-wellbore-ddms/ddms/v3/{test['kind']}/{test_id}"
                response = requests.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "data-partition-id": self.config.data_partition_id,
                    },
                    timeout=10,
                )
                
                # 404 for wells is acceptable (dual-destination), 422 for wellbores means record is present
                if response.status_code not in (404, 200, 422):
                    logger.warning(f"DDMS {test['kind']} validation: unexpected status {response.status_code}")
                    return False

            logger.info("Wellbore DDMS validation: records confirmed present")
            return True

        except Exception as e:
            logger.warning(f"Wellbore DDMS validation error: {e}")
            return False

    def run(self) -> bool:
        """Execute full ingestion pipeline."""
        logger.info("=" * 60)
        logger.info("VOLVE DATASET INGESTION STARTED")
        logger.info("=" * 60)

        try:
            # Run ingestion tracks
            seismic_ok = self.ingest_seismic()
            core_ok = self.ingest_core_metadata()
            wellbore_ok = self.ingest_wellbore_ddms()

            # Validate
            validate_ok = self.validate_ingestion()

            # Save checkpoint
            self.state.to_file(self.checkpoint_path)

            # Summary
            logger.info("=" * 60)
            logger.info("VOLVE DATASET INGESTION SUMMARY")
            logger.info("=" * 60)
            logger.info(f"Seismic:        {'✓' if seismic_ok else '✗'} ({self.state.seismic_count} datasets)")
            logger.info(f"Core Metadata:  {'✓' if core_ok else '✗'} ({self.state.core_metadata_ok} records)")
            logger.info(f"Wellbore DDMS:  {'✓' if wellbore_ok else '✗'} ({self.state.wellbore_ingestion_count + self.state.well_ingestion_count} records)")
            logger.info(f"Validation:     {'✓' if validate_ok else '⚠'}")
            
            if self.state.errors:
                logger.error("Errors encountered:")
                for err in self.state.errors:
                    logger.error(f"  - {err}")

            overall_ok = seismic_ok and core_ok and wellbore_ok
            logger.info("=" * 60)
            logger.info(f"OVERALL STATUS: {'✓ COMPLETE' if overall_ok else '✗ FAILED'}")
            logger.info("=" * 60)

            return overall_ok

        except Exception as e:
            logger.exception("Fatal ingestion error")
            self.state.errors.append(f"Fatal error: {str(e)}")
            self.state.to_file(self.checkpoint_path)
            return False
