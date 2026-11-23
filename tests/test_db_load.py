"""Tests for transactional snapshot loader and database migrations."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest

from transport_connectivity.db.load import load_snapshot
from transport_connectivity.db.migrate import get_connection_string, run_migrations

RUN_DB_TESTS = os.environ.get("RUN_DB_TESTS") == "1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_fixture_snapshot(
    tmp_path: Path,
    snapshot_id: str,
    *,
    corrupt_hash: bool = False,
    invalid_transport_type: bool = False,
) -> tuple[Path, Path]:
    raw_dir = tmp_path / "raw" / snapshot_id
    norm_dir = tmp_path / "normalized" / snapshot_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    norm_dir.mkdir(parents=True, exist_ok=True)

    city_a = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{snapshot_id}-city-a"))
    city_b = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{snapshot_id}-city-b"))
    meas_1 = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{snapshot_id}-meas-1"))
    transport = "teleport" if invalid_transport_type else "road"

    cities_csv = norm_dir / "cities.csv"
    cities_csv.write_text(
        "city_id,id_namespace,source_city_id,oktmo,name,region,latitude,longitude,population\n"
        f"{city_a},sber_territory,{snapshot_id}-1,{snapshot_id}-01,"
        "Москва,Москва,55.7558,37.6173,13000000\n"
        f"{city_b},sber_territory,{snapshot_id}-2,{snapshot_id}-02,"
        "Тверь,Тверская область,56.8587,35.9176,425000\n",
        encoding="utf-8",
    )

    routes_csv = norm_dir / "route_measurements.csv"
    routes_csv.write_text(
        "measurement_id,snapshot_id,source_row_number,from_city_id,to_city_id,"
        "transport_type,distance_m,duration_sec,missing_reason_raw,source_raw,comment_raw\n"
        f"{meas_1},{snapshot_id},2,{city_a},{city_b},{transport},180000,9000,CD,OSM,CD\n",
        encoding="utf-8",
    )

    rejected_csv = norm_dir / "rejected_rows.csv"
    rejected_csv.write_text(
        "source_row_number,reject_reason,detail,raw_json\n"
        '3,self_pair,from == to,"{""city_from"": ""Москва""}"\n',
        encoding="utf-8",
    )

    manifest = {
        "snapshot_id": snapshot_id,
        "downloaded_at_utc": "2026-11-23T00:00:00+00:00",
        "csv_sha256": hashlib.sha256(snapshot_id.encode()).hexdigest(),
        "row_count": 2,
    }
    (raw_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    report = {
        "contract_version": "1.0-draft",
        "snapshot_id": snapshot_id,
        "status": "success_with_rejections",
        "output": {
            "cities.csv": {"rows": 2, "sha256": _sha256(cities_csv)},
            "route_measurements.csv": {"rows": 1, "sha256": _sha256(routes_csv)},
            "rejected_rows.csv": {"rows": 1, "sha256": _sha256(rejected_csv)},
        },
        "balance": {
            "rows_read": 2,
            "route_measurements": 1,
            "rejected_rows": 1,
        },
        "duplicates": {"exact_duplicate": 0, "key_collision": 0},
        "service": {
            "started_at_utc": "2026-11-23T00:00:01+00:00",
            "finished_at_utc": "2026-11-23T00:00:02+00:00",
            "code_version": "test-commit",
        },
    }
    (norm_dir / "run-report.json").write_text(json.dumps(report), encoding="utf-8")

    if corrupt_hash:
        routes_csv.write_text(
            routes_csv.read_text(encoding="utf-8") + "corrupted\n",
            encoding="utf-8",
        )

    return raw_dir, norm_dir


def _cleanup_snapshot(snapshot_id: str) -> None:
    with psycopg.connect(get_connection_string()) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM route_measurement WHERE snapshot_id = %s;",
                    (snapshot_id,),
                )
                cur.execute("DELETE FROM load_run WHERE snapshot_id = %s;", (snapshot_id,))
                cur.execute(
                    "DELETE FROM city WHERE attributes_snapshot_id = %s;",
                    (snapshot_id,),
                )
                cur.execute(
                    "DELETE FROM data_snapshot WHERE snapshot_id = %s;",
                    (snapshot_id,),
                )


def test_load_rejects_corrupted_csv_checksum(tmp_path: Path) -> None:
    """Modified CSV fails SHA-256 verification before touching the database."""
    raw_dir, norm_dir = _create_fixture_snapshot(
        tmp_path,
        "test_corrupted_sha",
        corrupt_hash=True,
    )
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_snapshot(raw_dir=raw_dir, normalized_dir=norm_dir)


@pytest.mark.skipif(not RUN_DB_TESTS, reason="Set RUN_DB_TESTS=1 to run PostgreSQL tests")
def test_load_is_idempotent(tmp_path: Path) -> None:
    """Second load of the same snapshot skips cleanly without duplicating rows."""
    snapshot_id = "test_db_load_idempotent"
    run_migrations()
    _cleanup_snapshot(snapshot_id)
    try:
        raw_dir, norm_dir = _create_fixture_snapshot(tmp_path, snapshot_id)

        first = load_snapshot(raw_dir=raw_dir, normalized_dir=norm_dir)
        assert not first.already_loaded
        assert first.counts == {
            "city": 2,
            "city_external_id": 4,
            "route_measurement": 1,
            "rejected_row": 1,
        }

        second = load_snapshot(raw_dir=raw_dir, normalized_dir=norm_dir)
        assert second.already_loaded
        assert second.counts == {}

        with psycopg.connect(get_connection_string()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM data_snapshot WHERE snapshot_id = %s;",
                    (snapshot_id,),
                )
                assert cur.fetchone()[0] == 1
                cur.execute(
                    "SELECT count(*) FROM load_run WHERE snapshot_id = %s;",
                    (snapshot_id,),
                )
                assert cur.fetchone()[0] == 1
    finally:
        _cleanup_snapshot(snapshot_id)


@pytest.mark.skipif(not RUN_DB_TESTS, reason="Set RUN_DB_TESTS=1 to run PostgreSQL tests")
def test_load_rolls_back_on_invalid_csv_content(tmp_path: Path) -> None:
    """DB constraint violation inside CSV rolls back the entire transaction."""
    snapshot_id = "test_db_load_rollback"
    run_migrations()
    _cleanup_snapshot(snapshot_id)
    try:
        raw_dir, norm_dir = _create_fixture_snapshot(
            tmp_path,
            snapshot_id,
            invalid_transport_type=True,
        )
        with pytest.raises(psycopg.Error):
            load_snapshot(raw_dir=raw_dir, normalized_dir=norm_dir)

        with psycopg.connect(get_connection_string()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM data_snapshot WHERE snapshot_id = %s;",
                    (snapshot_id,),
                )
                assert cur.fetchone()[0] == 0
                cur.execute(
                    "SELECT count(*) FROM city WHERE attributes_snapshot_id = %s;",
                    (snapshot_id,),
                )
                assert cur.fetchone()[0] == 0
    finally:
        _cleanup_snapshot(snapshot_id)
