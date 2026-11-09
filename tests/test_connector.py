"""Offline unit tests for the full dataset connector and CLI."""

from __future__ import annotations

import io
import urllib.error
import zipfile
from pathlib import Path

import pytest

from transport_connectivity.cli import main as cli_main
from transport_connectivity.connector import (
    ARCHIVE_FILENAME,
    ConnectorError,
    fetch_full_dataset,
)

SAMPLE_CSV_CONTENT = (
    "city_from;region_from;city_to;region_to;transport_type;distance_km;duration_hours\n"
    "Москва;Москва;Тверь;Тверская область;car;178.5;2.4\n"
    "Тверь;Тверская область;Москва;Москва;train;167.0;1.8\n"
)


def make_zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def test_connector_download_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    zip_bytes = make_zip_bytes(
        {"data_intercity_connectivity_162_v20260629.csv": SAMPLE_CSV_CONTENT.encode("utf-8")}
    )
    calls = 0

    def fake_download(url: str, **kwargs: object) -> bytes:
        nonlocal calls
        calls += 1
        return zip_bytes

    monkeypatch.setattr("transport_connectivity.connector.download_archive_bytes", fake_download)

    manifest = fetch_full_dataset(tmp_path, url="https://example.test/dataset.zip")
    assert calls == 1
    assert not manifest.used_cache
    assert manifest.row_count == 2
    assert manifest.column_count == 7
    assert manifest.unique_cities_total == 2
    assert (tmp_path / ARCHIVE_FILENAME).is_file()
    assert (tmp_path / "data_intercity_connectivity_162_v20260629.csv").is_file()
    assert (tmp_path / "manifest.json").is_file()


def test_connector_reuses_cache_without_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    csv_name = "data_intercity_connectivity_162_v20260629.csv"
    zip_bytes = make_zip_bytes({csv_name: SAMPLE_CSV_CONTENT.encode("utf-8")})
    (tmp_path / ARCHIVE_FILENAME).write_bytes(zip_bytes)

    def forbidden_download(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("HTTP download should not be called when cache is valid")

    monkeypatch.setattr(
        "transport_connectivity.connector.download_archive_bytes", forbidden_download
    )

    manifest = fetch_full_dataset(tmp_path, offline=True)
    assert manifest.used_cache is True
    assert manifest.row_count == 2
    assert (tmp_path / csv_name).read_bytes() == SAMPLE_CSV_CONTENT.encode("utf-8")


def test_connector_offline_fails_when_cache_missing(tmp_path: Path) -> None:
    exit_code = cli_main(["download", "--output-dir", str(tmp_path), "--offline"])
    assert exit_code == 1


def test_connector_network_timeout_and_retry_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def failing_urlopen(*args: object, **kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", failing_urlopen)

    with pytest.raises(ConnectorError, match="Failed to download dataset"):
        fetch_full_dataset(
            tmp_path,
            url="https://example.test/dataset.zip",
            max_retries=2,
            backoff_seconds=0.0,
        )
    assert attempts == 2


def test_connector_corrupted_zip_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "transport_connectivity.connector.download_archive_bytes",
        lambda url, **kw: b"not-a-valid-zip-archive",
    )
    with pytest.raises(ConnectorError, match="Invalid or corrupted ZIP archive"):
        fetch_full_dataset(tmp_path)
    assert not (tmp_path / ARCHIVE_FILENAME).exists()


def test_connector_multiple_csv_members_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zip_bytes = make_zip_bytes(
        {
            "first.csv": SAMPLE_CSV_CONTENT.encode("utf-8"),
            "second.csv": SAMPLE_CSV_CONTENT.encode("utf-8"),
        }
    )
    monkeypatch.setattr(
        "transport_connectivity.connector.download_archive_bytes",
        lambda url, **kw: zip_bytes,
    )
    with pytest.raises(ConnectorError, match="Ambiguous archive contents"):
        fetch_full_dataset(tmp_path)


def test_connector_invalid_schema_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad_csv = b"col_a;col_b\n1;2\n"
    zip_bytes = make_zip_bytes({"only.csv": bad_csv})
    monkeypatch.setattr(
        "transport_connectivity.connector.download_archive_bytes",
        lambda url, **kw: zip_bytes,
    )
    with pytest.raises(ConnectorError, match="missing required columns"):
        fetch_full_dataset(tmp_path)
