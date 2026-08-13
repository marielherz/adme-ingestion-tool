"""Wellbore DDMS batch loader for Volve data."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Volve Wellbore source data - extracted from core metadata
VOLVE_WELLBORES = [
    {"source_id": "NPD-2043", "name": "30/9-13"},
    {"source_id": "NPD-1308", "name": "30/9-16"},
    {"source_id": "NPD-3146", "name": "15/9-F-11 AH"},
    {"source_id": "NPD-3129", "name": "15/9-F-14"},
    {"source_id": "NPD-3128", "name": "15/9-F-15"},
    {"source_id": "NPD-3139", "name": "15/9-F-2 AH"},
    {"source_id": "NPD-3134", "name": "15/9-F-4"},
    {"source_id": "NPD-3135", "name": "15/9-F-5"},
    {"source_id": "NPD-3138", "name": "15/9-F-7 AH"},
    {"source_id": "NPD-3137", "name": "15/9-F-8 AH"},
    {"source_id": "NPD-3140", "name": "15/9-F-9 AH"},
    {"source_id": "NPD-3145", "name": "15/9-F-12 AH"},
    {"source_id": "NPD-1301", "name": "30/9-15"},
    {"source_id": "NPD-1300", "name": "30/9-14"},
    {"source_id": "NPD-1299", "name": "30/9-13 A"},
    {"source_id": "NPD-1297", "name": "30/9-11"},
    {"source_id": "NPD-1294", "name": "30/9-8"},
    {"source_id": "NPD-1293", "name": "30/9-7"},
    {"source_id": "NPD-1292", "name": "30/9-6"},
    {"source_id": "NPD-1289", "name": "30/9-3"},
    {"source_id": "NPD-1251", "name": "30/9-1"},
    {"source_id": "NPD-2820", "name": "30/9-21"},
    {"source_id": "NPD-3082", "name": "30/9-25"},
    {"source_id": "NPD-3084", "name": "30/9-26"},
    {"source_id": "NPD-2621", "name": "30/9-17"},
    {"source_id": "NPD-2819", "name": "30/9-20"},
    {"source_id": "NPD-3083", "name": "30/9-24"},
]

VOLVE_WELLS = [
    {"source_id": "15/9-F-15", "name": "15/9-F-15"},
    {"source_id": "30/9-1", "name": "30/9-1"},
    {"source_id": "30/9-3", "name": "30/9-3"},
    {"source_id": "30/9-6", "name": "30/9-6"},
    {"source_id": "30/9-7", "name": "30/9-7"},
    {"source_id": "30/9-8", "name": "30/9-8"},
    {"source_id": "30/9-11", "name": "30/9-11"},
    {"source_id": "30/9-13", "name": "30/9-13"},
    {"source_id": "30/9-13 A", "name": "30/9-13 A"},
    {"source_id": "30/9-14", "name": "30/9-14"},
    {"source_id": "30/9-15", "name": "30/9-15"},
]


def _build_wellbore_record(source_id: str, name: str, data_partition_id: str) -> dict:
    """Build DDMS-compatible Wellbore record."""
    return {
        "id": f"opendes:master-data--Wellbore:{source_id}",
        "kind": f"osdu:wks:master-data--Wellbore:1.1.0",
        "acl": {
            "owners": [f"data.default.owners@{data_partition_id}.dataservices.energy"],
            "viewers": [f"data.default.viewers@{data_partition_id}.dataservices.energy"],
        },
        "legal": {
            "legaltags": ["opendes-referencedata-legal"],
            "otherRelevantDataCountries": ["US"],
            "status": "compliant",
        },
        "data": {
            "ResourceSecurityClassification": f"opendes:reference-data--ResourceSecurityClassification:Public:",
            "NameAliases": [
                {
                    "AliasName": name,
                    "AliasNameTypeID": "opendes:reference-data--AliasNameType:UWBI:",
                }
            ],
        },
    }


def _build_well_record(source_id: str, name: str, data_partition_id: str) -> dict:
    """Build DDMS-compatible Well record."""
    return {
        "id": f"opendes:master-data--Well:{source_id}",
        "kind": f"osdu:wks:master-data--Well:1.1.0",
        "acl": {
            "owners": [f"data.default.owners@{data_partition_id}.dataservices.energy"],
            "viewers": [f"data.default.viewers@{data_partition_id}.dataservices.energy"],
        },
        "legal": {
            "legaltags": ["opendes-referencedata-legal"],
            "otherRelevantDataCountries": ["US"],
            "status": "compliant",
        },
        "data": {
            "ResourceSecurityClassification": f"opendes:reference-data--ResourceSecurityClassification:Public:",
            "CommonName": name,
        },
    }


def load_volve_wellbores_to_ddms(
    endpoint: str,
    token: str,
    data_partition_id: str,
) -> tuple[int, int]:
    """Load Volve Wellbore and Well records to DDMS.
    
    Returns:
        Tuple of (wellbore_count, well_count) successfully ingested.
    """
    base_url = f"{endpoint}/api/os-wellbore-ddms/ddms/v3"
    headers = {
        "Authorization": f"Bearer {token}",
        "data-partition-id": data_partition_id,
        "Content-Type": "application/json",
    }

    wellbore_count = 0
    well_count = 0

    # Ingest Wellbores
    logger.info(f"Loading {len(VOLVE_WELLBORES)} Volve Wellbores to DDMS...")
    for wellbore in VOLVE_WELLBORES:
        try:
            record = _build_wellbore_record(
                wellbore["source_id"],
                wellbore["name"],
                data_partition_id,
            )
            
            response = requests.post(
                f"{base_url}/wellbores",
                json=record,
                headers=headers,
                timeout=30,
            )

            if response.status_code in (200, 201):
                wellbore_count += 1
                logger.debug(f"✓ Wellbore {wellbore['source_id']} ingested")
            else:
                logger.warning(f"✗ Wellbore {wellbore['source_id']} failed: {response.status_code} {response.text[:100]}")

        except Exception as e:
            logger.error(f"Error ingesting wellbore {wellbore['source_id']}: {e}")

    logger.info(f"Wellbores ingested: {wellbore_count}/{len(VOLVE_WELLBORES)}")

    # Ingest Wells
    logger.info(f"Loading {len(VOLVE_WELLS)} Volve Wells to DDMS...")
    for well in VOLVE_WELLS:
        try:
            record = _build_well_record(
                well["source_id"],
                well["name"],
                data_partition_id,
            )
            
            response = requests.post(
                f"{base_url}/wells",
                json=record,
                headers=headers,
                timeout=30,
            )

            if response.status_code in (200, 201):
                well_count += 1
                logger.debug(f"✓ Well {well['source_id']} ingested")
            else:
                logger.warning(f"✗ Well {well['source_id']} failed: {response.status_code} {response.text[:100]}")

        except Exception as e:
            logger.error(f"Error ingesting well {well['source_id']}: {e}")

    logger.info(f"Wells ingested: {well_count}/{len(VOLVE_WELLS)}")

    return wellbore_count, well_count
