"""Download and verify the full «Если быть точным» intercity connectivity CSV.

The connector fetches the pinned source archive, extracts its single CSV member,
checks the exact header and writes a manifest next to the files. Normalization is a
separate step (``processor.py``) that trusts only files described by a manifest.
"""

from __future__ import annotations

import csv
import hashlib
import http.client
import io
import json
import os
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
SNAPSHOT_ID = f"tochno_intercity_v{DATASET_VERSION}"
DATASET_URL = (
    "https://storage.yandexcloud.net/tochno-st-catalog/Geo/"
    "data_intercity_connectivity_162_v20260629/"
    "data_intercity_connectivity_162_v20260629_csv.zip"
)
DATASET_PAGE_URL = "https://tochno.st/datasets/intercity_connectivity"
ARCHIVE_FILENAME = "data_intercity_connectivity_162_v20260629_csv.zip"
MANIFEST_FILENAME = "manifest.json"
DEFAULT_RAW_DIR = Path("data/raw") / SNAPSHOT_ID
CSV_ENCODING = "utf-8"
CSV_DELIMITER = ";"
USER_AGENT = (
    "TransportConnectivity/0.1 (educational project; GitHub: AlexKeyyyy/transport-connectivity)"
)

# Exact header of the source CSV in file order (docs/data-contract.md, section 1).
SOURCE_COLUMNS = (
    "city_from",
    "region_from",
    "oktmo_from",
    "sber_territory_id_from",
    "is_capital_from",
    "population_from",
    "lat_from",
    "lon_from",
    "city_to",
    "region_to",
    "oktmo_to",
    "sber_territory_id_to",
    "is_capital_to",
    "population_to",
    "lat_to",
    "lon_to",
    "transport_type",
    "distance_km",
    "distance_hr",
    "missing_reason",
    "source",
    "comment",
)

# Published size of version 20260629. It is reported for comparison and never
# enforced: a different count is a finding to explain, not a reason to reject the file.
BASELINE_ROWS = 96_660
BASELINE_CITIES = 180

# Client errors (404, 403, ...) will not change on retry, so only these are repeated.
RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class ConnectorError(RuntimeError):
    """Download, extraction or verification of the source dataset failed."""


@dataclass(frozen=True)
class DatasetManifest:
    """Provenance and integrity of one extracted source snapshot."""

    snapshot_id: str
    dataset_version: str
    source_url: str
    page_url: str
    downloaded_at_utc: str
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
    columns: list[str]
    # Distinct Sber territory IDs over both ends; city names are not identifiers.
    city_count: int
    baseline: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FetchResult:
    manifest: DatasetManifest
    used_cache: bool


@dataclass(frozen=True)
class CsvSummary:
    row_count: int
    city_count: int


@dataclass(frozen=True)
class VerifiedArchive:
    archive_bytes: bytes
    csv_member: str
    csv_bytes: bytes
    summary: CsvSummary


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 16):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write to a temporary file next to ``target`` and move it into place."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".part")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        tmp_path.replace(target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in RETRYABLE_HTTP_STATUSES
    return isinstance(
        error, urllib.error.URLError | TimeoutError | ConnectionError | http.client.IncompleteRead
    )


def download_archive(
    url: str,
    *,
    timeout: float = 120,
    max_retries: int = 3,
    backoff_seconds: float = 2.0,
) -> bytes:
    """Download the archive, repeating only transient network failures."""
    if max_retries < 1:
        raise ValueError("max_retries must be at least 1")

    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/zip, */*"}
    )
    attempt = 1
    while True:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (OSError, http.client.IncompleteRead) as error:
            if not _is_retryable(error) or attempt >= max_retries:
                raise ConnectorError(
                    f"Download of {url} failed after {attempt} attempt(s): {error}"
                ) from error
        sleep(backoff_seconds * attempt)
        attempt += 1


def extract_csv(archive_bytes: bytes) -> tuple[str, bytes]:
    """Check ZIP integrity and return the name and bytes of its only CSV member."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            broken_member = archive.testzip()
            if broken_member is not None:
                raise ConnectorError(f"ZIP member is corrupted: {broken_member}")
            members = [
                name
                for name in archive.namelist()
                if name.endswith(".csv") and "__MACOSX" not in Path(name).parts
            ]
            if len(members) != 1:
                raise ConnectorError(f"Expected exactly one CSV in the archive, found {members}")
            return members[0], archive.read(members[0])
    except zipfile.BadZipFile as error:
        raise ConnectorError(f"Archive is not a valid ZIP file: {error}") from error


def inspect_csv(csv_bytes: bytes) -> CsvSummary:
    """Check encoding, exact header and field counts; count records and cities."""
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ConnectorError(f"CSV is not valid UTF-8: {error}") from error

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=CSV_DELIMITER)
    header = next(reader, None)
    if header is None or tuple(header) != SOURCE_COLUMNS:
        raise ConnectorError(
            f"Unexpected CSV header: expected {len(SOURCE_COLUMNS)} columns "
            f"{list(SOURCE_COLUMNS)}, got {header}"
        )

    sber_from = SOURCE_COLUMNS.index("sber_territory_id_from")
    sber_to = SOURCE_COLUMNS.index("sber_territory_id_to")
    row_count = 0
    sber_ids: set[str] = set()
    for row in reader:
        if len(row) != len(SOURCE_COLUMNS):
            raise ConnectorError(
                f"CSV record ending at line {reader.line_num} has {len(row)} fields, "
                f"expected {len(SOURCE_COLUMNS)}"
            )
        row_count += 1
        sber_ids.update(value.strip() for value in (row[sber_from], row[sber_to]) if value.strip())

    if row_count == 0:
        raise ConnectorError("CSV contains no data records")
    return CsvSummary(row_count=row_count, city_count=len(sber_ids))


def verify_archive(archive_bytes: bytes) -> VerifiedArchive:
    csv_member, csv_bytes = extract_csv(archive_bytes)
    return VerifiedArchive(archive_bytes, csv_member, csv_bytes, inspect_csv(csv_bytes))


def load_manifest(path: Path) -> DatasetManifest:
    try:
        return DatasetManifest(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as error:
        raise ConnectorError(f"Cannot read manifest {path.as_posix()}: {error}") from error


def _cached_download_time(output_dir: Path, archive_path: Path, archive_sha256: str) -> str:
    """Keep the original download time when the cache matches its previous manifest."""
    manifest_path = output_dir / MANIFEST_FILENAME
    if manifest_path.is_file():
        try:
            previous = load_manifest(manifest_path)
        except ConnectorError:
            previous = None
        if previous is not None and previous.archive_sha256 == archive_sha256:
            return previous.downloaded_at_utc
    # Without a matching manifest the file write time is the best available evidence.
    modified = datetime.fromtimestamp(archive_path.stat().st_mtime, UTC)
    return modified.isoformat(timespec="seconds")


def fetch_full_dataset(
    output_dir: Path = DEFAULT_RAW_DIR,
    *,
    url: str = DATASET_URL,
    offline: bool = False,
    timeout: float = 120,
    max_retries: int = 3,
    backoff_seconds: float = 2.0,
) -> FetchResult:
    """Reuse a valid cached archive or download it, then extract the CSV and a manifest.

    Files are verified in memory before anything is written, so an invalid download never
    becomes a cache. The manifest is written last: a directory without it is not a
    finished snapshot.
    """
    archive_path = output_dir / ARCHIVE_FILENAME
    verified: VerifiedArchive | None = None
    cache_error: ConnectorError | None = None
    if archive_path.is_file():
        try:
            verified = verify_archive(archive_path.read_bytes())
        except ConnectorError as error:
            cache_error = error

    used_cache = verified is not None
    if verified is None:
        if offline:
            reason = f": {cache_error}" if cache_error else ""
            raise ConnectorError(
                f"Offline mode: no valid cached archive at {archive_path.as_posix()}{reason}"
            )
        verified = verify_archive(
            download_archive(
                url, timeout=timeout, max_retries=max_retries, backoff_seconds=backoff_seconds
            )
        )
        atomic_write_bytes(archive_path, verified.archive_bytes)

    archive_sha256 = sha256_bytes(verified.archive_bytes)
    if used_cache:
        downloaded_at_utc = _cached_download_time(output_dir, archive_path, archive_sha256)
    else:
        downloaded_at_utc = datetime.now(UTC).isoformat(timespec="seconds")

    summary = verified.summary
    csv_filename = Path(verified.csv_member).name
    atomic_write_bytes(output_dir / csv_filename, verified.csv_bytes)

    manifest = DatasetManifest(
        snapshot_id=SNAPSHOT_ID,
        dataset_version=DATASET_VERSION,
        source_url=url,
        page_url=DATASET_PAGE_URL,
        downloaded_at_utc=downloaded_at_utc,
        archive_filename=ARCHIVE_FILENAME,
        archive_sha256=archive_sha256,
        archive_size_bytes=len(verified.archive_bytes),
        csv_member=verified.csv_member,
        csv_filename=csv_filename,
        csv_sha256=sha256_bytes(verified.csv_bytes),
        csv_size_bytes=len(verified.csv_bytes),
        encoding=CSV_ENCODING,
        delimiter=CSV_DELIMITER,
        row_count=summary.row_count,
        columns=list(SOURCE_COLUMNS),
        city_count=summary.city_count,
        baseline={
            "rows": BASELINE_ROWS,
            "cities": BASELINE_CITIES,
            "rows_match": summary.row_count == BASELINE_ROWS,
            "cities_match": summary.city_count == BASELINE_CITIES,
        },
    )
    manifest_json = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n"
    atomic_write_bytes(output_dir / MANIFEST_FILENAME, manifest_json.encode("utf-8"))
    return FetchResult(manifest=manifest, used_cache=used_cache)
