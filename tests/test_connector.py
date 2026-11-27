"""Offline tests for the full dataset connector: no test touches the network."""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
import zipfile
from pathlib import Path

import pytest

from transport_connectivity import connector
from transport_connectivity.cli import main as cli_main
from transport_connectivity.connector import (
    ARCHIVE_FILENAME,
    MANIFEST_FILENAME,
    SNAPSHOT_ID,
    SOURCE_COLUMNS,
    ConnectorError,
    fetch_full_dataset,
)

CSV_NAME = "data_intercity_connectivity_162_v20260629.csv"
HEADER = ";".join(SOURCE_COLUMNS)
ROWS = [
    "Брянск;Брянская область;15701000001;953;1;379152;53.2420053;34.3653002;"
    "Владимир;Владимирская область;17701000001;986;1;349951;56.1280444;40.4082983;"
    "road;694.0;9.8;CD;OSM;CD",
    "Владимир;Владимирская область;17701000001;986;1;349951;56.1280444;40.4082983;"
    "Брянск;Брянская область;15701000001;953;1;379152;53.2420053;34.3653002;"
    "air;;;Маршрут не найден;ND;CD",
]
CSV_BYTES = ("\n".join([HEADER, *ROWS]) + "\n").encode("utf-8")


def make_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def serve(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> list[str]:
    """Replace the HTTP download with a fixed payload and record requested URLs."""
    calls: list[str] = []

    def fake_download(url: str, **kwargs: object) -> bytes:
        calls.append(url)
        return payload

    monkeypatch.setattr(connector, "download_archive", fake_download)
    return calls


def fail_urlopen(monkeypatch: pytest.MonkeyPatch, error: Exception) -> list[int]:
    attempts: list[int] = []

    def fake_urlopen(*args: object, **kwargs: object) -> object:
        attempts.append(1)
        raise error

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return attempts


def test_download_writes_archive_csv_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = serve(monkeypatch, make_zip({CSV_NAME: CSV_BYTES}))

    result = fetch_full_dataset(tmp_path, url="https://example.test/data.zip")

    assert calls == ["https://example.test/data.zip"]
    assert result.used_cache is False
    manifest = json.loads((tmp_path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["snapshot_id"] == SNAPSHOT_ID == "tochno_intercity_v20260629"
    assert manifest["source_url"] == "https://example.test/data.zip"
    assert manifest["row_count"] == 2
    assert manifest["city_count"] == 2
    assert manifest["columns"] == list(SOURCE_COLUMNS)
    assert manifest["csv_sha256"] == connector.sha256_bytes(CSV_BYTES)
    assert manifest["baseline"]["rows_match"] is False
    assert (tmp_path / CSV_NAME).read_bytes() == CSV_BYTES
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [ARCHIVE_FILENAME, CSV_NAME, MANIFEST_FILENAME]
    )


def test_valid_cache_is_reused_offline_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, make_zip({CSV_NAME: CSV_BYTES}))
    first = fetch_full_dataset(tmp_path)
    (tmp_path / CSV_NAME).unlink()

    def forbidden(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("network must not be used when the cache is valid")

    monkeypatch.setattr(connector, "download_archive", forbidden)
    second = fetch_full_dataset(tmp_path, offline=True)

    assert second.used_cache is True
    assert second.manifest == first.manifest
    assert (tmp_path / CSV_NAME).read_bytes() == CSV_BYTES


def test_offline_without_cache_fails_in_cli(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert cli_main(["download", "--output-dir", str(tmp_path), "--offline"]) == 1
    assert "no valid cached archive" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_transient_network_errors_are_retried_a_bounded_number_of_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = fail_urlopen(monkeypatch, urllib.error.URLError("timed out"))

    with pytest.raises(ConnectorError, match="after 3 attempt"):
        fetch_full_dataset(tmp_path, max_retries=3, backoff_seconds=0)

    assert len(attempts) == 3
    assert list(tmp_path.iterdir()) == []


def test_client_http_error_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    error = urllib.error.HTTPError(
        "https://example.test", 404, "Not Found", email.message.Message(), None
    )
    attempts = fail_urlopen(monkeypatch, error)

    with pytest.raises(ConnectorError, match="after 1 attempt"):
        fetch_full_dataset(tmp_path, max_retries=3, backoff_seconds=0)

    assert len(attempts) == 1


def test_corrupted_zip_is_rejected_and_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, b"not a zip archive")

    with pytest.raises(ConnectorError, match="not a valid ZIP"):
        fetch_full_dataset(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_unexpected_schema_is_rejected_and_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reordered = ";".join(reversed(SOURCE_COLUMNS))
    serve(monkeypatch, make_zip({CSV_NAME: f"{reordered}\n".encode()}))

    with pytest.raises(ConnectorError, match="Unexpected CSV header"):
        fetch_full_dataset(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_record_with_wrong_field_count_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, make_zip({CSV_NAME: f"{HEADER}\n{ROWS[0]};extra\n".encode()}))

    with pytest.raises(ConnectorError, match="has 23 fields"):
        fetch_full_dataset(tmp_path)


def test_archive_with_several_csv_files_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, make_zip({"a.csv": CSV_BYTES, "b.csv": CSV_BYTES}))

    with pytest.raises(ConnectorError, match="exactly one CSV"):
        fetch_full_dataset(tmp_path)


def test_corrupted_cache_is_downloaded_again_online(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ARCHIVE_FILENAME).write_bytes(b"truncated")
    calls = serve(monkeypatch, make_zip({CSV_NAME: CSV_BYTES}))

    result = fetch_full_dataset(tmp_path)

    assert len(calls) == 1
    assert result.used_cache is False
    assert result.manifest.row_count == 2
