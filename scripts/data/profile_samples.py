"""Profile generated sample files and assemble the small instructor handoff."""

from __future__ import annotations

import csv
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "data" / "samples"
DELIVERABLE = ROOT / "deliverables" / "m0" / "data-preview"


def infer_type(values: list[str]) -> str:
    present = [value for value in values if value not in ("", "null", "None")]
    if not present:
        return "empty"
    try:
        for value in present:
            int(value)
        return "integer"
    except ValueError:
        pass
    try:
        for value in present:
            float(value)
        return "number"
    except ValueError:
        return "string"


def profile_csv(path: Path) -> dict[str, Any]:
    delimiter = ";" if path.name == "intercity_connectivity_sample.csv" else ","
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, delimiter=delimiter)
        records = list(reader)
        columns = reader.fieldnames or []
    missing = {
        column: sum(not row.get(column, "").strip() for row in records) for column in columns
    }
    types = {column: infer_type([row.get(column, "") for row in records]) for column in columns}
    duplicates = len(records) - len({tuple(row.get(col, "") for col in columns) for row in records})
    numeric_ranges: dict[str, dict[str, float | None]] = {}
    for column, kind in types.items():
        if kind in {"integer", "number"}:
            values = [float(row[column]) for row in records if row.get(column, "")]
            numeric_ranges[column] = {
                "min": min(values) if values else None,
                "max": max(values) if values else None,
            }
    details: dict[str, Any] = {
        "rows": len(records),
        "columns_count": len(columns),
        "columns": columns,
        "inferred_types": types,
        "missing_values": missing,
        "duplicate_rows": duplicates,
        "file_size_bytes": path.stat().st_size,
        "numeric_ranges": numeric_ranges,
        "delimiter": delimiter,
        "encoding": "UTF-8",
    }
    if path.name == "cities.csv":
        invalid = []
        for row in records:
            try:
                latitude, longitude = float(row["latitude"]), float(row["longitude"])
                if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                    invalid.append(row["name"])
            except (ValueError, KeyError):
                invalid.append(row.get("name", "unknown"))
        details["coordinate_validation"] = {"invalid_rows": invalid, "valid": not invalid}
        details["unique_city_ids"] = len({row["city_id"] for row in records})
    if path.name == "route_measurements.csv":
        ids = {
            row["city_id"]
            for row in csv.DictReader((SAMPLES / "cities.csv").open(encoding="utf-8"))
        }
        unknown = sorted(
            {
                city_id
                for row in records
                for city_id in (row["from_city_id"], row["to_city_id"])
                if city_id not in ids
            }
        )
        details["foreign_city_ids"] = unknown
        details["non_positive_distance_rows"] = sum(
            bool(row["distance_m"]) and float(row["distance_m"]) <= 0 for row in records
        )
        details["non_positive_duration_rows"] = sum(
            bool(row["duration_sec"]) and float(row["duration_sec"]) <= 0 for row in records
        )
    if path.name == "intercity_connectivity_sample.csv":
        details["unique_transport_types"] = dict(Counter(row["transport_type"] for row in records))
        details["missing_route_rows"] = sum(not row["distance_km"] for row in records)
        details["missing_route_reasons"] = dict(
            Counter(row["missing_reason"] for row in records if not row["distance_km"])
        )
        details["coordinate_validation"] = {
            "invalid_coordinate_pairs": sum(
                not (
                    -90 <= float(row["lat_from"]) <= 90
                    and -180 <= float(row["lon_from"]) <= 180
                    and -90 <= float(row["lat_to"]) <= 90
                    and -180 <= float(row["lon_to"]) <= 180
                )
                for row in records
                if all(row[key] for key in ("lat_from", "lon_from", "lat_to", "lon_to"))
            )
        }
    return details


def profile_geojson(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    features = document.get("features", [])
    invalid = []
    for feature in features:
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "LineString" or len(geometry.get("coordinates", [])) < 2:
            invalid.append(feature.get("id"))
        for longitude, latitude in geometry.get("coordinates", []):
            if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
                invalid.append(feature.get("id"))
                break
    properties = sorted({key for feature in features for key in feature.get("properties", {})})
    return {
        "rows": len(features),
        "columns_count": len(properties),
        "properties": properties,
        "missing_values_by_property": {
            key: sum(feature.get("properties", {}).get(key) in (None, "") for feature in features)
            for key in properties
        },
        "duplicate_osm_way_ids": len(features)
        - len({feature.get("properties", {}).get("osm_way_id") for feature in features}),
        "invalid_geometry_features": invalid,
        "osm_base_timestamp": document.get("osm_base_timestamp"),
        "file_size_bytes": path.stat().st_size,
        "encoding": "UTF-8",
    }


def main() -> None:
    report: dict[str, Any] = {}
    for path in sorted(SAMPLES.iterdir()):
        if path.name in {"data_profile.json", "extraction_metadata.json", "README.md"}:
            continue
        if path.suffix == ".csv":
            report[path.name] = profile_csv(path)
        elif path.suffix == ".geojson":
            report[path.name] = profile_geojson(path)
        elif path.suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            report[path.name] = {
                "rows": len(data.get("records", [])),
                "top_level_fields": list(data),
                "status": data.get("status"),
                "file_size_bytes": path.stat().st_size,
                "encoding": "UTF-8",
            }
    (SAMPLES / "data_profile.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    DELIVERABLE.mkdir(parents=True, exist_ok=True)
    for path in SAMPLES.iterdir():
        if path.is_file() and path.name != "README.md":
            shutil.copy2(path, DELIVERABLE / path.name)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
