"""Unit tests for the dataset processor covering all contract and edge cases."""

from __future__ import annotations

import csv
from pathlib import Path

from transport_connectivity.connector import EXPECTED_SOURCE_COLUMNS
from transport_connectivity.processor import process_dataset


def make_row(**overrides: str) -> dict[str, str]:
    base = {
        "city_from": "Москва",
        "region_from": "Москва",
        "oktmo_from": "45000000000",
        "sber_territory_id_from": "2539",
        "is_capital_from": "1",
        "population_from": "13010112",
        "lat_from": "55.7540584",
        "lon_from": "37.62049",
        "city_to": "Тверь",
        "region_to": "Тверская область",
        "oktmo_to": "28701000001",
        "sber_territory_id_to": "2101",
        "is_capital_to": "1",
        "population_to": "416219",
        "lat_to": "56.8586556",
        "lon_to": "35.9118279",
        "transport_type": "road",
        "distance_km": "12",
        "distance_hr": "1.5",
        "missing_reason": "CD",
        "source": "OSRM",
        "comment": "CD",
    }
    base.update(overrides)
    return base


def write_input_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(EXPECTED_SOURCE_COLUMNS), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def test_processor_conversions_directions_homonyms_and_missing_routes(
    tmp_path: Path,
) -> None:
    input_csv = tmp_path / "input.csv"
    out_dir = tmp_path / "out"

    rows = [
        # 1. A -> B: 12 km -> 12000.0 m, 1.5 hr -> 5400.0 s, with missing_reason=CD
        make_row(distance_km="12", distance_hr="1.5", missing_reason="CD"),
        # 2. B -> A: reverse direction preserved
        make_row(
            city_from="Тверь",
            region_from="Тверская область",
            oktmo_from="28701000001",
            sber_territory_id_from="2101",
            population_from="416219",
            lat_from="56.8586556",
            lon_from="35.9118279",
            city_to="Москва",
            region_to="Москва",
            oktmo_to="45000000000",
            sber_territory_id_to="2539",
            population_to="13010112",
            lat_to="55.7540584",
            lon_to="37.62049",
            distance_km="12",
            distance_hr="1.5",
        ),
        # 3. Homonym city: same name "Киров", different sber_territory_id (1001 vs 1002)
        make_row(
            city_from="Киров",
            region_from="Кировская область",
            oktmo_from="33701000001",
            sber_territory_id_from="1001",
            population_from="471754",
            lat_from="58.6035",
            lon_from="49.6680",
            city_to="Киров",
            region_to="Калужская область",
            oktmo_to="29614101001",
            sber_territory_id_to="1002",
            population_to="28000",
            lat_to="54.0786",
            lon_to="34.2994",
            distance_km="1100",
            distance_hr="14.0",
        ),
        # 4. Allowed missing route (empty metrics + valid missing_reason)
        make_row(
            transport_type="air",
            distance_km="",
            distance_hr="",
            missing_reason="Маршрут не найден",
            source="ND",
        ),
    ]
    write_input_csv(input_csv, rows)

    report = process_dataset(input_csv, out_dir)
    assert report.rows_read == 4
    assert report.rows_written == 4
    assert report.rows_rejected == 0
    assert report.duplicates_removed == 0
    assert report.balance_valid is True
    assert report.cities_written == 4  # Москва, Тверь, Киров (1001), Киров (1002)
    assert report.missing_route_rows_written == 1

    with (out_dir / "route_measurements.csv").open(encoding="utf-8") as f:
        measurements = list(csv.DictReader(f))
    assert measurements[0]["distance_m"] == "12000.0"
    assert measurements[0]["duration_sec"] == "5400.0"
    assert measurements[0]["source_row_number"] == "1"
    assert measurements[3]["distance_m"] == ""
    assert measurements[3]["duration_sec"] == ""
    assert measurements[3]["missing_reason_raw"] == "Маршрут не найден"


def test_processor_duplicates_collisions_and_invalid_numbers(tmp_path: Path) -> None:
    input_csv = tmp_path / "input.csv"
    out_dir = tmp_path / "out1"
    out_dir_2 = tmp_path / "out2"

    rows = [
        # Row 1: valid baseline
        make_row(distance_km="100", distance_hr="2.0"),
        # Row 2: exact duplicate -> duplicates_removed
        make_row(distance_km="100", distance_hr="2.0"),
        # Row 3: semantic collision (same pair+transport, different distance) -> rejected
        make_row(distance_km="150", distance_hr="2.5"),
        # Row 4: negative number -> rejected
        make_row(transport_type="rail", distance_km="-10", distance_hr="2.0"),
        # Row 5: NaN/inf -> rejected
        make_row(transport_type="air", distance_km="nan", distance_hr="inf"),
        # Row 6: partial metrics (only distance_km present) -> rejected
        make_row(transport_type="bus", distance_km="200", distance_hr=""),
    ]
    write_input_csv(input_csv, rows)

    report1 = process_dataset(input_csv, out_dir)
    assert report1.rows_read == 6
    assert report1.rows_written == 1
    assert report1.duplicates_removed == 1
    assert report1.rows_rejected == 4
    assert report1.semantic_collisions_count == 1
    assert report1.balance_valid is True

    # Re-run reproducibility check: hashes of data files must match identically
    report2 = process_dataset(input_csv, out_dir_2)
    assert report1.output_files_sha256 == report2.output_files_sha256
