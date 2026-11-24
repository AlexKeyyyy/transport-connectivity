"""Database tests for the analytic layer migration (0004_analytic_layer.sql)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import errors, sql

from transport_connectivity.db.migrate import get_connection_string, run_migrations

RUN_DB_TESTS = os.environ.get("RUN_DB_TESTS") == "1"
MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "transport_connectivity" / "db" / "migrations"
)

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(not RUN_DB_TESTS, reason="Set RUN_DB_TESTS=1 to run PostgreSQL tests"),
]


def _setup_fixture_cities(
    cur: psycopg.Cursor,
    prefix: str,
) -> tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert minimal source/snapshot/load_run/cities rows for analytic constraint tests."""
    source_id = f"{prefix}_src"
    snapshot_id = f"{prefix}_snap"
    city_a = uuid.uuid5(uuid.NAMESPACE_DNS, f"{prefix}-city-a")
    city_b = uuid.uuid5(uuid.NAMESPACE_DNS, f"{prefix}-city-b")

    cur.execute(
        "INSERT INTO data_source (source_id, name) VALUES (%s, %s);",
        (source_id, "Test Source"),
    )
    cur.execute(
        """
        INSERT INTO data_snapshot (
            snapshot_id, source_id, file_name, sha256, record_count, manifest, retrieved_at
        )
        VALUES (%s, %s, 'test.csv', %s, 0, '{}'::jsonb, NOW());
        """,
        (snapshot_id, source_id, f"{prefix}_sha256"),
    )
    cur.execute(
        """
        INSERT INTO load_run (
            snapshot_id, status, contract_version, code_commit,
            rows_read, rows_written, rows_rejected, started_at
        )
        VALUES (%s, 'success', '1.0', 'test', 0, 0, 0, NOW())
        RETURNING load_run_id;
        """,
        (snapshot_id,),
    )
    load_run_id = cur.fetchone()[0]

    for city_id, name in ((city_a, "Город А"), (city_b, "Город Б")):
        cur.execute(
            """
            INSERT INTO city (city_id, name, region, population, attributes_snapshot_id)
            VALUES (%s, %s, 'Тест', 100000, %s);
            """,
            (city_id, name, snapshot_id),
        )

    return snapshot_id, load_run_id, city_a, city_b


def test_0004_applies_on_clean_schema_and_migrate_is_idempotent() -> None:
    """Migrations 0001-0004 apply cleanly on an empty schema; repeat migrate is a no-op."""
    schema_name = f"test_clean_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(get_connection_string()) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
                cur.execute(
                    sql.SQL("SET LOCAL search_path TO {}, public").format(
                        sql.Identifier(schema_name)
                    )
                )
                for mig_name in (
                    "0001_extensions.sql",
                    "0002_source_layer.sql",
                    "0003_normalized_layer.sql",
                    "0004_analytic_layer.sql",
                ):
                    cur.execute((MIGRATIONS_DIR / mig_name).read_text(encoding="utf-8"))

                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = %s
                    """,
                    (schema_name,),
                )
                tables = {row[0] for row in cur.fetchall()}

                expected = {
                    "graph_snapshot",
                    "graph_snapshot_input",
                    "graph_node",
                    "graph_edge",
                    "analysis_run",
                    "metric_result",
                }
                assert expected.issubset(tables)
                cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))

    run_migrations()
    assert run_migrations() == []


def test_graph_snapshot_input_rejects_run_from_another_snapshot() -> None:
    run_migrations()
    prefix = f"t32_input_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(get_connection_string()) as conn:
        with conn.cursor() as cur:
            try:
                snapshot_a, run_a, _, _ = _setup_fixture_cities(cur, f"{prefix}_a")
                _, run_b, _, _ = _setup_fixture_cities(cur, f"{prefix}_b")
                cur.execute(
                    """
                    INSERT INTO graph_snapshot (
                        methodology_version, parameters, is_directed, status, code_commit
                    )
                    VALUES ('v1', '{}'::jsonb, true, 'building', 'test')
                    RETURNING graph_snapshot_id;
                    """
                )
                graph_id = cur.fetchone()[0]
                insert_input = """
                    INSERT INTO graph_snapshot_input (graph_snapshot_id, snapshot_id, load_run_id)
                    VALUES (%s, %s, %s)
                """
                with pytest.raises(errors.ForeignKeyViolation) as error, conn.transaction():
                    cur.execute(insert_input, (graph_id, snapshot_a, run_b))
                assert error.value.diag.constraint_name == "fk_graph_snapshot_input_load_run"
                cur.execute(insert_input, (graph_id, snapshot_a, run_a))
            finally:
                conn.rollback()


def test_graph_edge_rejects_node_from_another_version() -> None:
    """An edge cannot reference a city that is only a node in another graph_snapshot."""
    run_migrations()
    prefix = f"t32_edge_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(get_connection_string()) as conn:
        with conn.cursor() as cur:
            try:
                _snapshot_id, _load_run_id, city_a, city_b = _setup_fixture_cities(cur, prefix)

                cur.execute(
                    """
                    INSERT INTO graph_snapshot (
                        methodology_version, parameters, is_directed, status, code_commit
                    )
                    VALUES ('v1', '{}'::jsonb, true, 'ready', 'test')
                    RETURNING graph_snapshot_id;
                    """
                )
                graph_v1 = cur.fetchone()[0]

                cur.execute(
                    """
                    INSERT INTO graph_snapshot (
                        methodology_version, parameters, is_directed, status, code_commit
                    )
                    VALUES ('v2', '{}'::jsonb, true, 'ready', 'test')
                    RETURNING graph_snapshot_id;
                    """
                )
                graph_v2 = cur.fetchone()[0]

                cur.execute(
                    "INSERT INTO graph_node (graph_snapshot_id, city_id) VALUES (%s, %s);",
                    (graph_v1, city_a),
                )
                cur.execute(
                    "INSERT INTO graph_node (graph_snapshot_id, city_id) VALUES (%s, %s);",
                    (graph_v2, city_b),
                )

                with pytest.raises(errors.ForeignKeyViolation):
                    cur.execute(
                        """
                        INSERT INTO graph_edge (
                            graph_snapshot_id, from_city_id, to_city_id,
                            transport_type, weight, weight_unit
                        )
                        VALUES (%s, %s, %s, 'road', 3600, 'sec');
                        """,
                        (graph_v1, city_a, city_b),
                    )
            finally:
                conn.rollback()


def test_analysis_run_requires_exactly_one_source() -> None:
    """analysis_run must reference either graph_snapshot_id or snapshot_id, never neither/both."""
    run_migrations()
    prefix = f"t32_run_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(get_connection_string()) as conn:
        with conn.cursor() as cur:
            try:
                snapshot_id, _load_run_id, _city_a, _city_b = _setup_fixture_cities(cur, prefix)
                cur.execute(
                    """
                    INSERT INTO graph_snapshot (
                        methodology_version, parameters, is_directed, status, code_commit
                    )
                    VALUES ('v1', '{}'::jsonb, true, 'ready', 'test')
                    RETURNING graph_snapshot_id;
                    """
                )
                graph_id = cur.fetchone()[0]

                # Neither graph_snapshot_id nor snapshot_id -> CHECK violation
                cur.execute("SAVEPOINT sp_neither;")
                with pytest.raises(errors.CheckViolation):
                    cur.execute(
                        """
                        INSERT INTO analysis_run (
                            graph_snapshot_id, snapshot_id, algorithm, engine,
                            engine_version, parameters, code_commit, status
                        )
                        VALUES (
                          NULL, NULL, 'reach', 'python', '3.12', '{}'::jsonb, 'test', 'ready'
                        );
                        """
                    )
                cur.execute("ROLLBACK TO SAVEPOINT sp_neither;")

                # Both graph_snapshot_id and snapshot_id -> CHECK violation
                cur.execute("SAVEPOINT sp_both;")
                with pytest.raises(errors.CheckViolation):
                    cur.execute(
                        """
                        INSERT INTO analysis_run (
                            graph_snapshot_id, snapshot_id, algorithm, engine,
                            engine_version, parameters, code_commit, status
                        )
                        VALUES (%s, %s, 'reach', 'python', '3.12', '{}'::jsonb, 'test', 'ready');
                        """,
                        (graph_id, snapshot_id),
                    )
                cur.execute("ROLLBACK TO SAVEPOINT sp_both;")

                # Valid: snapshot_id only
                cur.execute(
                    """
                    INSERT INTO analysis_run (
                        graph_snapshot_id, snapshot_id, algorithm, engine,
                        engine_version, parameters, code_commit, status
                    )
                    VALUES (NULL, %s, 'reach', 'python', '3.12', '{}'::jsonb, 'test', 'ready');
                    """,
                    (snapshot_id,),
                )
            finally:
                conn.rollback()


def test_metric_result_rejects_duplicate_with_null_other_city() -> None:
    """UNIQUE NULLS NOT DISTINCT rejects duplicate metrics even when other_city_id is NULL."""
    run_migrations()
    prefix = f"t32_met_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(get_connection_string()) as conn:
        with conn.cursor() as cur:
            try:
                snapshot_id, _load_run_id, city_a, city_b = _setup_fixture_cities(cur, prefix)
                cur.execute(
                    """
                    INSERT INTO analysis_run (
                        graph_snapshot_id, snapshot_id, algorithm, engine,
                        engine_version, parameters, code_commit, status
                    )
                    VALUES (NULL, %s, 'reach', 'python', '3.12', '{}'::jsonb, 'test', 'ready')
                    RETURNING analysis_run_id;
                    """,
                    (snapshot_id,),
                )
                run_id = cur.fetchone()[0]

                # Single-city metric (other_city_id IS NULL)
                cur.execute(
                    """
                    INSERT INTO metric_result (
                      analysis_run_id, metric, city_id, other_city_id, value
                    )
                    VALUES (%s, 'reach_cities', %s, NULL, 12);
                    """,
                    (run_id, city_a),
                )
                cur.execute("SAVEPOINT sp_dup_null;")
                with pytest.raises(errors.UniqueViolation):
                    cur.execute(
                        """
                        INSERT INTO metric_result (
                            analysis_run_id, metric, city_id, other_city_id, value
                        )
                        VALUES (%s, 'reach_cities', %s, NULL, 15);
                        """,
                        (run_id, city_a),
                    )
                cur.execute("ROLLBACK TO SAVEPOINT sp_dup_null;")

                # Pair metric (other_city_id IS NOT NULL, value IS NULL = unreachable)
                cur.execute(
                    """
                    INSERT INTO metric_result (
                      analysis_run_id, metric, city_id, other_city_id, value
                    )
                    VALUES (%s, 'travel_time_sec', %s, %s, NULL);
                    """,
                    (run_id, city_a, city_b),
                )
                with pytest.raises(errors.UniqueViolation):
                    cur.execute(
                        """
                        INSERT INTO metric_result (
                            analysis_run_id, metric, city_id, other_city_id, value
                        )
                        VALUES (%s, 'travel_time_sec', %s, %s, 7200);
                        """,
                        (run_id, city_a, city_b),
                    )
            finally:
                conn.rollback()
