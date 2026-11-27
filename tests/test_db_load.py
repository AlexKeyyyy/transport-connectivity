"""Tests for transactional snapshot loader and database migrations."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from test_processor import SAMPLE_CSV, write_snapshot

from transport_connectivity.db.load import _city_values, _measurement_values, load_snapshot
from transport_connectivity.db.migrate import get_connection_string, run_migrations
from transport_connectivity.processor import process_snapshot

requires_database = pytest.mark.skipif(
    not os.environ.get("POSTGRES_HOST") or os.environ.get("RUN_DB_TESTS") != "1",
    reason="Set RUN_DB_TESTS=1 and POSTGRES_HOST for a dedicated test database",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_fixture_snapshot(
    tmp_path: Path,
    snapshot_id: str,
    *,
    corrupt_hash: bool = False,
    invalid_transport_type: bool = False,
    missing_city_attributes: bool = False,
) -> tuple[Path, Path]:
    raw_dir = tmp_path / "raw" / snapshot_id
    norm_dir = tmp_path / "normalized" / snapshot_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    norm_dir.mkdir(parents=True, exist_ok=True)

    city_a = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{snapshot_id}-city-a"))
    city_b = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{snapshot_id}-city-b"))
    meas_1 = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{snapshot_id}-meas-1"))
    transport = "teleport" if invalid_transport_type else "road"

    city_attributes = (
        ",,56.8587,35.9176,"
        if missing_city_attributes
        else "Тверь,Тверская область,56.8587,35.9176,425000"
    )
    cities_csv = norm_dir / "cities.csv"
    cities_csv.write_text(
        "city_id,id_namespace,source_city_id,oktmo,name,region,latitude,longitude,population\n"
        f"{city_a},sber_territory,{snapshot_id}-1,{snapshot_id}-01,"
        "Москва,Москва,55.7558,37.6173,13000000\n"
        f"{city_b},sber_territory,{snapshot_id}-2,{snapshot_id}-02,"
        f"{city_attributes}\n",
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
        "csv_filename": "input.csv",
    }
    (raw_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    report = {
        "contract_version": "1.0-draft",
        "snapshot_id": snapshot_id,
        "status": "success_with_rejections",
        "input": {"sha256": manifest["csv_sha256"]},
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


@pytest.mark.db
@requires_database
def test_load_is_idempotent(tmp_path: Path) -> None:
    """Second load of the same snapshot skips cleanly without duplicating rows."""
    snapshot_id = "test_db_load_idempotent"
    run_migrations()
    _cleanup_snapshot(snapshot_id)
    try:
        raw_dir, norm_dir = _create_fixture_snapshot(
            tmp_path, snapshot_id, missing_city_attributes=True
        )

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

        manifest_path = raw_dir / "manifest.json"
        report_path = norm_dir / "run-report.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        other_sha = "0" * 64
        manifest["csv_sha256"] = other_sha
        report["input"]["sha256"] = other_sha
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        report_path.write_text(json.dumps(report), encoding="utf-8")
        with pytest.raises(ValueError, match="already exists with different SHA-256"):
            load_snapshot(raw_dir, norm_dir)

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
                cur.execute(
                    "SELECT name, region, population FROM city "
                    "WHERE attributes_snapshot_id = %s AND name IS NULL",
                    (snapshot_id,),
                )
                assert cur.fetchone() == (None, None, None)
                cur.execute(
                    "SELECT pg_typeof(load_run_id)::text, raw_json FROM rejected_row "
                    "JOIN load_run USING (load_run_id) WHERE snapshot_id = %s",
                    (snapshot_id,),
                )
                assert cur.fetchone() == ("uuid", {"city_from": "Москва"})
    finally:
        _cleanup_snapshot(snapshot_id)


@pytest.mark.db
@requires_database
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


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"snapshot_id": "another_snapshot"}, "snapshot_id differs"),
        ({"input": {"sha256": "0" * 64}}, "Input SHA-256 differs"),
        ({"status": "failed"}, "Unsupported run-report status"),
        (
            {"balance": {"rows_read": 2, "route_measurements": 2, "rejected_rows": 0}},
            "Row counts disagree",
        ),
    ],
)
def test_rejects_inconsistent_metadata_before_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict, error: str
) -> None:
    raw_dir, norm_dir = _create_fixture_snapshot(tmp_path, "test_inconsistent_metadata")
    report_path = norm_dir / "run-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_path.write_text(json.dumps(report | change), encoding="utf-8")

    def unexpected_connection(*args: object, **kwargs: object) -> None:
        pytest.fail("Invalid input reached the database")

    monkeypatch.setattr(psycopg, "connect", unexpected_connection)
    with pytest.raises(ValueError, match=error):
        load_snapshot(raw_dir, norm_dir)


def test_copy_values_keep_nulls_and_exact_numbers() -> None:
    row = dict(city_id="city", name="", region="", population="", latitude="54", longitude="37")
    assert _city_values(row, "sample") == (
        "city",
        None,
        None,
        None,
        "SRID=4326;POINT(37 54)",
        "sample",
    )
    assert _city_values(row | {"longitude": ""}, "sample")[4] is None
    measurement = dict(
        measurement_id="measurement",
        snapshot_id="sample",
        source_row_number="2",
        from_city_id="a",
        to_city_id="b",
        transport_type="road",
        distance_m="694000.125",
        duration_sec="35280",
        missing_reason_raw="",
        source_raw="OSM",
        comment_raw="",
    )
    assert _measurement_values(measurement, "sample")[6:] == (
        "694000.125",
        "35280",
        None,
        "OSM",
        None,
    )


@pytest.mark.db
@requires_database
def test_process_and_load_project_sample(tmp_path: Path) -> None:
    raw_dir = write_snapshot(tmp_path / "raw", SAMPLE_CSV.read_bytes())
    manifest_path = raw_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["snapshot_id"] = f"test_db_sample_{uuid.uuid4().hex}"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output_root = tmp_path / "normalized"
    report = process_snapshot(raw_dir, output_root)
    snapshot_id = report["snapshot_id"]
    normalized_dir = output_root / snapshot_id
    run_migrations()
    try:
        result = load_snapshot(raw_dir, normalized_dir)
        assert result.counts == {
            "city": 10,
            "city_external_id": 20,
            "route_measurement": 270,
            "rejected_row": 0,
        }
        assert load_snapshot(raw_dir, normalized_dir).already_loaded
        with psycopg.connect(get_connection_string()) as conn:
            row = conn.execute(
                "SELECT source_row_number, distance_m, duration_sec, missing_reason_raw, "
                "source_raw "
                "FROM route_measurement WHERE snapshot_id = %s AND source_row_number = 2",
                (snapshot_id,),
            ).fetchone()
            assert row == (2, 694000, 35280, "CD", "OSM")
    finally:
        _cleanup_snapshot(snapshot_id)
