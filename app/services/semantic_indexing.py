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
from datetime import datetime

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
        """Extract semantic document from a Well (master-data) record."""
        data = well_record.get("data", {})
        
        return SemanticDocument(
            entity_id=well_record.get("id", "unknown"),
            entity_type="Well",
            entity_kind=well_record.get("kind", "osdu:wks:master-data--Well:1.0.0"),
            common_name=data.get("CommonName", ""),
            description=data.get("Description", None),
            remarks=data.get("Remarks", None),
            field_name=data.get("Field", None),
            operator=data.get("Operator", None),
            measured_depth=None,
            true_vertical_depth=None,
            inclination=None,
            created_at=well_record.get("createTime", datetime.utcnow().isoformat()),
        )
    
    @staticmethod
    def extract_from_wellbore(wellbore_record: dict) -> SemanticDocument:
        """Extract semantic document from a Wellbore (master-data) record."""
        data = wellbore_record.get("data", {})
        
        # Combine all trajectory descriptions if available
        trajectory_text = ""
        if "WellboreTrajectory" in data:
            trajectory_text = f"Trajectory: {data['WellboreTrajectory']}"
        
        remarks_combined = " ".join(filter(None, [
            data.get("Remarks", ""),
            trajectory_text
        ]))
        
        return SemanticDocument(
            entity_id=wellbore_record.get("id", "unknown"),
            entity_type="Wellbore",
            entity_kind=wellbore_record.get("kind", "osdu:wks:master-data--Wellbore:1.0.0"),
            common_name=data.get("CommonName", ""),
            description=data.get("Description", None),
            remarks=remarks_combined if remarks_combined else None,
            field_name=data.get("Field", None),
            operator=data.get("Operator", None),
            measured_depth=data.get("MeasuredDepth", None),
            true_vertical_depth=data.get("TrueVerticalDepth", None),
            inclination=data.get("Inclination", None),
            created_at=wellbore_record.get("createTime", datetime.utcnow().isoformat()),
        )
    
    @staticmethod
    def extract_from_wellbore_trajectory(trajectory_record: dict) -> SemanticDocument:
        """Extract semantic document from a WellboreTrajectory record."""
        data = trajectory_record.get("data", {})
        
        # Extract remarks from survey stations if available
        survey_remarks = ""
        if "SurveyStations" in data and isinstance(data["SurveyStations"], list):
            remarks_list = [
                str(station.get("Remarks", ""))
                for station in data["SurveyStations"]
                if station.get("Remarks")
            ]
            survey_remarks = " ".join(remarks_list)
        
        remarks_combined = " ".join(filter(None, [
            data.get("Remarks", ""),
            survey_remarks
        ]))
        
        return SemanticDocument(
            entity_id=trajectory_record.get("id", "unknown"),
            entity_type="WellboreTrajectory",
            entity_kind=trajectory_record.get("kind", "osdu:wks:work-product--WellboreTrajectory:1.0.0"),
            common_name=data.get("CommonName", trajectory_record.get("id", "")),
            description=data.get("Description", None),
            remarks=remarks_combined if remarks_combined else None,
            field_name=None,
            operator=data.get("Operator", None),
            measured_depth=data.get("MeasuredDepth", None),
            true_vertical_depth=data.get("TrueVerticalDepth", None),
            inclination=data.get("Inclination", None),
            created_at=trajectory_record.get("createTime", datetime.utcnow().isoformat()),
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
        """Load documents from JSONL file (Volve/TNO export format)."""
        count = 0
        extractor = TextFieldExtractor()
        
        if not jsonl_path.exists():
            logger.warning(f"File not found: {jsonl_path}")
            return 0
        
        try:
            with open(jsonl_path) as f:
                for line_num, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    
                    try:
                        record = json.loads(line)
                        
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
                        
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning(f"Error parsing line {line_num}: {e}")
        
        except Exception as e:
            logger.error(f"Error reading {jsonl_path}: {e}")
        
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
    
    # Build index from Volve exported data
    builder = SemanticIndexBuilder(
        Path.home() / "adme-ingestion-tool" / ".semantic-index.jsonl"
    )
    
    # Paths to Volve data (from your manifest files)
    volve_data_root = Path.home() / "osdu-data" / "volve" / "generated-json"
    
    print("Building semantic index from Volve data...")
    
    # Add Wells
    well_count = builder.add_from_jsonl(
        volve_data_root / "load_Well.jsonl",
        "Well"
    )
    print(f"  + {well_count} Wells")
    
    # Add Wellbores
    wellbore_count = builder.add_from_jsonl(
        volve_data_root / "load_Wellbore.jsonl",
        "Wellbore"
    )
    print(f"  + {wellbore_count} Wellbores")
    
    # Save
    builder.save_index("jsonl")
    stats = builder.stats()
    print(f"\nIndex statistics:")
    for key, value in stats.items():
        print(f"  {key}: {value}")
