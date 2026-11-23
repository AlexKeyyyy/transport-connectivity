"""Normalize the «Если быть точным» CSV into cities and route measurements.

Implements docs/data-contract.md (version 1.0-draft). ``normalize`` is a pure function
over parsed records; ``process_snapshot`` adds the file boundary: it verifies the input
against its manifest, writes the outputs to a staging directory and publishes them as a
whole, so a failed run never leaves a partial result.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import tempfile
import time
import tracemalloc
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from typing import Any, NamedTuple

from transport_connectivity.connector import (
    DEFAULT_RAW_DIR,
    MANIFEST_FILENAME,
    SOURCE_COLUMNS,
    ConnectorError,
    load_manifest,
    sha256_bytes,
    sha256_file,
)

CONTRACT_VERSION = "1.0-draft"
DEFAULT_OUTPUT_ROOT = Path("data/normalized")

ID_NAMESPACE = "sber_territory"
# Fixed forever (contract, sections 2 and 3): changing them changes every stored ID.
NAMESPACE_CITY = uuid.UUID("de98ebe7-8f16-5a7f-b476-6c58b1f92076")
NAMESPACE_MEASUREMENT = uuid.UUID("3df2f4b5-a23f-5ed3-8ea1-7d25fe8ea191")

TRANSPORT_TYPES = ("road", "rail", "air")
REJECT_REASONS = (
    "city_identity_missing",
    "unknown_transport_type",
    "self_pair",
    "missing_without_reason",
    "partial_measurement",
    "invalid_number",
    "exact_duplicate",
    "key_collision",
)

CITIES_FILE = "cities.csv"
MEASUREMENTS_FILE = "route_measurements.csv"
REJECTED_FILE = "rejected_rows.csv"
REPORT_FILE = "run-report.json"

CITY_COLUMNS = (
    "city_id",
    "id_namespace",
    "source_city_id",
    "oktmo",
    "name",
    "region",
    "latitude",
    "longitude",
    "population",
)
MEASUREMENT_COLUMNS = (
    "measurement_id",
    "snapshot_id",
    "source_row_number",
    "from_city_id",
    "to_city_id",
    "transport_type",
    "distance_m",
    "duration_sec",
    "missing_reason_raw",
    "source_raw",
    "comment_raw",
)
REJECTED_COLUMNS = ("source_row_number", "reject_reason", "detail", "raw_json")

# Plain decimal notation only: no exponent, sign, comma separator, NaN or Infinity.
_UNSIGNED_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_SIGNED_DECIMAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_UNSIGNED_INTEGER = re.compile(r"[0-9]+")
# snapshot_id becomes a directory name, so it must not contain path separators.
_SNAPSHOT_ID = re.compile(r"[a-z0-9_]+")

Row = tuple[str | int | None, ...]
_BOM = "\N{ZERO WIDTH NO-BREAK SPACE}"
_COLUMN_INDEX = {column: index for index, column in enumerate(SOURCE_COLUMNS)}


class ProcessingError(RuntimeError):
    """The run failed: input does not match its manifest or an invariant broke."""


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One CSV record with the physical line number where it starts."""

    row_number: int
    # Raw values in SOURCE_COLUMNS order; a tuple keeps 96 660 records compact.
    fields: tuple[str, ...]

    def raw(self, column: str) -> str:
        return self.fields[_COLUMN_INDEX[column]]

    def value(self, column: str) -> str:
        return self.raw(column).strip()


class Problem(NamedTuple):
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class Measurement:
    source_row_number: int
    from_city_id: str
    to_city_id: str
    transport_type: str
    distance_m: str | None
    duration_sec: str | None
    missing_reason_raw: str | None
    source_raw: str | None
    comment_raw: str | None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.from_city_id, self.to_city_id, self.transport_type

    @property
    def values(self) -> tuple[str | None, str | None, str | None]:
        """Measured values; source and comment only explain them (contract, section 5)."""
        return self.distance_m, self.duration_sec, self.missing_reason_raw

    def to_row(self, snapshot_id: str) -> Row:
        return (
            measurement_id(snapshot_id, self.from_city_id, self.to_city_id, self.transport_type),
            snapshot_id,
            self.source_row_number,
            self.from_city_id,
            self.to_city_id,
            self.transport_type,
            self.distance_m,
            self.duration_sec,
            self.missing_reason_raw,
            self.source_raw,
            self.comment_raw,
        )


@dataclass
class _CityEvidence:
    """Every occurrence of one Sber ID at either end of any input record."""

    oktmo: set[str] = field(default_factory=set)
    name: Counter[str] = field(default_factory=Counter)
    region: Counter[str] = field(default_factory=Counter)
    population: Counter[int] = field(default_factory=Counter)
    coordinates: Counter[tuple[Decimal, Decimal]] = field(default_factory=Counter)


@dataclass(frozen=True)
class NormalizationResult:
    city_rows: list[Row]
    measurement_rows: list[Row]
    rejected_rows: list[Row]
    report: dict[str, Any]


# --- identifiers and values -------------------------------------------------------------


def city_id(source_city_id: str) -> str:
    return str(uuid.uuid5(NAMESPACE_CITY, f"{ID_NAMESPACE}:{source_city_id}"))


def measurement_id(
    snapshot_id: str, from_city_id: str, to_city_id: str, transport_type: str
) -> str:
    name = f"{snapshot_id}|{from_city_id}|{to_city_id}|{transport_type}"
    return str(uuid.uuid5(NAMESPACE_MEASUREMENT, name))


def format_decimal(value: Decimal) -> str:
    """Write a decimal without exponent and trailing zeros: 694000.0 -> 694000."""
    if value == 0:
        return "0"
    with localcontext() as context:
        # normalize() rounds to the context precision; keep every digit of the value.
        context.prec = len(value.as_tuple().digits)
        return format(value.normalize(), "f")


def scale_exactly(value: Decimal, factor: int) -> Decimal:
    """Multiply without the default 28-digit context rounding long inputs."""
    with localcontext() as context:
        context.prec = len(value.as_tuple().digits) + len(str(factor))
        context.traps[Inexact] = True
        return value * factor


def parse_positive(text: str) -> Decimal | None:
    if not _UNSIGNED_DECIMAL.fullmatch(text):
        return None
    value = Decimal(text)
    return value if value > 0 else None


def _parse_coordinates(lat: str, lon: str) -> tuple[Decimal, Decimal] | None:
    if not (_SIGNED_DECIMAL.fullmatch(lat) and _SIGNED_DECIMAL.fullmatch(lon)):
        return None
    latitude, longitude = Decimal(lat), Decimal(lon)
    if -90 <= latitude <= 90 and -180 <= longitude <= 180:
        return latitude, longitude
    return None


def _majority[T](votes: Counter[T]) -> T | None:
    """Value with strictly more than half of the votes, otherwise None."""
    total = sum(votes.values())
    if not total:
        return None
    value, count = votes.most_common(1)[0]
    return value if count * 2 > total else None


# --- reading ---------------------------------------------------------------------------


def read_records(path: Path) -> tuple[list[SourceRecord], bool]:
    """Read all records as strings; return them and whether the file starts with a BOM."""
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle, delimiter=";")
            header = next(reader, [])
            has_bom = bool(header) and header[0].startswith(_BOM)
            if has_bom:
                header[0] = header[0].removeprefix(_BOM)
            if tuple(header) != SOURCE_COLUMNS:
                raise ProcessingError(f"Unexpected CSV header: {header}")

            records: list[SourceRecord] = []
            start_line = reader.line_num + 1
            for values in reader:
                if len(values) != len(SOURCE_COLUMNS):
                    raise ProcessingError(
                        f"Record at line {start_line} has {len(values)} fields, "
                        f"expected {len(SOURCE_COLUMNS)}"
                    )
                records.append(SourceRecord(start_line, tuple(values)))
                start_line = reader.line_num + 1
    except UnicodeDecodeError as error:
        raise ProcessingError(f"Input is not valid UTF-8: {error}") from error
    except csv.Error as error:
        raise ProcessingError(f"Input is not valid CSV: {error}") from error
    return records, has_bom


# --- row-level validation (contract, section 4) -----------------------------------------


def _measurement_problems(record: SourceRecord) -> tuple[list[Problem], str | None, str | None]:
    distance_km = record.value("distance_km")
    distance_hr = record.value("distance_hr")
    if not distance_km and not distance_hr:
        if record.value("missing_reason"):
            return [], None, None
        return [Problem("missing_without_reason", "missing_reason=")], None, None
    if not distance_km or not distance_hr:
        empty = "distance_km" if not distance_km else "distance_hr"
        return [Problem("partial_measurement", f"{empty}=")], None, None

    km, hours = parse_positive(distance_km), parse_positive(distance_hr)
    problems = [
        Problem("invalid_number", f"{column}={text}")
        for column, text, value in (
            ("distance_km", distance_km, km),
            ("distance_hr", distance_hr, hours),
        )
        if value is None
    ]
    if km is None or hours is None:
        return problems, None, None
    return [], format_decimal(scale_exactly(km, 1000)), format_decimal(scale_exactly(hours, 3600))


def validate_record(record: SourceRecord) -> Measurement | Problem:
    """Return the measurement or the first problem, with all problems in its detail.

    Checks are listed in contract priority order, so the first one found is the reason.
    """
    problems: list[Problem] = []
    sber_from = record.value("sber_territory_id_from")
    sber_to = record.value("sber_territory_id_to")
    for column, value in (("sber_territory_id_from", sber_from), ("sber_territory_id_to", sber_to)):
        if not value:
            problems.append(Problem("city_identity_missing", f"{column}="))

    transport = record.value("transport_type")
    if transport not in TRANSPORT_TYPES:
        problems.append(Problem("unknown_transport_type", f"transport_type={transport}"))
    if sber_from and sber_from == sber_to:
        problems.append(Problem("self_pair", f"sber_territory_id_to={sber_to}"))

    numeric_problems, distance_m, duration_sec = _measurement_problems(record)
    problems.extend(numeric_problems)
    if problems:
        return Problem(problems[0].reason, "; ".join(problem.detail for problem in problems))

    return Measurement(
        source_row_number=record.row_number,
        from_city_id=city_id(sber_from),
        to_city_id=city_id(sber_to),
        transport_type=transport,
        distance_m=distance_m,
        duration_sec=duration_sec,
        missing_reason_raw=record.value("missing_reason") or None,
        source_raw=record.value("source") or None,
        comment_raw=record.value("comment") or None,
    )


# --- repeated keys (contract, section 5) -------------------------------------------------


def resolve_repeats(
    measurements: Sequence[Measurement],
) -> tuple[list[Measurement], dict[int, Problem], list[dict[str, Any]]]:
    """Keep one row per key; return kept rows, rejections by row and source differences."""
    groups: dict[tuple[str, str, str], list[Measurement]] = defaultdict(list)
    for measurement in sorted(measurements, key=lambda item: item.source_row_number):
        groups[measurement.key].append(measurement)

    kept: list[Measurement] = []
    rejected: dict[int, Problem] = {}
    source_differences: list[dict[str, Any]] = []
    for group in groups.values():
        first, *repeats = group
        if len({item.values for item in group}) > 1:
            rows = ",".join(str(item.source_row_number) for item in group)
            for item in group:
                rejected[item.source_row_number] = Problem(
                    "key_collision", f"source_row_numbers={rows}"
                )
            continue

        kept.append(first)
        for item in repeats:
            rejected[item.source_row_number] = Problem(
                "exact_duplicate", f"kept_source_row_number={first.source_row_number}"
            )
        if len({(item.source_raw, item.comment_raw) for item in group}) > 1:
            source_differences.append(
                {
                    "from_city_id": first.from_city_id,
                    "to_city_id": first.to_city_id,
                    "transport_type": first.transport_type,
                    "rows": [
                        {
                            "source_row_number": item.source_row_number,
                            "source_raw": item.source_raw,
                            "comment_raw": item.comment_raw,
                        }
                        for item in group
                    ],
                }
            )
    return kept, rejected, source_differences


# --- cities (contract, section 2) ---------------------------------------------------------


def _collect_city_evidence(
    records: Iterable[SourceRecord],
) -> tuple[dict[str, _CityEvidence], Counter[tuple[str, str, str]], dict[str, int]]:
    evidence: dict[str, _CityEvidence] = defaultdict(_CityEvidence)
    invalid: Counter[tuple[str, str, str]] = Counter()
    endpoints = {"total": 0, "sber_id_missing": 0, "oktmo_missing": 0, "identifiers_trimmed": 0}

    for record in records:
        for end in ("from", "to"):
            raw_sber = record.raw(f"sber_territory_id_{end}")
            raw_oktmo = record.raw(f"oktmo_{end}")
            sber, oktmo = raw_sber.strip(), raw_oktmo.strip()
            endpoints["total"] += 1
            endpoints["sber_id_missing"] += not sber
            endpoints["oktmo_missing"] += not oktmo
            endpoints["identifiers_trimmed"] += (raw_sber != sber) + (raw_oktmo != oktmo)
            if not sber:
                continue

            city = evidence[sber]
            if oktmo:
                city.oktmo.add(oktmo)
            if name := record.value(f"city_{end}"):
                city.name[name] += 1
            if region := record.value(f"region_{end}"):
                city.region[region] += 1

            population = record.value(f"population_{end}")
            if _UNSIGNED_INTEGER.fullmatch(population):
                city.population[int(population)] += 1
            elif population:
                invalid[(sber, "population", population)] += 1

            lat, lon = record.value(f"lat_{end}"), record.value(f"lon_{end}")
            if coordinates := _parse_coordinates(lat, lon):
                city.coordinates[coordinates] += 1
            else:
                # Missing coordinates are reported too (contract, section 2).
                invalid[(sber, "coordinates", f"{lat},{lon}")] += 1
    return evidence, invalid, endpoints


def _oktmo_conflicts(evidence: dict[str, _CityEvidence]) -> tuple[set[str], dict[str, Any]]:
    """Sber IDs whose OKTMO is ambiguous in either direction of the mapping."""
    sber_by_oktmo: dict[str, set[str]] = defaultdict(set)
    for sber, city in evidence.items():
        for oktmo in city.oktmo:
            sber_by_oktmo[oktmo].add(sber)

    several_oktmo = {sber: city.oktmo for sber, city in evidence.items() if len(city.oktmo) > 1}
    several_sber = {oktmo: ids for oktmo, ids in sber_by_oktmo.items() if len(ids) > 1}
    affected = set(several_oktmo).union(*several_sber.values())
    report = {
        "sber_id_with_several_oktmo": [
            {"source_city_id": sber, "oktmo": sorted(values)}
            for sber, values in sorted(several_oktmo.items())
        ],
        "oktmo_with_several_sber_id": [
            {"oktmo": oktmo, "source_city_ids": sorted(ids)}
            for oktmo, ids in sorted(several_sber.items())
        ],
    }
    return affected, report


def _vote_text(value: object) -> str:
    if isinstance(value, tuple):
        return ",".join(format_decimal(part) for part in value)
    return str(value)


def resolve_cities(records: Sequence[SourceRecord]) -> tuple[list[Row], dict[str, Any]]:
    """Build one city per Sber ID by majority over all occurrences; report the evidence."""
    evidence, invalid, endpoints = _collect_city_evidence(records)
    oktmo_ambiguous, oktmo_report = _oktmo_conflicts(evidence)

    rows: list[Row] = []
    attribute_conflicts: list[dict[str, Any]] = []
    for sber in sorted(evidence):
        city = evidence[sber]
        chosen: dict[str, Any] = {}
        for attribute in ("name", "region", "population", "coordinates"):
            votes: Counter[Any] = getattr(city, attribute)
            winner = chosen[attribute] = _majority(votes)
            if len(votes) > 1:
                attribute_conflicts.append(
                    {
                        "source_city_id": sber,
                        "attribute": attribute,
                        "values": {_vote_text(value): n for value, n in sorted(votes.items())},
                        "chosen": None if winner is None else _vote_text(winner),
                    }
                )
        unambiguous = sber not in oktmo_ambiguous and len(city.oktmo) == 1
        coordinates = chosen["coordinates"] or (None, None)
        rows.append(
            (
                city_id(sber),
                ID_NAMESPACE,
                sber,
                next(iter(city.oktmo)) if unambiguous else None,
                chosen["name"],
                chosen["region"],
                *(None if part is None else format_decimal(part) for part in coordinates),
                chosen["population"],
            )
        )

    oktmo_column = CITY_COLUMNS.index("oktmo")
    identity = {
        "endpoints": endpoints,
        "cities": {
            "total": len(rows),
            "oktmo_missing": sum(row[oktmo_column] is None for row in rows),
        },
        "cities_by_namespace": {ID_NAMESPACE: len(rows)},
        "oktmo_conflicts": oktmo_report,
        "attribute_conflicts": attribute_conflicts,
        "invalid_attributes": [
            {"source_city_id": sber, "attribute": attribute, "value": value, "count": count}
            for (sber, attribute, value), count in sorted(invalid.items())
        ],
    }
    return rows, identity


# --- whole snapshot ----------------------------------------------------------------------


def _rejected_row(record: SourceRecord, problem: Problem) -> Row:
    raw_json = json.dumps(
        dict(zip(SOURCE_COLUMNS, record.fields, strict=True)),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return record.row_number, problem.reason, problem.detail, raw_json


def _sorted_counts(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def normalize(records: Sequence[SourceRecord], snapshot_id: str) -> NormalizationResult:
    """Apply the contract to parsed records; every record ends up in exactly one output."""
    records_by_row = {record.row_number: record for record in records}
    valid: list[Measurement] = []
    rejected: dict[int, Problem] = {}
    for record in records:
        outcome = validate_record(record)
        if isinstance(outcome, Measurement):
            valid.append(outcome)
        else:
            rejected[record.row_number] = outcome

    kept, repeat_rejections, source_differences = resolve_repeats(valid)
    rejected.update(repeat_rejections)
    kept.sort(key=lambda item: item.source_row_number)
    city_rows, identity = resolve_cities(records)

    rejection_counts = Counter(problem.reason for problem in rejected.values())
    report = {
        "rejections": {reason: rejection_counts[reason] for reason in REJECT_REASONS},
        "missing_reason_values": _sorted_counts(
            record.value("missing_reason") for record in records
        ),
        "measurements": {
            "with_values": sum(item.distance_m is not None for item in kept),
            "without_values": sum(item.distance_m is None for item in kept),
            "by_transport_type": _sorted_counts(item.transport_type for item in kept),
        },
        "duplicates": {
            "exact_duplicate": rejection_counts["exact_duplicate"],
            "key_collision": rejection_counts["key_collision"],
            "source_differences": source_differences,
        },
        "identity": identity,
    }
    return NormalizationResult(
        city_rows=city_rows,
        measurement_rows=[item.to_row(snapshot_id) for item in kept],
        rejected_rows=[
            _rejected_row(records_by_row[row], problem) for row, problem in sorted(rejected.items())
        ],
        report=report,
    )


def to_csv_bytes(columns: Sequence[str], rows: Iterable[Row]) -> bytes:
    """Contract output format: comma, ``\\n``, minimal quoting, NULL as an empty field."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def publish_directory(target: Path, files: dict[str, bytes]) -> None:
    """Write files to a staging directory and swap it with ``target`` in one step."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"))
    try:
        for name, data in files.items():
            (staging / name).write_bytes(data)
        previous = target.with_name(f".{target.name}.previous")
        if previous.exists():
            # Left by a run killed mid-swap: it is the last good copy if target is gone.
            if target.exists():
                shutil.rmtree(previous)
            else:
                previous.rename(target)
        if target.exists():
            target.rename(previous)
        try:
            staging.rename(target)
        except OSError:
            if previous.exists():
                previous.rename(target)
            raise
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(previous, ignore_errors=True)


def _display_path(path: Path) -> str:
    """Path relative to the working directory; never an absolute developer path."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.name


def process_snapshot(
    raw_dir: Path = DEFAULT_RAW_DIR,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    *,
    code_version: str = "unknown",
) -> dict[str, Any]:
    """Normalize the snapshot in ``raw_dir`` into ``output_root/<snapshot_id>/``.

    Raises ProcessingError (status ``failed``) before publishing anything when the input
    does not match its manifest or the row balance does not hold.
    """
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    tracemalloc.start()
    try:
        try:
            manifest = load_manifest(raw_dir / MANIFEST_FILENAME)
        except ConnectorError as error:
            raise ProcessingError(str(error)) from error
        csv_path = raw_dir / manifest.csv_filename
        if not csv_path.is_file():
            raise ProcessingError(f"Input CSV not found: {_display_path(csv_path)}")
        input_sha256 = sha256_file(csv_path)
        if input_sha256 != manifest.csv_sha256:
            raise ProcessingError(
                f"Input SHA-256 {input_sha256} does not match manifest {manifest.csv_sha256}"
            )
        records, has_bom = read_records(csv_path)
        if len(records) != manifest.row_count:
            raise ProcessingError(
                f"Input has {len(records)} records, manifest says {manifest.row_count}"
            )

        if not _SNAPSHOT_ID.fullmatch(manifest.snapshot_id):
            raise ProcessingError(f"Invalid snapshot_id in manifest: {manifest.snapshot_id!r}")
        result = normalize(records, manifest.snapshot_id)
        rows_out = len(result.measurement_rows) + len(result.rejected_rows)
        if rows_out != len(records):
            raise ProcessingError(f"Balance broken: {len(records)} read, {rows_out} written")

        outputs = {
            CITIES_FILE: to_csv_bytes(CITY_COLUMNS, result.city_rows),
            MEASUREMENTS_FILE: to_csv_bytes(MEASUREMENT_COLUMNS, result.measurement_rows),
            REJECTED_FILE: to_csv_bytes(REJECTED_COLUMNS, result.rejected_rows),
        }
        _, peak_memory = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    output_dir = output_root / manifest.snapshot_id
    row_counts = {
        CITIES_FILE: len(result.city_rows),
        MEASUREMENTS_FILE: len(result.measurement_rows),
        REJECTED_FILE: len(result.rejected_rows),
    }
    report = {
        "contract_version": CONTRACT_VERSION,
        "snapshot_id": manifest.snapshot_id,
        "status": "success_with_rejections" if result.rejected_rows else "success",
        "input": {
            "path": _display_path(csv_path),
            "sha256": input_sha256,
            "size_bytes": csv_path.stat().st_size,
            "records": len(records),
            "bom": has_bom,
            "header_matches_contract": True,
            "manifest_matches": True,
        },
        "output": {
            name: {"rows": row_counts[name], "sha256": sha256_bytes(data)}
            for name, data in outputs.items()
        },
        "balance": {
            "rows_read": len(records),
            "route_measurements": len(result.measurement_rows),
            "rejected_rows": len(result.rejected_rows),
        },
        **result.report,
        "service": {
            "started_at_utc": started_at.isoformat(timespec="seconds"),
            "finished_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "duration_sec": round(time.perf_counter() - started, 3),
            "peak_memory_bytes": peak_memory,
            "peak_memory_method": "tracemalloc: Python allocations from manifest to CSV bytes",
            "code_version": code_version,
            "parameters": {
                "raw_dir": _display_path(raw_dir),
                "output_dir": _display_path(output_dir),
            },
        },
    }
    report_bytes = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    publish_directory(output_dir, {**outputs, REPORT_FILE: report_bytes})
    return report
