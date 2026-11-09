"""Connector for downloading and verifying the full intercity connectivity dataset."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import sleep
from typing import Any

DATASET_VERSION = "20260629"
SNAPSHOT_ID = f"intercity-{DATASET_VERSION}"
DATASET_URL = (
    "https://storage.yandexcloud.net/tochno-st-catalog/Geo/"
    "data_intercity_connectivity_162_v20260629/"
    "data_intercity_connectivity_162_v20260629_csv.zip"
)
DATASET_PAGE_URL = "https://tochno.st/datasets/intercity_connectivity"
ARCHIVE_FILENAME = "data_intercity_connectivity_162_v20260629_csv.zip"
MANIFEST_FILENAME = "manifest.json"
USER_AGENT = (
    "TransportConnectivity/0.1 (educational project; GitHub: AlexKeyyyy/transport-connectivity)"
)

BASELINE_EXPECTED_ROWS = 96_660
BASELINE_EXPECTED_COLUMNS = 22
BASELINE_EXPECTED_CITIES = 180

REQUIRED_MIN_COLUMNS = {"city_from", "city_to", "transport_type", "distance_km"}


class ConnectorError(RuntimeError):
    """Raised when dataset download, extraction, or verification fails."""


@dataclass(frozen=True)
class DatasetManifest:
    """Metadata and integrity hashes for the extracted raw dataset."""

    snapshot_id: str
    dataset_version: str
    source_url: str
    page_url: str
    downloaded_at_utc: str
    used_cache: bool
    archive_filename: str
    archive_sha256: str
    archive_size_bytes: int
    csv_member: str
    csv_filename: str
    csv_sha256: str
    csv_size_bytes: int
    encoding: str
    delimiter: str
    row_count: int
    column_count: int
    columns: list[str]
    unique_cities_from: int
    unique_cities_to: int
    unique_cities_total: int
    baseline_comparison: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def download_archive_bytes(
    url: str,
    *,
    timeout: int = 120,
    max_retries: int = 3,
    backoff_seconds: float = 2.0,
) -> bytes:
    """Download raw archive bytes with bounded retries and clear error reporting."""
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1")

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/zip, application/octet-stream, */*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
                if not data:
                    raise ConnectorError(f"Empty response body received from {url}")
                return data
        except (urllib.error.URLError, TimeoutError, OSError, ConnectorError) as error:
            last_error = error
            if attempt < max_retries and backoff_seconds > 0:
                sleep(backoff_seconds)

    raise ConnectorError(
        f"Failed to download dataset from {url} after {max_retries} attempts: {last_error}"
    ) from last_error


def select_single_csv_member(archive: zipfile.ZipFile) -> str:
    """Select the exact CSV member from the archive; fail if ambiguous or missing."""
    candidates = [
        name
        for name in archive.namelist()
        if not name.endswith("/")
        and Path(name).name.endswith(".csv")
        and not Path(name).name.startswith((".", "__"))
        and "__MACOSX" not in Path(name).parts
    ]
    if not candidates:
        raise ConnectorError("Archive contains no valid CSV file.")
    if len(candidates) > 1:
        joined = ", ".join(sorted(candidates))
        raise ConnectorError(
            f"Ambiguous archive contents: expected 1 CSV member, found {len(candidates)} ({joined})"
        )
    return candidates[0]


def verify_and_inspect_csv(
    csv_bytes: bytes,
    *,
    delimiter: str = ";",
) -> tuple[list[str], int, int, int, int]:
    """Validate CSV encoding, header, and inspect row/city counts without filtering."""
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ConnectorError(f"CSV bytes are not valid UTF-8: {error}") from error

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    columns = list(reader.fieldnames or [])
    if not columns:
        raise ConnectorError("Extracted CSV file has an empty or missing header.")

    missing_required = sorted(REQUIRED_MIN_COLUMNS - set(columns))
    if missing_required:
        raise ConnectorError(
            f"CSV schema is missing required columns: {', '.join(missing_required)}"
        )

    row_count = 0
    cities_from: set[str] = set()
    cities_to: set[str] = set()
    for row_num, row in enumerate(reader, start=1):
        if None in row:
            raise ConnectorError(f"Malformed CSV row {row_num}: contains more fields than header.")
        row_count += 1
        c_from = (row.get("city_from") or "").strip()
        c_to = (row.get("city_to") or "").strip()
        if c_from:
            cities_from.add(c_from)
        if c_to:
            cities_to.add(c_to)

    if row_count == 0:
        raise ConnectorError("Extracted CSV contains 0 data rows.")

    all_cities = cities_from | cities_to
    return columns, row_count, len(cities_from), len(cities_to), len(all_cities)


def extract_and_validate_zip(archive_path: Path) -> tuple[str, bytes]:
    """Verify ZIP integrity and return (csv_member_name, raw_csv_bytes)."""
    if not archive_path.is_file() or archive_path.stat().st_size == 0:
        raise ConnectorError(f"Archive file is missing or empty: {archive_path}")

    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ConnectorError(f"Corrupted member inside ZIP archive: {bad_member}")
            csv_member = select_single_csv_member(archive)
            csv_bytes = archive.read(csv_member)
    except zipfile.BadZipFile as error:
        raise ConnectorError(
            f"Invalid or corrupted ZIP archive {archive_path.name}: {error}"
        ) from error

    if not csv_bytes:
        raise ConnectorError(f"Extracted CSV member '{csv_member}' is empty.")
    return csv_member, csv_bytes


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write bytes to a temporary file in the same directory and atomically replace target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False, suffix=".part") as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    tmp_path.replace(target)


def fetch_full_dataset(
    output_dir: Path,
    *,
    url: str = DATASET_URL,
    offline: bool = False,
    timeout: int = 120,
    max_retries: int = 3,
    backoff_seconds: float = 2.0,
) -> DatasetManifest:
    """Download or reuse cached full dataset ZIP, extract exact CSV bytes, and write manifest."""
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / ARCHIVE_FILENAME
    manifest_path = output_dir / MANIFEST_FILENAME

    used_cache = False
    downloaded_at_utc = datetime.now(UTC).isoformat(timespec="seconds")

    if archive_path.exists():
        try:
            csv_member, csv_bytes = extract_and_validate_zip(archive_path)
            used_cache = True
            if manifest_path.exists():
                try:
                    prev = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if isinstance(prev.get("downloaded_at_utc"), str):
                        downloaded_at_utc = prev["downloaded_at_utc"]
                except (OSError, ValueError):
                    pass
        except ConnectorError:
            if offline:
                raise
            # Cached archive is corrupted and online mode is allowed: re-download.
            used_cache = False

    if not used_cache:
        if offline:
            raise ConnectorError(
                f"Offline mode requested (--offline), but no valid cached archive found at "
                f"{archive_path.as_posix()}"
            )
        raw_zip_bytes = download_archive_bytes(
            url,
            timeout=timeout,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
        )
        with tempfile.NamedTemporaryFile(
            dir=output_dir, delete=False, suffix=".zip.part"
        ) as tmp_zip:
            tmp_zip.write(raw_zip_bytes)
            tmp_zip_path = Path(tmp_zip.name)
        try:
            csv_member, csv_bytes = extract_and_validate_zip(tmp_zip_path)
            tmp_zip_path.replace(archive_path)
        except Exception:
            tmp_zip_path.unlink(missing_ok=True)
            raise
        downloaded_at_utc = datetime.now(UTC).isoformat(timespec="seconds")

    columns, row_count, cities_from, cities_to, cities_total = verify_and_inspect_csv(csv_bytes)
    csv_filename = Path(csv_member).name
    csv_path = output_dir / csv_filename

    if not csv_path.exists() or sha256_file(csv_path) != sha256_bytes(csv_bytes):
        atomic_write_bytes(csv_path, csv_bytes)

    archive_sha256 = sha256_file(archive_path)
    csv_sha256 = sha256_bytes(csv_bytes)

    baseline_comparison = {
        "expected_rows": BASELINE_EXPECTED_ROWS,
        "actual_rows": row_count,
        "rows_match_baseline": row_count == BASELINE_EXPECTED_ROWS,
        "expected_columns": BASELINE_EXPECTED_COLUMNS,
        "actual_columns": len(columns),
        "columns_match_baseline": len(columns) == BASELINE_EXPECTED_COLUMNS,
        "expected_cities": BASELINE_EXPECTED_CITIES,
        "actual_cities_total": cities_total,
        "cities_match_baseline": cities_total == BASELINE_EXPECTED_CITIES,
    }

    manifest = DatasetManifest(
        snapshot_id=SNAPSHOT_ID,
        dataset_version=DATASET_VERSION,
        source_url=url,
        page_url=DATASET_PAGE_URL,
        downloaded_at_utc=downloaded_at_utc,
        used_cache=used_cache,
        archive_filename=ARCHIVE_FILENAME,
        archive_sha256=archive_sha256,
        archive_size_bytes=archive_path.stat().st_size,
        csv_member=csv_member,
        csv_filename=csv_filename,
        csv_sha256=csv_sha256,
        csv_size_bytes=len(csv_bytes),
        encoding="UTF-8",
        delimiter=";",
        row_count=row_count,
        column_count=len(columns),
        columns=columns,
        unique_cities_from=cities_from,
        unique_cities_to=cities_to,
        unique_cities_total=cities_total,
        baseline_comparison=baseline_comparison,
    )

    atomic_write_bytes(
        manifest_path,
        (json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return manifest
