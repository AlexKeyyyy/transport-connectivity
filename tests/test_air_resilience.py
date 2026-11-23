import json
from pathlib import Path
from uuid import UUID

import pytest

from transport_connectivity.air_resilience import MOSCOW_ID, compare_reachability
from transport_connectivity.cli import main
from transport_connectivity.connector import sha256_bytes

A, B, ISOLATED = (UUID(int=number) for number in (1, 2, 3))
CITIES = {A, MOSCOW_ID, B, ISOLATED}


def test_removing_moscow_only_counts_pairs_with_other_cities() -> None:
    via_moscow = {(A, MOSCOW_ID), (MOSCOW_ID, B)}
    assert compare_reachability(CITIES, via_moscow, MOSCOW_ID) == (1, 0)
    assert compare_reachability(CITIES, via_moscow | {(B, A)}, MOSCOW_ID) == (2, 1)


@pytest.fixture
def snapshots(tmp_path: Path) -> tuple[Path, Path]:
    cities_dir, rasp_dir = tmp_path / "cities", tmp_path / "rasp"
    cities_dir.mkdir()
    rasp_dir.mkdir()
    cities = ("city_id\n" + "\n".join(map(str, sorted(CITIES))) + "\n").encode()
    links = (
        "from_city_id,to_city_id,transport_type,snapshot_id\n"
        f"{A},{MOSCOW_ID},air,yandex_rasp_20261201\n"
        f"{MOSCOW_ID},{B},air,yandex_rasp_20261201\n"
        f"{A},{B},rail,yandex_rasp_20261201\n"
    ).encode()
    cities_hash = sha256_bytes(cities)
    (cities_dir / "cities.csv").write_bytes(cities)
    (rasp_dir / "direct_links.csv").write_bytes(links)
    (cities_dir / "run-report.json").write_text(
        json.dumps(
            {
                "status": "success",
                "snapshot_id": "tochno_intercity_v20260629",
                "output": {"cities.csv": {"sha256": cities_hash, "rows": 4}},
            }
        ),
        encoding="utf-8",
    )
    (rasp_dir / "run-report.json").write_text(
        json.dumps(
            {
                "status": "success",
                "source": "Яндекс Расписания",
                "snapshot_id": "yandex_rasp_20261201",
                "service_date": "2026-12-01",
                "input": {"cities_sha256": cities_hash},
                "output": {"direct_links.csv": {"sha256": sha256_bytes(links), "rows": 3}},
                "balance": {"direct_links_by_type": {"air": 2, "rail": 1}},
            }
        ),
        encoding="utf-8",
    )
    return cities_dir, rasp_dir


def _run_cli(cities_dir: Path, rasp_dir: Path, day: str = "2026-12-01") -> int:
    return main(
        [
            "rasp-air-resilience",
            "--date",
            day,
            "--cities-dir",
            str(cities_dir),
            "--rasp-dir",
            str(rasp_dir),
        ]
    )


def test_cli_uses_only_air_links(
    snapshots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_cli(*snapshots) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["air_link_count"] == 2
    assert result["cities_with_air_links"] == 3
    assert result["endpoint_city_count"] == 3
    assert result["reachable_pairs_before"] == 1
    assert result["reachable_pairs_after"] == 0
    assert result["lost_pairs"] == 1
    assert result["lost_pct_of_reachable_before"] == 100


@pytest.mark.parametrize(("index", "name"), [(0, "cities.csv"), (1, "direct_links.csv")])
def test_cli_rejects_modified_csv(
    snapshots: tuple[Path, Path], capsys: pytest.CaptureFixture[str], index: int, name: str
) -> None:
    path = snapshots[index] / name
    path.write_bytes(path.read_bytes() + b"\n")
    assert _run_cli(*snapshots) == 1
    captured = capsys.readouterr()
    assert f"{name} SHA-256" in captured.err
    assert captured.out == ""


def test_cli_rejects_cities_from_another_yandex_input(
    snapshots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    report_path = snapshots[1] / "run-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["input"]["cities_sha256"] = "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")
    assert _run_cli(*snapshots) == 1
    assert "does not match the Yandex Rasp input" in capsys.readouterr().err


def test_cli_rejects_wrong_service_date(
    snapshots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_cli(*snapshots, day="2026-12-02") == 1
    assert "does not describe the requested snapshot" in capsys.readouterr().err
