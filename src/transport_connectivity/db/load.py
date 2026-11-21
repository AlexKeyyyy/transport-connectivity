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

DEFAULT_RAW_DIR = Path("data/raw/intercity-20260629")
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


def _load_run_report(normalized_dir: Path) -> dict[str, Any]:
    for filename in ("run_report.json", "run-report.json"):
        candidate = normalized_dir / filename
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise FileNotFoundError(
        f"Required run report not found in {normalized_dir} "
        "(expected run_report.json or run-report.json)"
    )


def _null_if_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _strip_prefix(value: str, prefix: str) -> str:
    return value[len(prefix) :] if value.startswith(prefix) else value


def _parse_bool(value: str | None, default: bool = False) -> bool:
    cleaned = _null_if_empty(value)
    if cleaned is None:
        return default
    return cleaned.lower() in {"1", "true", "t", "yes", "y"}


def _make_point_ewkt(lon: str | None, lat: str | None) -> str | None:
    lon_val = _null_if_empty(lon)
    lat_val = _null_if_empty(lat)
    if lon_val is None or lat_val is None:
        return None
    return f"SRID=4326;POINT({lon_val} {lat_val})"


def _resolve_report_status(report: dict[str, Any]) -> str:
    raw_status = report.get("status")
    if raw_status:
        return str(raw_status)
    if report.get("balance_valid") is False:
        return "failed"
    if int(report.get("rows_rejected", 0)) > 0:
        return "success_with_rejections"
    return "success"


def _verify_output_hashes(
    normalized_dir: Path,
    report: dict[str, Any],
) -> dict[str, Path]:
    output_meta = report.get("output_files_sha256") or report.get("output") or {}
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
            raise ValueError(f"Missing expected sha256 for '{key}' in run report")

        actual_sha = _sha256_file(path)
        if actual_sha.lower() != str(expected_sha).lower():
            raise ValueError(
                f"SHA-256 mismatch for {path.name}: expected {expected_sha}, got {actual_sha}"
            )

    return csv_files


def _extract_expected_counts(
    report: dict[str, Any],
    csv_files: dict[str, Path],
) -> dict[str, int]:
    counts = report.get("counts") or report.get("stats") or report

    if "cities_written" in counts:
        city_expected = int(counts["cities_written"])
    elif "cities" in counts:
        city_expected = int(counts["cities"])
    else:
        with csv_files["cities"].open("r", encoding="utf-8", newline="") as fh:
            city_expected = sum(1 for _ in csv.DictReader(fh))

    if "rows_written" in counts:
        routes_expected = int(counts["rows_written"])
    elif "route_measurements" in counts:
        routes_expected = int(counts["route_measurements"])
    else:
        with csv_files["route_measurements"].open("r", encoding="utf-8", newline="") as fh:
            routes_expected = sum(1 for _ in csv.DictReader(fh))

    if "rows_rejected" in counts:
        rejected_expected = int(counts["rows_rejected"])
    elif "rejected_rows" in counts:
        rejected_expected = int(counts["rejected_rows"])
    else:
        with csv_files["rejected_rows"].open("r", encoding="utf-8", newline="") as fh:
            rejected_expected = sum(1 for _ in csv.DictReader(fh))

    return {
        "city": city_expected,
        "route_measurement": routes_expected,
        "rejected_row": rejected_expected,
    }


def load_snapshot(
    raw_dir: Path = DEFAULT_RAW_DIR,
    normalized_dir: Path = DEFAULT_NORMALIZED_DIR,
) -> LoadResult:
    """Validate and load a normalized snapshot into PostgreSQL in one transaction."""
    manifest = _load_json(raw_dir / "manifest.json")
    report = _load_run_report(normalized_dir)

    status = _resolve_report_status(report)
    if status not in ALLOWED_REPORT_STATUSES:
        raise ValueError(
            f"Unsupported run-report status '{status}'. "
            f"Expected one of: {sorted(ALLOWED_REPORT_STATUSES)}"
        )

    csv_files = _verify_output_hashes(normalized_dir, report)
    expected_counts = _extract_expected_counts(report, csv_files)

    snapshot_id = str(manifest.get("snapshot_id") or report["snapshot_id"])
    source_sha256 = str(
        manifest.get("csv_sha256") or manifest.get("sha256") or report.get("input_csv_sha256") or ""
    )
    if not source_sha256:
        raise ValueError("Missing source sha256 in manifest.json and run report")

    record_count = int(manifest.get("row_count") or report.get("rows_read") or 0)
    retrieved_at = (
        manifest.get("downloaded_at_utc")
        or manifest.get("retrieved_at")
        or report.get("finished_at_utc")
    )
    if not retrieved_at:
        raise ValueError("Missing timestamp in manifest.json and run report")

    contract_version = str(report.get("contract_version", ""))
    code_commit = str(report.get("code_commit", ""))
    rows_read = int(report.get("rows_read", expected_counts["route_measurement"]))
    rows_written = int(report.get("rows_written", expected_counts["route_measurement"]))
    rows_rejected = int(report.get("rows_rejected", expected_counts["rejected_row"]))
    duplicates_removed = int(report.get("duplicates_removed", 0))
    started_at = report.get("started_at_utc") or retrieved_at
    finished_at = report.get("finished_at_utc") or retrieved_at

    conn_str = get_connection_string()
    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sha256 FROM data_snapshot WHERE snapshot_id = %s;",
                (snapshot_id,),
            )
            existing = cur.fetchone()
            if existing is not None:
                existing_sha = str(existing[0])
                if existing_sha.lower() == source_sha256.lower():
                    return LoadResult(
                        snapshot_id=snapshot_id,
                        already_loaded=True,
                        counts={},
                    )
                raise ValueError(
                    f"Snapshot '{snapshot_id}' already exists with different SHA-256: "
                    f"db={existing_sha}, manifest={source_sha256}"
                )

        with conn.transaction():
            with conn.cursor() as cur:
                # 1. data_source
                cur.execute(
                    """
                    INSERT INTO data_source (source_id, name)
                    VALUES (%s, %s)
                    ON CONFLICT (source_id) DO NOTHING;
                    """,
                    (SOURCE_ID, SOURCE_NAME),
                )

                # 2. data_snapshot
                cur.execute(
                    """
                    INSERT INTO data_snapshot (
                        snapshot_id,
                        source_id,
                        sha256,
                        record_count,
                        manifest,
                        retrieved_at
                    )
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s);
                    """,
                    (
                        snapshot_id,
                        SOURCE_ID,
                        source_sha256,
                        record_count,
                        json.dumps(manifest, ensure_ascii=False),
                        retrieved_at,
                    ),
                )

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
                        duplicates_removed,
                        started_at,
                        finished_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        duplicates_removed,
                        started_at,
                        finished_at,
                    ),
                )
                load_run_id = cur.fetchone()[0]

                # 4. Bulk COPY: city + сбор внешних идентификаторов
                external_id_rows: list[tuple[str, str, str]] = []
                with csv_files["cities"].open("r", encoding="utf-8", newline="") as fh:
                    reader = csv.DictReader(fh)
                    with cur.copy(
                        """
                        COPY city (
                            city_id,
                            name,
                            region,
                            oktmo,
                            is_capital,
                            population,
                            location,
                            attributes_snapshot_id
                        ) FROM STDIN
                        """
                    ) as copy:
                        for row in reader:
                            city_uuid = _strip_prefix(row["city_id"].strip(), "city-")
                            oktmo = _null_if_empty(row.get("oktmo"))
                            is_capital = _parse_bool(row.get("is_capital"), default=False)
                            population = int(float(row["population"]))
                            location = _make_point_ewkt(
                                row.get("longitude") or row.get("lon"),
                                row.get("latitude") or row.get("lat"),
                            )
                            copy.write_row(
                                (
                                    city_uuid,
                                    row["name"],
                                    row["region"],
                                    oktmo,
                                    is_capital,
                                    population,
                                    location,
                                    snapshot_id,
                                )
                            )

                            raw_source_id = _null_if_empty(
                                row.get("source_city_id") or row.get("sber_territory_id")
                            )
                            if raw_source_id is not None:
                                sber_id = _strip_prefix(raw_source_id, "sber:")
                                external_id_rows.append((city_uuid, "sber_territory", sber_id))

                            if oktmo is not None:
                                external_id_rows.append((city_uuid, "oktmo", oktmo))

                # 5. Bulk COPY: city_external_id
                with cur.copy(
                    """
                    COPY city_external_id (
                        city_id,
                        id_namespace,
                        external_id
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
                            from_city_id,
                            to_city_id,
                            transport_type,
                            distance_m,
                            duration_sec,
                            missing_reason,
                            source,
                            comment
                        ) FROM STDIN
                        """
                    ) as copy:
                        for row in reader:
                            meas_uuid = _strip_prefix(row["measurement_id"].strip(), "meas-")
                            from_uuid = _strip_prefix(row["from_city_id"].strip(), "city-")
                            to_uuid = _strip_prefix(row["to_city_id"].strip(), "city-")
                            copy.write_row(
                                (
                                    meas_uuid,
                                    snapshot_id,
                                    from_uuid,
                                    to_uuid,
                                    row["transport_type"].strip(),
                                    _null_if_empty(row.get("distance_m")),
                                    _null_if_empty(row.get("duration_sec")),
                                    _null_if_empty(
                                        row.get("missing_reason_raw") or row.get("missing_reason")
                                    ),
                                    _null_if_empty(row.get("source_raw") or row.get("source")),
                                    _null_if_empty(row.get("comment_raw") or row.get("comment")),
                                )
                            )

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
                            raw_json = _null_if_empty(row.get("raw_json")) or json.dumps(
                                row, ensure_ascii=False
                            )
                            copy.write_row(
                                (
                                    load_run_id,
                                    int(row["source_row_number"]),
                                    row["rejection_reason"],
                                    _null_if_empty(row.get("rejection_detail")),
                                    raw_json,
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
