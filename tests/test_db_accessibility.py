"""End-to-end publication tests on a dedicated PostgreSQL database."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from transport_connectivity.cli import main
from transport_connectivity.db import accessibility as db
from transport_connectivity.db.migrate import get_connection_string, run_migrations

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(os.environ.get("RUN_DB_TESTS") != "1", reason="Set RUN_DB_TESTS=1"),
]


@pytest.fixture
def snapshot() -> Iterator[tuple[str, list[str]]]:
    run_migrations()
    snapshot_id = f"accessibility-test-{uuid4()}"
    city_ids = [str(uuid4()) for _ in range(10)]
    with psycopg.connect(get_connection_string()) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO data_source (source_id, name) VALUES (%s, 'Test')", (snapshot_id,))
        cur.execute(
            """
            INSERT INTO data_snapshot
                (snapshot_id, source_id, file_name, sha256, manifest, retrieved_at)
            VALUES (%s, %s, 'test.csv', %s, '{}'::jsonb, now())
            """,
            (snapshot_id, snapshot_id, snapshot_id),
        )
        cur.execute(
            """
            INSERT INTO load_run (
                snapshot_id, status, contract_version, code_commit,
                rows_read, rows_written, rows_rejected, started_at
            )
            VALUES (%s, 'success', 'test', 'test', 5, 5, 0, now())
            """,
            (snapshot_id,),
        )
        cur.executemany(
            """
            INSERT INTO city (city_id, name, region, population, attributes_snapshot_id)
            VALUES (%s, %s, 'Test region', %s, %s)
            """,
            [
                (city, f"City {i}", None if i == 2 else i * 1000, snapshot_id)
                for i, city in enumerate(city_ids, start=1)
            ],
        )
        cur.executemany(
            """
            INSERT INTO route_measurement (
                measurement_id, snapshot_id, source_row_number, from_city_id, to_city_id,
                transport_type, distance_m, duration_sec, missing_reason_raw
            )
            VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (snapshot_id, 2, city_ids[0], city_ids[1], "road", 1000, 21600, None),
                (snapshot_id, 3, city_ids[0], city_ids[2], "road", 1000, 21601, None),
                (snapshot_id, 4, city_ids[0], city_ids[2], "air", 1000, 10000, None),
                (snapshot_id, 5, city_ids[1], city_ids[0], "rail", 1000, 10800, None),
                (snapshot_id, 6, city_ids[0], city_ids[3], "road", None, None, "Unavailable"),
            ],
        )

    try:
        yield snapshot_id, city_ids
    finally:
        with psycopg.connect(get_connection_string()) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM analysis_run WHERE snapshot_id = %s", (snapshot_id,))
            cur.execute("DELETE FROM route_measurement WHERE snapshot_id = %s", (snapshot_id,))
            cur.execute("DELETE FROM city WHERE attributes_snapshot_id = %s", (snapshot_id,))
            cur.execute("DELETE FROM load_run WHERE snapshot_id = %s", (snapshot_id,))
            cur.execute("DELETE FROM data_snapshot WHERE snapshot_id = %s", (snapshot_id,))
            cur.execute("DELETE FROM data_source WHERE source_id = %s", (snapshot_id,))


def test_publishes_once_even_with_two_callers(
    snapshot: tuple[str, list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_id, city_ids = snapshot
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(db.run_accessibility, snapshot_id, code_version="test")
        second = pool.submit(db.run_accessibility, snapshot_id, code_version="test")
        reports = [first.result(), second.result()]

    assert sorted(report["runs_created"] for report in reports) == [0, 12]
    assert reports[0]["best_half_day"] == reports[1]["best_half_day"]
    assert main(["accessibility", "--snapshot-id", snapshot_id]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert (repeated["runs_created"], repeated["runs_reused"]) == (0, 12)
    assert repeated["cities_without_population"] == 1
    assert repeated["best_half_day"]["top_10"][0]["city_id"] == city_ids[0]
    assert repeated["best_half_day"]["top_10"][0]["reach_cities"] == 2
    assert repeated["best_half_day"]["top_10"][0]["reach_population"] == 3000

    with psycopg.connect(get_connection_string()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ar.algorithm, count(m.metric_result_id), ar.status, ar.parameters
            FROM analysis_run ar JOIN metric_result m USING (analysis_run_id)
            WHERE ar.snapshot_id = %s AND ar.graph_snapshot_id IS NULL AND ar.scenario IS NULL
            GROUP BY ar.analysis_run_id
            """,
            (snapshot_id,),
        )
        runs = cur.fetchall()
        assert len(runs) == 12
        assert sum(row[0] == "travel_times" for row in runs) == 3
        for algorithm, count, status, parameters in runs:
            assert status == "ready"
            assert parameters["layer"] in ("road", "rail", "best")
            if algorithm == "threshold_accessibility":
                assert count == 20
                assert parameters["air_access_sec"] == 10800
                assert parameters["threshold_sec"] in (10800, 21600, 86400)
            else:
                assert count == 90

        cur.execute(
            """
            SELECT m.other_city_id::text, m.value
            FROM metric_result m JOIN analysis_run ar USING (analysis_run_id)
            WHERE ar.snapshot_id = %s AND ar.algorithm = 'travel_times'
              AND ar.parameters ->> 'layer' = 'best' AND m.city_id = %s
            """,
            (snapshot_id, city_ids[0]),
        )
        rows = dict(cur.fetchall())
        assert len(rows) == 9
        assert rows[city_ids[2]] == Decimal(20800)
        assert rows[city_ids[3]] is None
        assert city_ids[0] not in rows


def test_copy_error_rolls_back_runs(
    snapshot: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_id, city_ids = snapshot
    original = db._metric_rows

    def duplicate_row(run: db.MatrixRun, times: dict, population: dict) -> Iterator[db.MetricRow]:
        yield from original(run, times, population)
        if run.algorithm == db.ACCESSIBILITY:
            yield "reach_cities", city_ids[0], None, 42

    monkeypatch.setattr(db, "_metric_rows", duplicate_row)
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.run_accessibility(snapshot_id, code_version="test")

    with psycopg.connect(get_connection_string()) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM analysis_run WHERE snapshot_id = %s", (snapshot_id,))
        assert cur.fetchone() == (0,)


def test_ignores_a_ready_run_with_unrelated_json_parameters(
    snapshot: tuple[str, list[str]],
) -> None:
    snapshot_id, _ = snapshot
    with psycopg.connect(get_connection_string()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analysis_run (
                snapshot_id, algorithm, engine, engine_version, parameters, code_commit, status
            )
            VALUES (%s, 'threshold_accessibility', 'python', 'test', '[]'::jsonb, 'test', 'ready')
            """,
            (snapshot_id,),
        )

    assert db.run_accessibility(snapshot_id, code_version="test")["runs_created"] == 12
    assert db.run_accessibility(snapshot_id, code_version="test")["runs_reused"] == 12
