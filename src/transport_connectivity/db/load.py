"""Transactional loader for normalized transport connectivity data."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from transport_connectivity.db.migrate import get_connection_string

DEFAULT_RAW_DIR = Path("data/raw/tochno_intercity_v20260629")
DEFAULT_NORMALIZED_DIR = Path("data/normalized/tochno_intercity_v20260629")
SOURCE_ID = "tochno_intercity"
SOURCE_NAME = "Tochno Intercity Connectivity"
ALLOWED_REPORT_STATUSES = {"success", "success_with_rejections"}


@dataclass(frozen=True)
class LoadResult:
    """Summary of a load operation."""

    snapshot_id: str
    already_loaded: bool
    counts: dict[str, int]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Required file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _null_if_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _city_values(
    row: dict[str, str], snapshot_id: str
) -> tuple[str, str | None, str | None, int | None, str | None, str]:
    population = _null_if_empty(row["population"])
    return (
        row["city_id"],
        _null_if_empty(row["name"]),
        _null_if_empty(row["region"]),
        int(population) if population is not None else None,
        _make_point_ewkt(row["longitude"], row["latitude"]),
        snapshot_id,
    )


def _measurement_values(row: dict[str, str], snapshot_id: str) -> tuple[str | int | None, ...]:
    if row["snapshot_id"] != snapshot_id:
        raise ValueError("Measurement snapshot_id does not match run-report.json")
    return (
        row["measurement_id"],
        snapshot_id,
        int(row["source_row_number"]),
        row["from_city_id"],
        row["to_city_id"],
        row["transport_type"],
        _null_if_empty(row["distance_m"]),
        _null_if_empty(row["duration_sec"]),
        _null_if_empty(row["missing_reason_raw"]),
        _null_if_empty(row["source_raw"]),
        _null_if_empty(row["comment_raw"]),
    )


def _make_point_ewkt(lon: str | None, lat: str | None) -> str | None:
    lon_val = _null_if_empty(lon)
    lat_val = _null_if_empty(lat)
    if lon_val is None or lat_val is None:
        return None
    return f"SRID=4326;POINT({lon_val} {lat_val})"


def _verify_output_hashes(
    normalized_dir: Path,
    report: dict[str, Any],
) -> dict[str, Path]:
    output_meta = report.get("output") or {}
    csv_files = {
        "cities": normalized_dir / "cities.csv",
        "route_measurements": normalized_dir / "route_measurements.csv",
        "rejected_rows": normalized_dir / "rejected_rows.csv",
    }

    for key, path in csv_files.items():
        if not path.is_file():
            raise FileNotFoundError(f"Missing normalized CSV: {path}")

        expected_entry = output_meta.get(path.name) or output_meta.get(key)
        expected_sha = (
            expected_entry.get("sha256") if isinstance(expected_entry, dict) else expected_entry
        )
        if not expected_sha:
            raise ValueError(f"Missing expected sha256 for '{path.name}' in run-report.json")

        actual_sha = _sha256_file(path)
        if actual_sha.lower() != str(expected_sha).lower():
            raise ValueError(
                f"SHA-256 mismatch for {path.name}: expected {expected_sha}, got {actual_sha}"
            )

    return csv_files


def _extract_expected_counts(report: dict[str, Any]) -> dict[str, int]:
    output = report["output"]
    return {
        "city": int(output["cities.csv"]["rows"]),
        "route_measurement": int(output["route_measurements.csv"]["rows"]),
        "rejected_row": int(output["rejected_rows.csv"]["rows"]),
    }


def load_snapshot(
    raw_dir: Path = DEFAULT_RAW_DIR,
    normalized_dir: Path = DEFAULT_NORMALIZED_DIR,
) -> LoadResult:
    """Validate and load a normalized snapshot into PostgreSQL in one transaction."""
    manifest = _load_json(raw_dir / "manifest.json")
    report = _load_json(normalized_dir / "run-report.json")

    status = report.get("status")
    if status not in ALLOWED_REPORT_STATUSES:
        raise ValueError(f"Unsupported run-report status '{status}'")

    snapshot_id = manifest["snapshot_id"]
    if snapshot_id != report.get("snapshot_id"):
        raise ValueError("snapshot_id differs between manifest.json and run-report.json")
    source_sha256 = manifest.get("csv_sha256")
    if not source_sha256 or source_sha256 != report.get("input", {}).get("sha256"):
        raise ValueError("Input SHA-256 differs between manifest.json and run-report.json")

    csv_files = _verify_output_hashes(normalized_dir, report)
    expected_counts = _extract_expected_counts(report)
    record_count = manifest["row_count"]
    retrieved_at = manifest["downloaded_at_utc"]
    file_name = manifest["csv_filename"]
    if not file_name or file_name in {".", ".."} or any(char in file_name for char in "/\\:"):
        raise ValueError("manifest.json csv_filename must be a file name without a path")

    balance = report["balance"]
    service = report["service"]
    contract_version = report["contract_version"]
    code_commit = service["code_version"]
    rows_read = balance["rows_read"]
    rows_written = expected_counts["route_measurement"]
    rows_rejected = expected_counts["rejected_row"]
    if (
        rows_written != balance["route_measurements"]
        or rows_rejected != balance["rejected_rows"]
        or rows_read != rows_written + rows_rejected
        or rows_read != record_count
    ):
        raise ValueError("Row counts disagree between manifest, output metadata and balance")

    started_at = service.get("started_at_utc") or retrieved_at
    finished_at = service.get("finished_at_utc")

    with psycopg.connect(get_connection_string()) as conn:
        with conn.cursor() as cur:
            # 1. data_source
            cur.execute(
                """
                INSERT INTO data_source (source_id, name, license)
                VALUES (%s, %s, %s)
                ON CONFLICT (source_id) DO NOTHING;
                """,
                (SOURCE_ID, SOURCE_NAME, "CC BY 4.0"),
            )

            # 2. data_snapshot
            cur.execute(
                """
                INSERT INTO data_snapshot (
                    snapshot_id,
                    source_id,
                    sha256,
                    file_name,
                    record_count,
                    manifest,
                    retrieved_at
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (snapshot_id) DO NOTHING
                RETURNING snapshot_id;
                """,
                (
                    snapshot_id,
                    SOURCE_ID,
                    source_sha256,
                    file_name,
                    record_count,
                    json.dumps(manifest, ensure_ascii=False),
                    retrieved_at,
                ),
            )

            if cur.fetchone() is None:
                cur.execute(
                    "SELECT sha256 FROM data_snapshot WHERE snapshot_id = %s", (snapshot_id,)
                )
                existing = cur.fetchone()
                if existing is not None and existing[0] == source_sha256:
                    return LoadResult(snapshot_id=snapshot_id, already_loaded=True, counts={})
                raise ValueError(f"Snapshot '{snapshot_id}' already exists with different SHA-256")

            # 3. load_run
            cur.execute(
                """
                INSERT INTO load_run (
                    snapshot_id,
                    status,
                    contract_version,
                    code_commit,
                    rows_read,
                    rows_written,
                    rows_rejected,
                    started_at,
                    finished_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING load_run_id;
                """,
                (
                    snapshot_id,
                    status,
                    contract_version,
                    code_commit,
                    rows_read,
                    rows_written,
                    rows_rejected,
                    started_at,
                    finished_at,
                ),
            )
            load_run_id = cur.fetchone()[0]

            # 4. Bulk COPY: city + сбор внешних идентификаторов
            external_id_rows: list[tuple[str, str, str, str]] = []
            with csv_files["cities"].open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                with cur.copy(
                    """
                    COPY city (
                        city_id,
                        name,
                        region,
                        population,
                        location,
                        attributes_snapshot_id
                    ) FROM STDIN
                    """
                ) as copy:
                    for row in reader:
                        city_id = row["city_id"].strip()
                        oktmo = _null_if_empty(row.get("oktmo"))
                        copy.write_row(_city_values(row, snapshot_id))
                        external_id_rows.append(
                            (city_id, row["id_namespace"], row["source_city_id"], snapshot_id)
                        )
                        if oktmo is not None:
                            external_id_rows.append((city_id, "oktmo", oktmo, snapshot_id))

            # 5. Bulk COPY: city_external_id
            with cur.copy(
                """
                COPY city_external_id (
                    city_id,
                    id_namespace,
                    external_id,
                    snapshot_id
                ) FROM STDIN
                """
            ) as copy:
                for ext_row in external_id_rows:
                    copy.write_row(ext_row)

            # 6. Bulk COPY: route_measurement
            with csv_files["route_measurements"].open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                with cur.copy(
                    """
                    COPY route_measurement (
                        measurement_id,
                        snapshot_id,
                        source_row_number,
                        from_city_id,
                        to_city_id,
                        transport_type,
                        distance_m,
                        duration_sec,
                        missing_reason_raw,
                        source_raw,
                        comment_raw
                    ) FROM STDIN
                    """
                ) as copy:
                    for row in reader:
                        copy.write_row(_measurement_values(row, snapshot_id))

            # 7. Bulk COPY: rejected_row
            with csv_files["rejected_rows"].open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                with cur.copy(
                    """
                    COPY rejected_row (
                        load_run_id,
                        source_row_number,
                        reject_reason,
                        detail,
                        raw_json
                    ) FROM STDIN
                    """
                ) as copy:
                    for row in reader:
                        copy.write_row(
                            (
                                load_run_id,
                                int(row["source_row_number"]),
                                row["reject_reason"],
                                _null_if_empty(row.get("detail")),
                                row["raw_json"],
                            )
                        )

            # 8. Сверка счётчиков до коммита
            cur.execute(
                "SELECT count(*) FROM city WHERE attributes_snapshot_id = %s;",
                (snapshot_id,),
            )
            actual_city = int(cur.fetchone()[0])

            cur.execute(
                "SELECT count(*) FROM route_measurement WHERE snapshot_id = %s;",
                (snapshot_id,),
            )
            actual_routes = int(cur.fetchone()[0])

            cur.execute(
                "SELECT count(*) FROM rejected_row WHERE load_run_id = %s;",
                (load_run_id,),
            )
            actual_rejected = int(cur.fetchone()[0])

            actual_counts = {
                "city": actual_city,
                "city_external_id": len(external_id_rows),
                "route_measurement": actual_routes,
                "rejected_row": actual_rejected,
            }

            for table_name in ("city", "route_measurement", "rejected_row"):
                if actual_counts[table_name] != expected_counts[table_name]:
                    raise RuntimeError(
                        f"Row count mismatch for '{table_name}': "
                        f"expected {expected_counts[table_name]}, "
                        f"loaded {actual_counts[table_name]}"
                    )

    return LoadResult(
        snapshot_id=snapshot_id,
        already_loaded=False,
        counts=actual_counts,
    )
