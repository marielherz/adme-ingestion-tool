"""
Generate OSDU WellboreTrajectory manifests from Volve CSV trajectory data.

This bridges Volve's raw CSV trajectory data to OSDU-compliant JSON records
suitable for ingestion and semantic search.

Track B (parallel with semantic indexing): Enrich trajectory data with OSDU schema.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class SurveyStation:
    """Represents a single survey station in a wellbore trajectory."""
    
    measured_depth: float  # MD in feet or meters
    true_vertical_depth: Optional[float] = None  # TVD
    inclination: Optional[float] = None  # Angle in degrees
    azimuth: Optional[float] = None  # Direction in degrees
    dogleg_severity: Optional[float] = None
    tool_face: Optional[float] = None
    remarks: Optional[str] = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "MeasuredDepth": self.measured_depth,
            "TrueVerticalDepth": self.true_vertical_depth,
            "Inclination": self.inclination,
            "Azimuth": self.azimuth,
            "DoglegSeverity": self.dogleg_severity,
            "ToolFace": self.tool_face,
            "Remarks": self.remarks,
        }


@dataclass
class WellboreTrajectoryRecord:
    """OSDU-compliant WellboreTrajectory record."""
    
    entity_id: str
    wellbore_id: str  # Reference to parent Wellbore
    common_name: str
    description: Optional[str] = None
    acquisition_remark: Optional[str] = None
    survey_reference_identifier: Optional[str] = None
    survey_type: str = "DirectionalSurvey"  # Horizontal, Vertical, Directional
    survey_tool_type_id: Optional[str] = None  # e.g., "MWD", "Gyro"
    survey_version: Optional[str] = None
    tags: list[str] = None
    business_activities: list[str] = None
    survey_stations: list[SurveyStation] = None
    
    # OSDU system metadata
    kind: str = "osdu:wks:work-product-component--WellboreTrajectory:1.0.0"
    created_time: str = ""
    
    def __post_init__(self):
        """Set defaults."""
        if self.tags is None:
            self.tags = ["trajectory", "survey", "wellbore"]
        if self.business_activities is None:
            self.business_activities = ["well-planning", "drilling"]
        if self.survey_stations is None:
            self.survey_stations = []
        if not self.created_time:
            self.created_time = datetime.utcnow().isoformat() + "Z"
    
    def to_osdu_record(self) -> dict:
        """Convert to full OSDU record for ingestion."""
        return {
            "id": self.entity_id,
            "kind": self.kind,
            "acl": {
                "owners": ["data.datalake.owners@opendes.opengroup.org"],
                "viewers": ["data.datalake.viewers@opendes.opengroup.org"],
            },
            "legal": {
                "legaltags": ["opendes-US-dataset"],
                "otherRelevantDataCountries": ["US"],
                "status": "compliant",
            },
            "tags": {},
            "createTime": self.created_time,
            "createUser": "semantic-search-generator",
            "modifyTime": self.created_time,
            "modifyUser": "semantic-search-generator",
            "meta": [],
            "data": {
                "CommonName": self.common_name,
                "Description": self.description,
                "AcquisitionRemark": self.acquisition_remark,
                "SurveyReferenceIdentifier": self.survey_reference_identifier,
                "SurveyType": self.survey_type,
                "SurveyToolTypeID": self.survey_tool_type_id,
                "SurveyVersion": self.survey_version,
                "Tags": self.tags,
                "BusinessActivities": self.business_activities,
                "SurveyStations": [s.to_dict() for s in self.survey_stations] if self.survey_stations else [],
                # Reference to parent wellbore (would be populated from wellbore linkage)
                "WellboreID": self.wellbore_id,
            },
        }


class TrajectoryCSVParser:
    """Parse Volve CSV trajectory data and convert to WellboreTrajectory records."""
    
    def __init__(self, csv_file: Path, wellbore_id_prefix: str = ""):
        """Initialize parser.
        
        Args:
            csv_file: Path to trajectory CSV file
            wellbore_id_prefix: Prefix for wellbore ID (e.g., VOLVE-01-A)
        """
        self.csv_file = Path(csv_file)
        self.wellbore_id_prefix = wellbore_id_prefix
        self.survey_stations = []
    
    def parse(self) -> list[SurveyStation]:
        """Parse CSV file and extract survey stations.
        
        Expected CSV columns (case-insensitive):
        - MD, MeasuredDepth: Measured depth
        - TVD, TrueVerticalDepth: True vertical depth  
        - INC, Inclination: Wellbore inclination
        - AZ, Azimuth: Wellbore azimuth
        - DLS, DoglegSeverity: Dogleg severity
        - TF, ToolFace: Tool face angle
        - Remarks: Any survey remarks
        """
        if not self.csv_file.exists():
            logger.warning(f"CSV file not found: {self.csv_file}")
            return []
        
        self.survey_stations = []
        
        try:
            with open(self.csv_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames:
                    logger.warning(f"Empty CSV file: {self.csv_file}")
                    return []
                
                # Normalize header names (case-insensitive)
                normalized_headers = {name.lower(): name for name in reader.fieldnames}
                
                for row_num, row in enumerate(reader, 2):  # Skip header
                    try:
                        # Extract values (handle multiple possible column names)
                        md = self._get_float(row, normalized_headers, ['md', 'measureddepth'])
                        if md is None:
                            logger.warning(f"Row {row_num}: Missing measured depth, skipping")
                            continue
                        
                        tvd = self._get_float(row, normalized_headers, ['tvd', 'truevertdicaldepth'])
                        inc = self._get_float(row, normalized_headers, ['inc', 'inclination'])
                        az = self._get_float(row, normalized_headers, ['az', 'azimuth'])
                        dls = self._get_float(row, normalized_headers, ['dls', 'doglegseverity'])
                        tf = self._get_float(row, normalized_headers, ['tf', 'toolface'])
                        remarks = self._get_string(row, normalized_headers, ['remarks', 'notes', 'comments'])
                        
                        station = SurveyStation(
                            measured_depth=md,
                            true_vertical_depth=tvd,
                            inclination=inc,
                            azimuth=az,
                            dogleg_severity=dls,
                            tool_face=tf,
                            remarks=remarks,
                        )
                        self.survey_stations.append(station)
                        
                    except (ValueError, KeyError) as e:
                        logger.warning(f"Row {row_num}: Error parsing - {e}")
                        continue
            
            logger.info(f"Parsed {len(self.survey_stations)} survey stations from {self.csv_file}")
            return self.survey_stations
        
        except Exception as e:
            logger.error(f"Error reading CSV: {e}")
            return []
    
    @staticmethod
    def _get_float(row: dict, headers: dict, possible_names: list[str]) -> Optional[float]:
        """Extract float value from row, trying multiple column name variants."""
        for name in possible_names:
            if name in headers:
                actual_col = headers[name]
                try:
                    val = row.get(actual_col, "").strip()
                    return float(val) if val else None
                except (ValueError, TypeError):
                    return None
        return None
    
    @staticmethod
    def _get_string(row: dict, headers: dict, possible_names: list[str]) -> Optional[str]:
        """Extract string value from row, trying multiple column name variants."""
        for name in possible_names:
            if name in headers:
                actual_col = headers[name]
                val = row.get(actual_col, "").strip()
                return val if val else None
        return None


class TrajectoryManifestGenerator:
    """Generate OSDU WellboreTrajectory manifests from parsed CSV data."""
    
    def __init__(self, output_file: Path):
        """Initialize generator.
        
        Args:
            output_file: Path to output JSONL manifest file
        """
        self.output_file = Path(output_file)
        self.records = []
    
    def generate_from_csv(self, csv_file: Path, wellbore_id: str, common_name: str = "") -> WellboreTrajectoryRecord:
        """Generate a WellboreTrajectory record from CSV data.
        
        Args:
            csv_file: Path to trajectory CSV
            wellbore_id: Parent wellbore ID (OSDU format)
            common_name: Human-readable name for this trajectory
            
        Returns:
            WellboreTrajectoryRecord with parsed survey data
        """
        # Parse CSV
        parser = TrajectoryCSVParser(csv_file, wellbore_id)
        survey_stations = parser.parse()
        
        if not survey_stations:
            logger.warning(f"No survey stations parsed from {csv_file}")
            return None
        
        # Generate OSDU ID
        entity_id = f"opendes:work-product-component--WellboreTrajectory:{uuid4()}:1"
        
        # Create record
        record = WellboreTrajectoryRecord(
            entity_id=entity_id,
            wellbore_id=wellbore_id,
            common_name=common_name or f"Trajectory-{len(survey_stations)}-stations",
            description=f"Wellbore trajectory with {len(survey_stations)} survey stations",
            acquisition_remark=f"Auto-generated from CSV: {csv_file.name}",
            survey_reference_identifier=csv_file.stem,
            survey_type="DirectionalSurvey" if any(s.inclination for s in survey_stations) else "VerticalSurvey",
            survey_tool_type_id="CSV-Source",
            survey_version="1.0",
            tags=["trajectory", "survey", "volve", "directional"],
            business_activities=["well-planning", "drilling", "well-analysis"],
            survey_stations=survey_stations,
        )
        
        self.records.append(record)
        return record
    
    def save_manifest(self, format: str = "jsonl") -> Path:
        """Save trajectory records to manifest file.
        
        Args:
            format: "jsonl" or "json"
            
        Returns:
            Path to saved manifest file
        """
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        
        if format == "jsonl":
            output_file = self.output_file.with_suffix(".jsonl")
            with open(output_file, "w") as f:
                for record in self.records:
                    f.write(json.dumps(record.to_osdu_record()) + "\n")
        else:  # JSON
            output_file = self.output_file.with_suffix(".json")
            with open(output_file, "w") as f:
                json.dump([r.to_osdu_record() for r in self.records], f, indent=2)
        
        logger.info(f"Saved {len(self.records)} trajectory records to {output_file}")
        return output_file
    
    def stats(self) -> dict:
        """Return statistics about generated manifests."""
        total_stations = sum(len(r.survey_stations) for r in self.records)
        
        return {
            "total_trajectories": len(self.records),
            "total_survey_stations": total_stations,
            "avg_stations_per_trajectory": total_stations / len(self.records) if self.records else 0,
        }


# Example usage
if __name__ == "__main__":
    import sys
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    
    print("Wellbore Trajectory Manifest Generator")
    print("=====================================\n")
    
    # Locate Volve trajectory CSV
    volve_traj_root = Path.home() / "osdu-data" / "volve" / "Volve" / "work-products" / "trajectories_1_1_0" / "inputdata"
    
    print(f"Searching for trajectory data in: {volve_traj_root}")
    
    csv_files = list(volve_traj_root.glob("*.csv"))
    if not csv_files:
        print(f"✗ No CSV trajectory files found in {volve_traj_root}")
        sys.exit(1)
    
    print(f"Found {len(csv_files)} CSV file(s):")
    for csv_file in csv_files:
        print(f"  - {csv_file.name}")
    
    # Generate manifests
    generator = TrajectoryManifestGenerator(
        Path.home() / "adme-ingestion-tool" / ".wellbore-trajectories.jsonl"
    )
    
    print("\nGenerating trajectory manifests...")
    
    for csv_file in csv_files:
        # Extract wellbore ID from filename (e.g., NPD-3145.csv → VOLVE-NPD-3145)
        wellbore_name = csv_file.stem  # "NPD-3145"
        wellbore_id = f"opendes:master-data--Wellbore:{wellbore_name}:1"
        
        record = generator.generate_from_csv(
            csv_file,
            wellbore_id=wellbore_id,
            common_name=f"Trajectory-{wellbore_name}"
        )
        
        if record:
            print(f"  ✓ {csv_file.name} → {len(record.survey_stations)} stations")
        else:
            print(f"  ✗ Failed to generate from {csv_file.name}")
    
    # Save
    if generator.records:
        generator.save_manifest("jsonl")
        stats = generator.stats()
        print(f"\nManifest Statistics:")
        for key, value in stats.items():
            print(f"  {key}: {value}")
        print(f"\n✓ Ready for semantic indexing with WellboreTrajectory records")
    else:
        print("\n✗ No trajectory records generated")
