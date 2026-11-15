"""Offline tests for the processor against docs/data-contract.md (sections 2-7)."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from transport_connectivity.cli import main as cli_main
from transport_connectivity.connector import (
    MANIFEST_FILENAME,
    SNAPSHOT_ID,
    SOURCE_COLUMNS,
    DatasetManifest,
    sha256_bytes,
)
from transport_connectivity.processor import (
    CITIES_FILE,
    MEASUREMENTS_FILE,
    REJECTED_FILE,
    REPORT_FILE,
    ProcessingError,
    city_id,
    format_decimal,
    process_snapshot,
)

SAMPLE_CSV = Path(__file__).resolve().parents[1] / "data/samples/intercity_connectivity_sample.csv"
CSV_NAME = "input.csv"

BRYANSK = {
    "city": "Брянск",
    "region": "Брянская область",
    "oktmo": "15701000001",
    "sber_territory_id": "953",
    "is_capital": "1",
    "population": "379152",
    "lat": "53.2420053",
    "lon": "34.3653002",
}
VLADIMIR = {
    "city": "Владимир",
    "region": "Владимирская область",
    "oktmo": "17701000001",
    "sber_territory_id": "986",
    "is_capital": "1",
    "population": "349951",
    "lat": "56.1280444",
    "lon": "40.4082983",
}
TULA = {
    **VLADIMIR,
    "city": "Тула",
    "region": "Тульская область",
    "oktmo": "70701000001",
    "sber_territory_id": "2164",
}


def record(
    origin: dict[str, str] = BRYANSK, target: dict[str, str] = VLADIMIR, **fields: str
) -> dict[str, str]:
    row = {f"{key}_from": value for key, value in origin.items()}
    row |= {f"{key}_to": value for key, value in target.items()}
    row |= {
        "transport_type": "road",
        "distance_km": "694.0",
        "distance_hr": "9.8",
        "missing_reason": "CD",
        "source": "OSM",
        "comment": "CD",
    }
    return row | fields


def write_snapshot(raw_dir: Path, content: bytes, *, row_count: int | None = None) -> Path:
    """Write a CSV and a manifest that describes it, as `download` would."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / CSV_NAME).write_bytes(content)
    records = sum(1 for _ in csv.reader(content.decode("utf-8-sig").splitlines(), delimiter=";"))
    manifest = DatasetManifest(
        snapshot_id=SNAPSHOT_ID,
        dataset_version="test",
        source_url="https://example.test/data.zip",
        page_url="https://example.test",
        downloaded_at_utc="2026-11-15T00:00:00+00:00",
        archive_filename="data.zip",
        archive_sha256="0" * 64,
        archive_size_bytes=0,
        csv_member=CSV_NAME,
        csv_filename=CSV_NAME,
        csv_sha256=sha256_bytes(content),
        csv_size_bytes=len(content),
        encoding="utf-8",
        delimiter=";",
        row_count=records - 1 if row_count is None else row_count,
        columns=list(SOURCE_COLUMNS),
        city_count=0,
        baseline={},
    )
    (raw_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    return raw_dir


def csv_bytes(rows: list[dict[str, str]]) -> bytes:
    lines = [";".join(SOURCE_COLUMNS)] + [";".join(row[c] for c in SOURCE_COLUMNS) for row in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


class Run:
    """Outputs of one successful run, read back from disk."""

    def __init__(self, output_dir: Path) -> None:
        self.dir = output_dir
        self.report: dict[str, Any] = json.loads((output_dir / REPORT_FILE).read_text("utf-8"))
        self.cities = self._read(CITIES_FILE)
        self.measurements = self._read(MEASUREMENTS_FILE)
        self.rejected = self._read(REJECTED_FILE)

    def _read(self, name: str) -> list[dict[str, str]]:
        with (self.dir / name).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def city(self, source_city_id: str) -> dict[str, str]:
        return next(row for row in self.cities if row["source_city_id"] == source_city_id)

    def reasons(self) -> list[str]:
        return [row["reject_reason"] for row in self.rejected]


def run(tmp_path: Path, rows: list[dict[str, str]]) -> Run:
    raw_dir = write_snapshot(tmp_path / "raw", csv_bytes(rows))
    process_snapshot(raw_dir, tmp_path / "out")
    return Run(tmp_path / "out" / SNAPSHOT_ID)


# --- section 7.2: the 270-record slice ---------------------------------------------------


@pytest.fixture(scope="module")
def sample_run(tmp_path_factory: pytest.TempPathFactory) -> Run:
    tmp_path = tmp_path_factory.mktemp("sample")
    raw_dir = write_snapshot(tmp_path / "raw", SAMPLE_CSV.read_bytes())
    process_snapshot(raw_dir, tmp_path / "out")
    return Run(tmp_path / "out" / SNAPSHOT_ID)


def test_sample_counts_and_balance(sample_run: Run) -> None:
    report = sample_run.report
    assert report["status"] == "success"
    assert report["balance"] == {"rows_read": 270, "route_measurements": 270, "rejected_rows": 0}
    assert report["measurements"] == {
        "with_values": 180,
        "without_values": 90,
        "by_transport_type": {"air": 90, "rail": 90, "road": 90},
    }
    assert report["missing_reason_values"] == {"CD": 180, "Маршрут не найден": 90}
    assert len(sample_run.cities) == 10
    assert sample_run.rejected == []


def test_sample_cities_are_keyed_by_sber_id_in_string_order(sample_run: Run) -> None:
    assert [(row["source_city_id"], row["name"]) for row in sample_run.cities] == [
        ("1198", "Калуга"),
        ("1707", "Орёл"),
        ("1845", "Рязань"),
        ("2044", "Смоленск"),
        ("2101", "Тверь"),
        ("2164", "Тула"),
        ("2318", "Ярославль"),
        ("2539", "Москва"),
        ("953", "Брянск"),
        ("986", "Владимир"),
    ]
    assert {row["id_namespace"] for row in sample_run.cities} == {"sber_territory"}


def test_reference_identifiers_from_contract(sample_run: Run) -> None:
    bryansk, vladimir = sample_run.city("953"), sample_run.city("986")
    assert bryansk["city_id"] == "83c80e94-954a-5182-b2dd-a4aa219e6400"
    assert vladimir["city_id"] == "7e9d63d7-2778-54ed-bd76-e000209493d6"
    road = next(
        row
        for row in sample_run.measurements
        if (row["from_city_id"], row["to_city_id"], row["transport_type"])
        == (bryansk["city_id"], vladimir["city_id"], "road")
    )
    assert road["measurement_id"] == "fb7e015a-513c-5f3b-849a-a5a886ad08a8"
    assert road["snapshot_id"] == "tochno_intercity_v20260629"


@pytest.mark.parametrize(
    ("origin", "target", "transport", "expected"),
    [
        ("Брянск", "Владимир", "road", ("694000", "35280", "CD", "OSM")),
        ("Москва", "Тверь", "road", ("187000", "10800", "CD", "OSRM")),
        ("Тверь", "Тула", "rail", ("375000", "24480", "CD", "SberIndex")),
        ("Брянск", "Владимир", "air", ("", "", "Маршрут не найден", "ND")),
    ],
)
def test_sample_spot_checks(
    sample_run: Run, origin: str, target: str, transport: str, expected: tuple[str, ...]
) -> None:
    ids = {row["name"]: row["city_id"] for row in sample_run.cities}
    row = next(
        row
        for row in sample_run.measurements
        if (row["from_city_id"], row["to_city_id"], row["transport_type"])
        == (ids[origin], ids[target], transport)
    )
    columns = ("distance_m", "duration_sec", "missing_reason_raw", "source_raw")
    assert tuple(row[column] for column in columns) == expected


def test_sample_source_row_numbers_count_physical_lines(sample_run: Run) -> None:
    numbers = [int(row["source_row_number"]) for row in sample_run.measurements]
    assert numbers == list(range(2, 272))


def test_repeated_run_is_byte_identical_and_does_not_accumulate(tmp_path: Path) -> None:
    raw_dir = write_snapshot(tmp_path / "raw", SAMPLE_CSV.read_bytes())
    first = process_snapshot(raw_dir, tmp_path / "out")
    second = process_snapshot(raw_dir, tmp_path / "out")

    assert first["output"] == second["output"]
    assert {key: value for key, value in first.items() if key != "service"} == {
        key: value for key, value in second.items() if key != "service"
    }
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == [SNAPSHOT_ID]
    assert len(Run(tmp_path / "out" / SNAPSHOT_ID).measurements) == 270


# --- sections 3-4: units and row-level validation ----------------------------------------


@pytest.mark.parametrize(
    ("value", "factor", "expected"),
    [("694.0", 1000, "694000"), ("9.8", 3600, "35280"), ("123.456", 1000, "123456")]
    + [("123.4567", 1000, "123456.7"), ("0.25", 3600, "900")],
)
def test_decimal_conversion_has_no_float_noise_or_trailing_zeros(
    value: str, factor: int, expected: str
) -> None:
    assert format_decimal(Decimal(value) * factor) == expected


def test_directions_are_independent_and_namesakes_are_different_cities(tmp_path: Path) -> None:
    namesake = {**VLADIMIR, "sber_territory_id": "5000", "oktmo": "17999000001"}
    result = run(
        tmp_path,
        [
            record(BRYANSK, VLADIMIR),
            record(VLADIMIR, BRYANSK, distance_km="700"),
            record(BRYANSK, namesake),
        ],
    )

    assert len(result.measurements) == 3
    assert [row["distance_m"] for row in result.measurements] == ["694000", "700000", "694000"]
    assert [row["name"] for row in result.cities] == ["Владимир", "Брянск", "Владимир"]
    assert len({row["city_id"] for row in result.cities}) == 3


def test_missing_measurement_is_kept_with_nulls_and_code_is_not_interpreted(
    tmp_path: Path,
) -> None:
    result = run(
        tmp_path,
        [
            record(transport_type="air", distance_km="", distance_hr="", missing_reason="Нет"),
            record(transport_type="rail", distance_km="", distance_hr="", missing_reason="CD"),
        ],
    )

    assert result.rejected == []
    assert [(row["distance_m"], row["duration_sec"]) for row in result.measurements] == [
        ("", ""),
        ("", ""),
    ]
    assert [row["missing_reason_raw"] for row in result.measurements] == ["Нет", "CD"]


@pytest.mark.parametrize("bad", ["1,5", "-10", "0", "nan", "inf", "1e3", "abc", "+5"])
def test_invalid_numbers_are_rejected(tmp_path: Path, bad: str) -> None:
    result = run(tmp_path, [record(distance_km=bad)])

    assert result.measurements == []
    assert result.rejected[0]["reject_reason"] == "invalid_number"
    assert result.rejected[0]["detail"] == f"distance_km={bad}"


@pytest.mark.parametrize(
    ("fields", "reason", "detail"),
    [
        ({"distance_hr": ""}, "partial_measurement", "distance_hr="),
        ({"distance_km": "", "distance_hr": "bad"}, "partial_measurement", "distance_km="),
        (
            {"distance_km": "", "distance_hr": "", "missing_reason": ""},
            "missing_without_reason",
            "missing_reason=",
        ),
        ({"transport_type": "bus"}, "unknown_transport_type", "transport_type=bus"),
    ],
)
def test_row_level_rejections(
    tmp_path: Path, fields: dict[str, str], reason: str, detail: str
) -> None:
    result = run(tmp_path, [record(**fields)])

    assert result.reasons() == [reason]
    assert result.rejected[0]["detail"] == detail
    assert result.report["status"] == "success_with_rejections"
    assert result.report["rejections"][reason] == 1


def test_self_pair_is_rejected(tmp_path: Path) -> None:
    result = run(tmp_path, [record(BRYANSK, BRYANSK)])

    assert result.reasons() == ["self_pair"]


def test_first_problem_is_the_reason_and_detail_lists_all(tmp_path: Path) -> None:
    result = run(
        tmp_path,
        [record(sber_territory_id_to="", transport_type="bus", distance_km="x", distance_hr="-1")],
    )

    assert result.reasons() == ["city_identity_missing"]
    assert result.rejected[0]["detail"] == (
        "sber_territory_id_to=; transport_type=bus; distance_km=x; distance_hr=-1"
    )


def test_rejected_row_keeps_raw_values_in_file_order(tmp_path: Path) -> None:
    result = run(tmp_path, [record(transport_type=" bus ", city_from=" Брянск ")])

    raw = json.loads(result.rejected[0]["raw_json"])
    assert list(raw) == list(SOURCE_COLUMNS)
    assert raw["transport_type"] == " bus "
    assert raw["city_from"] == " Брянск "
    assert "\\u" not in result.rejected[0]["raw_json"]
    assert result.rejected[0]["source_row_number"] == "2"


def test_text_fields_are_trimmed_in_output(tmp_path: Path) -> None:
    result = run(tmp_path, [record(source=" OSM ", city_from="  Брянск ")])

    assert result.measurements[0]["source_raw"] == "OSM"
    assert result.city("953")["name"] == "Брянск"


def test_quoted_line_break_keeps_start_line_of_each_record(tmp_path: Path) -> None:
    rows = [record(comment='"первая\nвторая"'), record(transport_type="rail")]
    result = run(tmp_path, rows)

    assert [row["source_row_number"] for row in result.measurements] == ["2", "4"]
    assert result.measurements[0]["comment_raw"] == "первая\nвторая"


# --- section 7.3: synthetic scenarios S1-S10 ---------------------------------------------


def test_s1_sber_id_with_two_oktmo_nulls_oktmo(tmp_path: Path) -> None:
    other_oktmo = {**BRYANSK, "oktmo": "15000000000"}
    result = run(tmp_path, [record(BRYANSK, VLADIMIR), record(other_oktmo, TULA)])

    assert result.city("953")["oktmo"] == ""
    assert result.city("986")["oktmo"] == "17701000001"
    assert len(result.measurements) == 2
    conflicts = result.report["identity"]["oktmo_conflicts"]
    assert conflicts["sber_id_with_several_oktmo"] == [
        {"source_city_id": "953", "oktmo": ["15000000000", "15701000001"]}
    ]


def test_s2_oktmo_shared_by_two_sber_ids_nulls_both(tmp_path: Path) -> None:
    shared = {**VLADIMIR, "oktmo": BRYANSK["oktmo"]}
    result = run(tmp_path, [record(BRYANSK, shared)])

    assert result.city("953")["oktmo"] == ""
    assert result.city("986")["oktmo"] == ""
    assert result.report["identity"]["oktmo_conflicts"]["oktmo_with_several_sber_id"] == [
        {"oktmo": "15701000001", "source_city_ids": ["953", "986"]}
    ]


def test_s3_region_tie_gives_null(tmp_path: Path) -> None:
    other_region = {**BRYANSK, "region": "Другая область"}
    result = run(tmp_path, [record(BRYANSK, VLADIMIR), record(other_region, TULA)])

    assert result.city("953")["region"] == ""
    assert {
        "source_city_id": "953",
        "attribute": "region",
        "values": {"Брянская область": 1, "Другая область": 1},
        "chosen": None,
    } in result.report["identity"]["attribute_conflicts"]


def test_s4_invalid_population_does_not_vote(tmp_path: Path) -> None:
    rows = [
        record({**BRYANSK, "population": bad}, target, transport_type=transport)
        for bad, target, transport in (
            ("-5", VLADIMIR, "road"),
            ("1.5", VLADIMIR, "rail"),
            ("много", VLADIMIR, "air"),
        )
    ]
    rows.append(record({**BRYANSK, "population": "100000"}, TULA))
    result = run(tmp_path, rows)

    assert result.city("953")["population"] == "100000"
    assert len(result.measurements) == 4
    invalid = [
        item["value"]
        for item in result.report["identity"]["invalid_attributes"]
        if item["source_city_id"] == "953"
    ]
    assert sorted(invalid) == ["-5", "1.5", "много"]


def test_s5_exact_repeat_with_other_source_keeps_first_row(tmp_path: Path) -> None:
    result = run(tmp_path, [record(), record(source="OSRM", comment="другой")])

    assert [row["source_row_number"] for row in result.measurements] == ["2"]
    assert result.reasons() == ["exact_duplicate"]
    assert result.rejected[0]["detail"] == "kept_source_row_number=2"
    differences = result.report["duplicates"]["source_differences"]
    assert [row["source_raw"] for row in differences[0]["rows"]] == ["OSM", "OSRM"]


def test_s6_different_numbers_reject_whole_group(tmp_path: Path) -> None:
    result = run(tmp_path, [record(distance_km="100"), record(distance_km="120")])

    assert result.measurements == []
    assert result.reasons() == ["key_collision", "key_collision"]
    assert result.report["duplicates"]["key_collision"] == 2


def test_s7_different_missing_reason_is_a_collision(tmp_path: Path) -> None:
    result = run(tmp_path, [record(missing_reason="CD"), record(missing_reason="ND")])

    assert result.measurements == []
    assert result.reasons() == ["key_collision", "key_collision"]


def test_s8_partial_coordinates_null_both_without_rejecting(tmp_path: Path) -> None:
    result = run(tmp_path, [record({**BRYANSK, "lon": ""}, VLADIMIR)])

    bryansk = result.city("953")
    assert (bryansk["latitude"], bryansk["longitude"]) == ("", "")
    assert len(result.measurements) == 1
    assert result.report["identity"]["invalid_attributes"] == [
        {
            "source_city_id": "953",
            "attribute": "coordinates",
            "value": "53.2420053,",
            "count": 1,
        }
    ]


def test_s9_three_row_group_with_one_difference_is_rejected_entirely(tmp_path: Path) -> None:
    rows = [record(distance_km="100"), record(distance_km="100"), record(distance_km="120")]
    result = run(tmp_path, rows)

    assert result.measurements == []
    assert result.reasons() == ["key_collision"] * 3
    assert result.rejected[0]["detail"] == "source_row_numbers=2,3,4"


def test_s10_missing_sber_id_rejects_row_and_city(tmp_path: Path) -> None:
    result = run(tmp_path, [record(BRYANSK, {**VLADIMIR, "sber_territory_id": ""})])

    assert result.reasons() == ["city_identity_missing"]
    assert [row["source_city_id"] for row in result.cities] == ["953"]
    assert result.city("953")["city_id"] == city_id("953")


# --- sections 1 and 4: failed runs publish nothing ---------------------------------------


@pytest.mark.parametrize(
    ("corrupt", "message"),
    [
        (lambda raw: (raw / CSV_NAME).write_bytes(b"changed"), "does not match manifest"),
        (lambda raw: write_snapshot(raw, b"city_from;region_from\n"), "Unexpected CSV header"),
        (lambda raw: write_snapshot(raw, csv_bytes([record()]), row_count=5), "manifest says 5"),
        (lambda raw: (raw / MANIFEST_FILENAME).unlink(), "Cannot read manifest"),
    ],
)
def test_failed_run_keeps_previous_output(tmp_path: Path, corrupt: Any, message: str) -> None:
    raw_dir = write_snapshot(tmp_path / "raw", csv_bytes([record()]))
    process_snapshot(raw_dir, tmp_path / "out")
    published = {
        path.name: path.read_bytes() for path in (tmp_path / "out" / SNAPSHOT_ID).iterdir()
    }

    corrupt(raw_dir)
    with pytest.raises(ProcessingError, match=message):
        process_snapshot(raw_dir, tmp_path / "out")

    assert {
        path.name: path.read_bytes() for path in (tmp_path / "out" / SNAPSHOT_ID).iterdir()
    } == published
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == [SNAPSHOT_ID]


def test_bom_is_accepted_and_reported(tmp_path: Path) -> None:
    raw_dir = write_snapshot(tmp_path / "raw", b"\xef\xbb\xbf" + csv_bytes([record()]))
    report = process_snapshot(raw_dir, tmp_path / "out")

    assert report["input"]["bom"] is True
    assert report["balance"]["route_measurements"] == 1


def test_cli_process_writes_report(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    raw_dir = write_snapshot(tmp_path / "raw", csv_bytes([record()]))
    exit_code = cli_main(
        ["process", "--input-dir", str(raw_dir), "--output-root", str(tmp_path / "out")]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "success"


def test_cli_process_reports_failure(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    exit_code = cli_main(["process", "--input-dir", str(tmp_path / "missing")])

    assert exit_code == 1
    assert "status failed" in capsys.readouterr().err


def test_cli_prints_utf8_json_regardless_of_console_encoding(tmp_path: Path) -> None:
    row = record(distance_km="", distance_hr="", missing_reason="Маршрут не найден")
    raw_dir = write_snapshot(tmp_path / "raw", csv_bytes([row]))
    command = [sys.executable, "-m", "transport_connectivity.cli", "process"]
    command += ["--input-dir", str(raw_dir), "--output-root", str(tmp_path / "out")]
    completed = subprocess.run(
        command, capture_output=True, check=True, env={**os.environ, "PYTHONIOENCODING": "cp1251"}
    )

    report = json.loads(completed.stdout.decode("utf-8"))
    assert report["missing_reason_values"] == {"Маршрут не найден": 1}
    assert report["identity"]["cities"]["total"] == 2
