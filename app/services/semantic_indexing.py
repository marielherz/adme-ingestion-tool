"""
Extract descriptive text fields from Volve/TNO Well/Wellbore data
and prepare for semantic search indexing.

This is a foundation module for the ADME semantic search system.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class SemanticDocument:
    """Representation of a searchable entity with text content and metadata."""
    
    entity_id: str
    entity_type: str  # "Well", "Wellbore", "WellboreTrajectory"
    entity_kind: str  # Full OSDU kind
    
    # Descriptive text fields (for embedding)
    common_name: str
    description: Optional[str] = None
    remarks: Optional[str] = None
    
    # Structured metadata (for filtering)
    field_name: Optional[str] = None
    measured_depth: Optional[float] = None
    true_vertical_depth: Optional[float] = None
    inclination: Optional[float] = None
    operator: Optional[str] = None
    
    # Temporal
    created_at: str = ""
    
    # Embedding (computed downstream)
    embedding: Optional[list] = None
    embedding_model: str = ""
    
    def to_searchable_text(self) -> str:
        """Concatenate all text fields into a single document for embedding."""
        parts = [
            self.common_name,
            self.description or "",
            self.remarks or "",
            f"Field: {self.field_name}" if self.field_name else "",
            f"Operator: {self.operator}" if self.operator else "",
        ]
        # Join non-empty parts, deduplicate sentences
        text = " ".join(p.strip() for p in parts if p.strip())
        return text
    
    def to_dict(self):
        """Convert to dictionary for JSON/database storage."""
        return asdict(self)


class TextFieldExtractor:
    """Extract text fields from OSDU Well/Wellbore records."""
    
    @staticmethod
    def extract_from_well(well_record: dict) -> SemanticDocument:
        """Extract semantic document from a Well (master-data) record.
        
        OSDU Well has limited text fields. We extract:
        - FacilityName: Name of the facility
        - Source: Entity that produced the record
        - VersionCreationReason: Why this version was created
        """
        data = well_record.get("data", {})
        
        # Build description from available text fields
        text_parts = []
        if facility_name := data.get("FacilityName"):
            text_parts.append(f"Facility: {facility_name}")
        if source := data.get("Source"):
            text_parts.append(f"Source: {source}")
        if version_reason := data.get("VersionCreationReason"):
            text_parts.append(f"Version reason: {version_reason}")
        
        combined_description = " | ".join(text_parts) if text_parts else None
        
        return SemanticDocument(
            entity_id=well_record.get("id", "unknown"),
            entity_type="Well",
            entity_kind=well_record.get("kind", "osdu:wks:master-data--Well:1.0.0"),
            common_name=data.get("FacilityName", ""),
            description=combined_description,
            remarks=None,  # Not in standard OSDU Well schema
            field_name=None,  # Not in standard schema
            operator=None,  # Use InitialOperatorID or CurrentOperatorID if needed
            measured_depth=None,
            true_vertical_depth=None,
            inclination=None,
            created_at=well_record.get("createTime", datetime.now(timezone.utc).isoformat()),
        )
    
    @staticmethod
    def extract_from_wellbore(wellbore_record: dict) -> SemanticDocument:
        """Extract semantic document from a Wellbore (master-data) record.
        
        OSDU Wellbore text fields:
        - FacilityName: Name of the facility
        - Source: Entity that produced the record  
        - VersionCreationReason: Why this version was created
        - DrillingReasons: Array of drilling reasons (nested, may have text)
        """
        data = wellbore_record.get("data", {})
        
        # Extract drilling reasons (nested objects with descriptions)
        drilling_text_parts = []
        if drilling_reasons := data.get("DrillingReasons"):
            if isinstance(drilling_reasons, list):
                for reason in drilling_reasons:
                    # Each reason might have Description field
                    if isinstance(reason, dict):
                        if desc := reason.get("Description"):
                            drilling_text_parts.append(str(desc))
                        if reason_text := reason.get("Reason"):
                            drilling_text_parts.append(str(reason_text))
        
        # Build comprehensive description
        text_parts = []
        if facility_name := data.get("FacilityName"):
            text_parts.append(f"Facility: {facility_name}")
        if source := data.get("Source"):
            text_parts.append(f"Source: {source}")
        if version_reason := data.get("VersionCreationReason"):
            text_parts.append(f"Version reason: {version_reason}")
        if drilling_text_parts:
            text_parts.append(f"Drilling: {' '.join(drilling_text_parts)}")
        
        combined_description = " | ".join(text_parts) if text_parts else None
        
        return SemanticDocument(
            entity_id=wellbore_record.get("id", "unknown"),
            entity_type="Wellbore",
            entity_kind=wellbore_record.get("kind", "osdu:wks:master-data--Wellbore:1.0.0"),
            common_name=data.get("FacilityName", ""),
            description=combined_description,
            remarks=None,  # Not in standard OSDU schema
            field_name=None,  # Not in standard schema
            operator=None,  # Would need to resolve InitialOperatorID/CurrentOperatorID
            measured_depth=data.get("MeasuredDepth"),
            true_vertical_depth=data.get("TrueVerticalDepth"),
            inclination=data.get("Inclination"),
            created_at=wellbore_record.get("createTime", datetime.now(timezone.utc).isoformat()),
        )
    
    @staticmethod
    def extract_from_wellbore_trajectory(trajectory_record: dict) -> SemanticDocument:
        """Extract semantic document from a WellboreTrajectory (work product) record.
        
        OSDU WellboreTrajectory has rich text fields ideal for semantic search:
        - Description: Summary of the trajectory work product
        - AcquisitionRemark: Remarks about acquisition context  
        - SurveyReferenceIdentifier: Reference to source documents
        - SurveyType: Type of survey (Horizontal, Vertical, Directional)
        - SurveyToolTypeID: Equipment type (e.g., MWD, Gyro)
        - Tags: Keywords for search
        - BusinessActivities: Process/workflow context
        - SurveyStations: Nested survey data with per-station remarks
        """
        data = trajectory_record.get("data", {})
        
        # Extract remarks from survey stations if available
        survey_remarks = []
        if "SurveyStations" in data and isinstance(data["SurveyStations"], list):
            for station in data["SurveyStations"]:
                if isinstance(station, dict) and (remarks := station.get("Remarks")):
                    survey_remarks.append(str(remarks))
        
        # Build comprehensive description from all available text fields
        text_parts = []
        if desc := data.get("Description"):
            text_parts.append(desc)
        if acq := data.get("AcquisitionRemark"):
            text_parts.append(f"Acquisition: {acq}")
        if ref := data.get("SurveyReferenceIdentifier"):
            text_parts.append(f"Reference: {ref}")
        if survey_type := data.get("SurveyType"):
            text_parts.append(f"Type: {survey_type}")
        if tool_id := data.get("SurveyToolTypeID"):
            text_parts.append(f"Tool: {tool_id}")
        if tags := data.get("Tags"):
            if isinstance(tags, list):
                text_parts.append(f"Tags: {', '.join(tags)}")
        if activities := data.get("BusinessActivities"):
            if isinstance(activities, list):
                text_parts.append(f"Activities: {', '.join(activities)}")
        if survey_remarks:
            text_parts.append(f"Station remarks: {' | '.join(survey_remarks)}")
        
        combined_description = " | ".join(text_parts) if text_parts else None
        
        # Extract depth/inclination from survey stations if available
        measured_depth = None
        true_vertical_depth = None
        inclination = None
        
        if "SurveyStations" in data and isinstance(data["SurveyStations"], list) and data["SurveyStations"]:
            last_station = data["SurveyStations"][-1]  # Last station has final values
            if isinstance(last_station, dict):
                measured_depth = last_station.get("MeasuredDepth")
                true_vertical_depth = last_station.get("TrueVerticalDepth")
                inclination = last_station.get("Inclination")
        
        return SemanticDocument(
            entity_id=trajectory_record.get("id", "unknown"),
            entity_type="WellboreTrajectory",
            entity_kind=trajectory_record.get("kind", "osdu:wks:work-product-component--WellboreTrajectory:1.0.0"),
            common_name=data.get("CommonName", trajectory_record.get("id", "")),
            description=combined_description,
            remarks=None,  # Individual station remarks are included in description
            field_name=None,
            operator=None,
            measured_depth=measured_depth,
            true_vertical_depth=true_vertical_depth,
            inclination=inclination,
            created_at=trajectory_record.get("createTime", datetime.now(timezone.utc).isoformat()),
        )


class SemanticIndexBuilder:
    """Build searchable index from extracted semantic documents."""
    
    def __init__(self, output_path: Path):
        self.output_path = Path(output_path)
        self.documents = []
    
    def add_document(self, doc: SemanticDocument) -> None:
        """Add a semantic document to the index."""
        self.documents.append(doc)
    
    def add_from_jsonl(self, jsonl_path: Path, entity_type: str) -> int:
        """Load documents from JSON or JSONL file (Volve/TNO export format).
        
        Supports multiple formats:
        - JSON array: [{ ... }, { ... }]
        - JSON object with MasterData: { "MasterData": [{ ... }] } (Volve format)
        - JSONL: One JSON object per line
        """
        count = 0
        extractor = TextFieldExtractor()
        
        if not jsonl_path.exists():
            logger.warning(f"File not found: {jsonl_path}")
            return 0
        
        try:
            with open(jsonl_path) as f:
                # Detect format by reading first character
                first_char = f.read(1)
                f.seek(0)
                
                records = []
                
                if first_char == '[':
                    # JSON array format: [{ ... }, { ... }]
                    logger.info(f"Loading JSON array from {jsonl_path.name}...")
                    data = json.load(f)
                    records = data if isinstance(data, list) else [data]
                    
                elif first_char == '{':
                    # JSON object - could be Volve format or single record
                    logger.info(f"Loading JSON object from {jsonl_path.name}...")
                    data = json.load(f)
                    
                    # Check for Volve format: { "MasterData": [...] }
                    if isinstance(data, dict) and "MasterData" in data:
                        records = data["MasterData"]
                        logger.info(f"  Found MasterData with {len(records)} records")
                    elif isinstance(data, dict) and entity_type.lower() + "s" in data:
                        # Alternative format: { "Wells": [...] }, { "Wellbores": [...] }
                        key = entity_type.lower() + "s"
                        records = data[key] if isinstance(data[key], list) else [data[key]]
                        logger.info(f"  Found {key} with {len(records)} records")
                    elif isinstance(data, dict) and "data" in data:
                        # Single record format
                        records = [data]
                    else:
                        logger.warning(f"Unknown JSON object format. Keys: {list(data.keys())}")
                        records = []
                
                else:
                    # JSONL format (one JSON per line)
                    logger.info(f"Loading JSONL from {jsonl_path.name}...")
                    for line in f:
                        if line.strip():
                            try:
                                records.append(json.loads(line))
                            except json.JSONDecodeError as e:
                                logger.warning(f"Skipping invalid JSON line: {e}")
                
                # Process records
                for record in records:
                    try:
                        if entity_type == "Well":
                            doc = extractor.extract_from_well(record)
                        elif entity_type == "Wellbore":
                            doc = extractor.extract_from_wellbore(record)
                        elif entity_type == "WellboreTrajectory":
                            doc = extractor.extract_from_wellbore_trajectory(record)
                        else:
                            logger.warning(f"Unknown entity type: {entity_type}")
                            continue
                        
                        self.add_document(doc)
                        count += 1
                        
                    except (KeyError, AttributeError, TypeError) as e:
                        logger.warning(f"Error extracting from record: {e}")
        
        except Exception as e:
            logger.error(f"Error reading {jsonl_path}: {e}")
        
        logger.info(f"Loaded {count} {entity_type} records")
        return count
    
    def save_index(self, format: str = "jsonl") -> Path:
        """Save index to file (JSONL or JSON)."""
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        
        if format == "jsonl":
            output_file = self.output_path.with_suffix(".jsonl")
            with open(output_file, "w") as f:
                for doc in self.documents:
                    f.write(json.dumps(doc.to_dict()) + "\n")
        else:  # JSON
            output_file = self.output_path.with_suffix(".json")
            with open(output_file, "w") as f:
                json.dump([doc.to_dict() for doc in self.documents], f, indent=2)
        
        logger.info(f"Saved {len(self.documents)} documents to {output_file}")
        return output_file
    
    def stats(self) -> dict:
        """Return statistics about the index."""
        by_type = {}
        has_remarks = 0
        
        for doc in self.documents:
            by_type[doc.entity_type] = by_type.get(doc.entity_type, 0) + 1
            if doc.remarks:
                has_remarks += 1
        
        return {
            "total_documents": len(self.documents),
            "by_type": by_type,
            "documents_with_remarks": has_remarks,
            "avg_text_length": sum(
                len(d.to_searchable_text()) for d in self.documents
            ) / len(self.documents) if self.documents else 0,
        }


# Example usage
if __name__ == "__main__":
    import sys
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    
    print("ADME Semantic Indexing - Track A: Well/Wellbore Extraction")
    print("=" * 60)
    
    # Build index from Volve exported data (TRACK A)
    builder = SemanticIndexBuilder(
        Path.home() / "adme-ingestion-tool" / ".semantic-index.jsonl"
    )
    
    # Paths to Volve data (from your manifest files)
    volve_data_root = Path.home() / "osdu-data" / "volve" / "generated-json" / "provided" / "master-data"
    
    print(f"\n1. Extracting from Volve master-data...")
    print(f"   Source: {volve_data_root}")
    
    # Auto-discover Well files (pattern: EIQExport *_wells.json)
    well_files = list(volve_data_root.glob("Well/EIQExport*_wells.json"))
    well_count = 0
    if well_files:
        well_file = well_files[0]  # Use first match
        well_count = builder.add_from_jsonl(well_file, "Well")
        print(f"   [OK] {well_count} Wells extracted")
    else:
        print(f"   [!] No Well files found in {volve_data_root / 'Well'}")
    
    # Auto-discover Wellbore files (pattern: EIQExport *_wellbores.json)
    wellbore_files = list(volve_data_root.glob("Wellbore/EIQExport*_wellbores.json"))
    wellbore_count = 0
    if wellbore_files:
        wellbore_file = wellbore_files[0]  # Use first match
        wellbore_count = builder.add_from_jsonl(wellbore_file, "Wellbore")
        print(f"   [OK] {wellbore_count} Wellbores extracted")
    else:
        print(f"   [!] No Wellbore files found in {volve_data_root / 'Wellbore'}")
    
    # Add WellboreTrajectory (if available from Track B generator)
    trajectory_file = Path.home() / "adme-ingestion-tool" / ".wellbore-trajectories.jsonl"
    trajectory_count = 0
    if trajectory_file.exists():
        trajectory_count = builder.add_from_jsonl(trajectory_file, "WellboreTrajectory")
        print(f"   [OK] {trajectory_count} WellboreTrajectories extracted")
    else:
        print(f"   [!] Trajectory file not available (run Track B generator first)")
    
    # Save
    total = well_count + wellbore_count + trajectory_count
    if total > 0:
        print(f"\n2. Saving semantic index...")
        builder.save_index("jsonl")
        stats = builder.stats()
        print(f"\n3. Index Statistics:")
        print(f"   Total documents: {stats['total_documents']}")
        print(f"   By type: {stats['by_type']}")
        print(f"   Documents with text: {stats['documents_with_remarks']}")
        print(f"   Avg text length: {stats['avg_text_length']:.0f} chars")
        print(f"\n[SUCCESS] Ready for embedding generation (semantic_embeddings.py)")
    else:
        print("\n[ERROR] No records extracted. Check file paths.")
