"""Full dataset processor and normalizer according to the data contract."""

from __future__ import annotations

import csv
import json
import math
import subprocess
import time
import tracemalloc
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from transport_connectivity.connector import (
    EXPECTED_SOURCE_COLUMNS,
    SNAPSHOT_ID,
    atomic_write_bytes,
    sha256_file,
)

CONTRACT_VERSION = "1.0.0"
CONTRACT_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

CITIES_COLUMNS = [
    "city_id",
    "source_city_id",
    "oktmo",
    "name",
    "region",
    "latitude",
    "longitude",
    "population",
]

MEASUREMENTS_COLUMNS = [
    "measurement_id",
    "snapshot_id",
    "source_row_number",
    "from_city_id",
    "to_city_id",
    "transport_type",
    "distance_m",
    "duration_sec",
    "missing_reason_raw",
    "source_raw",
    "comment_raw",
]

REJECTED_COLUMNS = [
    "source_row_number",
    "rejection_reason",
    "rejection_detail",
    "city_from",
    "city_to",
    "sber_territory_id_from",
    "sber_territory_id_to",
    "transport_type",
    "distance_km",
    "distance_hr",
    "missing_reason",
]


class ProcessingError(RuntimeError):
    """Raised when input files are invalid or balance invariants fail."""


@dataclass(frozen=True)
class CityRecord:
    city_id: str
    source_city_id: str
    oktmo: str
    name: str
    region: str
    latitude: str
    longitude: str
    population: str

    def to_row(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class RunReport:
    snapshot_id: str
    contract_version: str
    code_commit: str
    input_csv_path: str
    input_csv_sha256: str
    started_at_utc: str
    finished_at_utc: str
    elapsed_seconds: float
    peak_memory_value: int
    peak_memory_method: str
    rows_read: int
    rows_written: int
    rows_rejected: int
    duplicates_removed: int
    balance_valid: bool
    balance_formula: str
    cities_written: int
    city_attribute_conflicts: int
    transport_counts: dict[str, int]
    missing_route_rows_written: int
    missing_reason_counts: dict[str, int]
    rejection_reason_counts: dict[str, int]
    semantic_collisions_count: int
    output_files_sha256: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_git_commit() -> str:
    """Return current short git commit SHA or 'unknown' if unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except OSError:
        pass
    return "unknown"


def make_city_ids(sber_id_raw: str, oktmo_raw: str) -> tuple[str, str] | None:
    """Build deterministic (city_id, source_city_id) preserving string representation."""
    sber = sber_id_raw.strip()
    oktmo = oktmo_raw.strip()
    if sber:
        source_city_id = f"sber:{sber}"
    elif oktmo:
        source_city_id = f"oktmo:{oktmo}"
    else:
        return None
    deterministic_uuid = uuid.uuid5(
        CONTRACT_NAMESPACE, f"transport-connectivity.city/{source_city_id}"
    )
    return f"city-{deterministic_uuid}", source_city_id


def make_measurement_id(
    snapshot_id: str, from_city_id: str, to_city_id: str, transport_type: str
) -> str:
    key = (
        f"transport-connectivity.measurement/{snapshot_id}/"
        f"{from_city_id}/{to_city_id}/{transport_type}"
    )
    return f"meas-{uuid.uuid5(CONTRACT_NAMESPACE, key)}"


def parse_coordinate(lat_raw: str, lon_raw: str) -> tuple[str, str] | None:
    try:
        lat = float(lat_raw.strip())
        lon = float(lon_raw.strip())
    except ValueError:
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return f"{lat:.7f}".rstrip("0").rstrip("."), f"{lon:.7f}".rstrip("0").rstrip(".")


def parse_population(pop_raw: str) -> str | None:
    val = pop_raw.strip()
    if not val:
        return ""
    if not val.isdigit():
        return None
    pop_int = int(val)
    if pop_int <= 0:
        return None
    return str(pop_int)


def format_metric(value: float) -> str:
    rounded = round(value, 3)
    if rounded.is_integer():
        return f"{int(rounded)}.0"
    return f"{rounded:.3f}".rstrip("0").rstrip(".")


def parse_positive_finite(raw: str) -> float | None:
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0.0:
        return None
    return value


def serialize_csv_bytes(columns: list[str], rows: list[dict[str, Any]]) -> bytes:
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=columns, delimiter=",", extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def process_dataset(
    input_csv_path: Path,
    output_dir: Path,
    *,
    manifest_path: Path | None = None,
) -> RunReport:
    """Normalize the full connectivity CSV into cities, route_measurements, and run_report."""
    if not input_csv_path.is_file():
        raise ProcessingError(f"Input CSV file does not exist: {input_csv_path}")

    snapshot_id = SNAPSHOT_ID
    input_sha256 = sha256_file(input_csv_path)

    if manifest_path is not None:
        if not manifest_path.is_file():
            raise ProcessingError(f"Manifest file does not exist: {manifest_path}")
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ProcessingError(f"Invalid manifest JSON: {error}") from error
        expected_sha = manifest_data.get("csv_sha256")
        if expected_sha and expected_sha != input_sha256:
            raise ProcessingError(
                f"Input CSV SHA-256 ({input_sha256}) does not match manifest ({expected_sha})"
            )
        if isinstance(manifest_data.get("snapshot_id"), str):
            snapshot_id = manifest_data["snapshot_id"]

    started_at_utc = datetime.now(UTC).isoformat(timespec="seconds")
    tracemalloc.start()
    t0 = time.perf_counter()

    try:
        with input_csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=";")
            columns = list(reader.fieldnames or [])
            missing_cols = [col for col in EXPECTED_SOURCE_COLUMNS if col not in columns]
            if missing_cols:
                raise ProcessingError(
                    f"Input CSV is missing required columns: {', '.join(missing_cols)}"
                )

            cities_by_id: dict[str, CityRecord] = {}
            city_conflicts = 0
            measurements_written: list[dict[str, Any]] = []
            rejected_rows: list[dict[str, Any]] = []
            seen_keys: dict[tuple[str, str, str], tuple[str, str]] = {}

            rows_read = 0
            duplicates_removed = 0
            semantic_collisions = 0
            missing_route_rows_written = 0
            transport_counts: Counter[str] = Counter()
            missing_reason_counts: Counter[str] = Counter()
            rejection_reason_counts: Counter[str] = Counter()

            def reject(row_num: int, row: dict[str, str], reason: str, detail: str) -> None:
                rejection_reason_counts[reason] += 1
                rejected_rows.append(
                    {
                        "source_row_number": row_num,
                        "rejection_reason": reason,
                        "rejection_detail": detail,
                        "city_from": (row.get("city_from") or "").strip(),
                        "city_to": (row.get("city_to") or "").strip(),
                        "sber_territory_id_from": (row.get("sber_territory_id_from") or "").strip(),
                        "sber_territory_id_to": (row.get("sber_territory_id_to") or "").strip(),
                        "transport_type": (row.get("transport_type") or "").strip(),
                        "distance_km": (row.get("distance_km") or "").strip(),
                        "distance_hr": (row.get("distance_hr") or "").strip(),
                        "missing_reason": (row.get("missing_reason") or "").strip(),
                    }
                )

            for row_num, row in enumerate(reader, start=1):
                rows_read += 1
                if None in row or any(v is None for k, v in row.items() if k is not None):
                    reject(row_num, row, "malformed_csv_row", "Column count mismatch in row")
                    continue

                ids_from = make_city_ids(
                    row.get("sber_territory_id_from", ""), row.get("oktmo_from", "")
                )
                ids_to = make_city_ids(row.get("sber_territory_id_to", ""), row.get("oktmo_to", ""))
                if ids_from is None or ids_to is None:
                    reject(row_num, row, "missing_city_id", "Missing sber_territory_id and oktmo")
                    continue

                from_city_id, from_source_id = ids_from
                to_city_id, to_source_id = ids_to
                if from_city_id == to_city_id:
                    reject(row_num, row, "self_loop", "from_city_id equals to_city_id")
                    continue

                name_from = row.get("city_from", "").strip()
                name_to = row.get("city_to", "").strip()
                if not name_from or not name_to:
                    reject(row_num, row, "missing_city_name", "Empty city_from or city_to")
                    continue

                coords_from = parse_coordinate(row.get("lat_from", ""), row.get("lon_from", ""))
                coords_to = parse_coordinate(row.get("lat_to", ""), row.get("lon_to", ""))
                if coords_from is None or coords_to is None:
                    reject(
                        row_num,
                        row,
                        "invalid_coordinates",
                        "Coordinates are non-numeric, non-finite, or out of WGS84 bounds",
                    )
                    continue

                pop_from = parse_population(row.get("population_from", ""))
                pop_to = parse_population(row.get("population_to", ""))
                if pop_from is None or pop_to is None:
                    reject(row_num, row, "invalid_population", "Population is not a positive int")
                    continue

                transport = row.get("transport_type", "").strip()
                if not transport:
                    reject(row_num, row, "missing_transport_type", "Empty transport_type")
                    continue

                dist_raw = row.get("distance_km", "").strip()
                dur_raw = row.get("distance_hr", "").strip()
                missing_reason_raw = row.get("missing_reason", "").strip()
                source_raw = row.get("source", "").strip()
                comment_raw = row.get("comment", "").strip()

                if not dist_raw and not dur_raw:
                    # Legitimate missing route only when a reason other than empty/CD is given
                    if not missing_reason_raw or missing_reason_raw == "CD":
                        reject(
                            row_num,
                            row,
                            "unexplained_missing_metrics",
                            "Both distance_km and distance_hr are empty without missing_reason",
                        )
                        continue
                    distance_m_str = ""
                    duration_sec_str = ""
                elif not dist_raw or not dur_raw:
                    reject(
                        row_num,
                        row,
                        "partial_metrics",
                        "Only one of distance_km or distance_hr is populated",
                    )
                    continue
                else:
                    dist_km = parse_positive_finite(dist_raw)
                    dur_hr = parse_positive_finite(dur_raw)
                    if dist_km is None or dur_hr is None:
                        reject(
                            row_num,
                            row,
                            "invalid_metric_value",
                            "distance_km or distance_hr is non-numeric, non-finite, or <= 0",
                        )
                        continue
                    distance_m_str = format_metric(dist_km * 1000.0)
                    duration_sec_str = format_metric(dur_hr * 3600.0)

                city_from_rec = CityRecord(
                    city_id=from_city_id,
                    source_city_id=from_source_id,
                    oktmo=row.get("oktmo_from", "").strip(),
                    name=name_from,
                    region=row.get("region_from", "").strip(),
                    latitude=coords_from[0],
                    longitude=coords_from[1],
                    population=pop_from,
                )
                city_to_rec = CityRecord(
                    city_id=to_city_id,
                    source_city_id=to_source_id,
                    oktmo=row.get("oktmo_to", "").strip(),
                    name=name_to,
                    region=row.get("region_to", "").strip(),
                    latitude=coords_to[0],
                    longitude=coords_to[1],
                    population=pop_to,
                )

                city_conflict_detected = False
                for candidate in (city_from_rec, city_to_rec):
                    existing = cities_by_id.get(candidate.city_id)
                    if existing is not None and existing != candidate:
                        city_conflicts += 1
                        city_conflict_detected = True
                        break
                if city_conflict_detected:
                    reject(
                        row_num,
                        row,
                        "city_attribute_conflict",
                        "Conflicting city attributes for the same source_city_id",
                    )
                    continue

                obs_key = (from_city_id, to_city_id, transport)
                obs_metrics = (distance_m_str, duration_sec_str)
                if obs_key in seen_keys:
                    if seen_keys[obs_key] == obs_metrics:
                        duplicates_removed += 1
                        continue
                    semantic_collisions += 1
                    reject(
                        row_num,
                        row,
                        "semantic_collision",
                        "Conflicting metrics for identical route and transport",
                    )
                    continue

                seen_keys[obs_key] = obs_metrics
                cities_by_id.setdefault(city_from_rec.city_id, city_from_rec)
                cities_by_id.setdefault(city_to_rec.city_id, city_to_rec)

                if not distance_m_str:
                    missing_route_rows_written += 1
                transport_counts[transport] += 1
                missing_reason_counts[missing_reason_raw or "<empty>"] += 1

                measurements_written.append(
                    {
                        "measurement_id": make_measurement_id(
                            snapshot_id, from_city_id, to_city_id, transport
                        ),
                        "snapshot_id": snapshot_id,
                        "source_row_number": row_num,
                        "from_city_id": from_city_id,
                        "to_city_id": to_city_id,
                        "transport_type": transport,
                        "distance_m": distance_m_str,
                        "duration_sec": duration_sec_str,
                        "missing_reason_raw": missing_reason_raw,
                        "source_raw": source_raw,
                        "comment_raw": comment_raw,
                    }
                )

        elapsed_seconds = round(time.perf_counter() - t0, 6)
        _, peak_memory_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    rows_written = len(measurements_written)
    rows_rejected = len(rejected_rows)
    balance_valid = rows_read == (rows_written + rows_rejected + duplicates_removed)
    if not balance_valid:
        raise ProcessingError(
            f"Row balance violation: rows_read={rows_read} != "
            f"rows_written={rows_written} + rows_rejected={rows_rejected} + "
            f"duplicates_removed={duplicates_removed}"
        )

    sorted_cities = [cities_by_id[cid].to_row() for cid in sorted(cities_by_id.keys())]
    measurements_written.sort(key=lambda item: int(item["source_row_number"]))
    rejected_rows.sort(key=lambda item: int(item["source_row_number"]))

    output_dir.mkdir(parents=True, exist_ok=True)
    cities_path = output_dir / "cities.csv"
    measurements_path = output_dir / "route_measurements.csv"
    rejected_path = output_dir / "rejected_rows.csv"
    report_path = output_dir / "run_report.json"

    atomic_write_bytes(cities_path, serialize_csv_bytes(CITIES_COLUMNS, sorted_cities))
    atomic_write_bytes(
        measurements_path, serialize_csv_bytes(MEASUREMENTS_COLUMNS, measurements_written)
    )
    atomic_write_bytes(rejected_path, serialize_csv_bytes(REJECTED_COLUMNS, rejected_rows))

    output_hashes = {
        "cities.csv": sha256_file(cities_path),
        "route_measurements.csv": sha256_file(measurements_path),
        "rejected_rows.csv": sha256_file(rejected_path),
    }

    finished_at_utc = datetime.now(UTC).isoformat(timespec="seconds")
    report = RunReport(
        snapshot_id=snapshot_id,
        contract_version=CONTRACT_VERSION,
        code_commit=get_git_commit(),
        input_csv_path=input_csv_path.as_posix(),
        input_csv_sha256=input_sha256,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        elapsed_seconds=elapsed_seconds,
        peak_memory_value=peak_memory_bytes,
        peak_memory_method="python_tracemalloc_bytes",
        rows_read=rows_read,
        rows_written=rows_written,
        rows_rejected=rows_rejected,
        duplicates_removed=duplicates_removed,
        balance_valid=balance_valid,
        balance_formula="rows_read == rows_written + rows_rejected + duplicates_removed",
        cities_written=len(sorted_cities),
        city_attribute_conflicts=city_conflicts,
        transport_counts=dict(sorted(transport_counts.items())),
        missing_route_rows_written=missing_route_rows_written,
        missing_reason_counts=dict(sorted(missing_reason_counts.items())),
        rejection_reason_counts=dict(sorted(rejection_reason_counts.items())),
        semantic_collisions_count=semantic_collisions,
        output_files_sha256=output_hashes,
    )

    atomic_write_bytes(
        report_path,
        (json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return report
