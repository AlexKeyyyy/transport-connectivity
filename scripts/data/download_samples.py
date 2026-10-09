"""Download a small, reproducible pilot sample from open transport data sources."""

from __future__ import annotations

import csv
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from time import sleep
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
SAMPLES_DIR = ROOT / "data" / "samples"
DELIVERABLE_DIR = ROOT / "deliverables" / "m0" / "data-preview"
DATASET_URL = (
    "https://storage.yandexcloud.net/tochno-st-catalog/Geo/"
    "data_intercity_connectivity_162_v20260629/"
    "data_intercity_connectivity_162_v20260629_csv.zip"
)
DATASET_PAGE = "https://tochno.st/datasets/intercity_connectivity"
OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
OSRM_URL = os.environ.get("OSRM_URL", "https://router.project-osrm.org").rstrip("/")
USER_AGENT = (
    "TransportConnectivityM0Sample/1.0 "
    "(educational project; GitHub: AlexKeyyyy/transport-connectivity)"
)
OVERPASS_FALLBACKS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# 10 cities suggested by issue #3; source names are matched exactly in the dataset.
PILOT_CITIES = [
    "Москва",
    "Тверь",
    "Ярославль",
    "Владимир",
    "Рязань",
    "Тула",
    "Калуга",
    "Смоленск",
    "Брянск",
    "Орёл",
]


def request_bytes(url: str, data: bytes | None = None) -> bytes:
    urls = [url]
    if url == OVERPASS_URL:
        urls.extend(endpoint for endpoint in OVERPASS_FALLBACKS if endpoint != url)
    last_error: Exception | None = None
    for candidate in urls:
        for attempt in range(2):
            request = urllib.request.Request(
                candidate,
                data=data,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/csv, application/zip, */*",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return response.read()
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
                if attempt == 0:
                    sleep(2)
    raise RuntimeError(f"Request failed for {url}: {last_error}") from last_error


def fetch_dataset() -> tuple[list[dict[str, str]], dict[str, Any]]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = RAW_DIR / "intercity_connectivity_source.zip"
    if not archive_path.exists():
        archive_path.write_bytes(request_bytes(DATASET_URL))

    with zipfile.ZipFile(archive_path) as archive:
        csv_member = next(
            name
            for name in archive.namelist()
            if name.endswith(".csv") and not name.startswith("__")
        )
        text = archive.read(csv_member).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    if not reader.fieldnames:
        raise ValueError("Downloaded connectivity dataset has no CSV header")

    cities = set(PILOT_CITIES)
    sample = [
        row
        for row in reader
        if row["city_from"] in cities
        and row["city_to"] in cities
        and row["city_from"] != row["city_to"]
    ]
    if not sample:
        raise ValueError("None of the pilot city pairs was found in the source dataset")
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    write_csv(
        SAMPLES_DIR / "intercity_connectivity_sample.csv", reader.fieldnames, sample, delimiter=";"
    )
    metadata = {
        "source_url": DATASET_URL,
        "page_url": DATASET_PAGE,
        "archive": archive_path.name,
        "member": csv_member,
        "source_rows": sum(1 for _ in csv.DictReader(io.StringIO(text), delimiter=";")),
        "sample_rows": len(sample),
        "columns": reader.fieldnames,
    }
    (RAW_DIR / "dataset_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return sample, metadata


def write_csv(
    path: Path, columns: list[str], rows: list[dict[str, Any]], delimiter: str = ","
) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=columns, delimiter=delimiter, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)


def source_city_attributes(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for row in rows:
        for side in ("from", "to"):
            name = row[f"city_{side}"]
            if name in PILOT_CITIES:
                found.setdefault(
                    name,
                    {
                        "region": row[f"region_{side}"],
                        "population": row[f"population_{side}"],
                    },
                )
    return found


def fetch_osm(sample_rows: list[dict[str, str]]) -> dict[str, Any]:
    names = "".join(f'nwr["name"="{name}"]["place"~"^(city|town)$"];' for name in PILOT_CITIES)
    query = f"[out:json][timeout:60];({names});out center tags;"
    payload = urllib.parse.urlencode({"data": query}).encode()
    response = json.loads(request_bytes(OVERPASS_URL, payload))
    attributes = source_city_attributes(sample_rows)
    candidates: dict[str, list[dict[str, Any]]] = {name: [] for name in PILOT_CITIES}
    for element in response.get("elements", []):
        tags = element.get("tags", {})
        name = tags.get("name")
        if name not in candidates:
            continue
        if element["type"] == "node":
            latitude, longitude = element["lat"], element["lon"]
        else:
            center = element.get("center", {})
            latitude, longitude = center.get("lat"), center.get("lon")
        if latitude is None or longitude is None:
            continue
        candidates[name].append(
            {"element": element, "latitude": latitude, "longitude": longitude, "tags": tags}
        )

    city_records: list[dict[str, Any]] = []
    for name in PILOT_CITIES:
        options = candidates[name]
        if not options:
            continue
        # Prefer the point geometry when multiple OSM features share a place name.
        match = sorted(
            options, key=lambda item: (item["element"]["type"] != "node", item["element"]["id"])
        )[0]
        element, tags = match["element"], match["tags"]
        source_attrs = attributes.get(name, {})
        city_records.append(
            {
                "city_id": f"osm-{element['type']}-{element['id']}",
                "osm_id": f"{element['type']}/{element['id']}",
                "name": name,
                "region": source_attrs.get("region", ""),
                "place_type": tags.get("place", ""),
                "latitude": match["latitude"],
                "longitude": match["longitude"],
                "population": source_attrs.get("population", ""),
                "source": "OpenStreetMap; region/population from connectivity dataset",
            }
        )
    if not city_records:
        raise ValueError("Overpass returned no named pilot cities")
    write_csv(
        SAMPLES_DIR / "cities.csv",
        [
            "city_id",
            "osm_id",
            "name",
            "region",
            "place_type",
            "latitude",
            "longitude",
            "population",
            "source",
        ],
        city_records,
    )

    # A deliberately small Moscow urban road sample; the bounding box is fixed and documented.
    road_query = "[out:json][timeout:60];way[highway](55.74,37.58,55.78,37.66);out 150 geom;"
    road_payload = urllib.parse.urlencode({"data": road_query}).encode()
    road_response = json.loads(request_bytes(OVERPASS_URL, road_payload))
    roads = []
    for way in sorted(road_response.get("elements", []), key=lambda item: item["id"])[:150]:
        points = way.get("geometry", [])
        if len(points) < 2:
            continue
        tags = way.get("tags", {})
        roads.append(
            {
                "type": "Feature",
                "id": f"way/{way['id']}",
                "properties": {
                    "osm_way_id": way["id"],
                    "highway_type": tags.get("highway"),
                    "name": tags.get("name"),
                    "oneway": tags.get("oneway"),
                    "maxspeed": tags.get("maxspeed"),
                    "surface": tags.get("surface"),
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[point["lon"], point["lat"]] for point in points],
                },
            }
        )
    geojson = {
        "type": "FeatureCollection",
        "name": "Moscow OSM road segments sample",
        "osm_base_timestamp": response.get("osm3s", {}).get("timestamp_osm_base"),
        "features": roads,
    }
    (SAMPLES_DIR / "road_segments_sample.geojson").write_text(
        json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "osm_base_timestamp": response.get("osm3s", {}).get("timestamp_osm_base"),
        "cities_found": len(city_records),
        "cities_missing": [
            name for name in PILOT_CITIES if name not in {c["name"] for c in city_records}
        ],
        "road_features": len(roads),
        "roads_bbox": "55.74,37.58,55.78,37.66",
    }


def fetch_routes() -> dict[str, Any]:
    city_path = SAMPLES_DIR / "cities.csv"
    with city_path.open(encoding="utf-8", newline="") as source:
        cities = list(csv.DictReader(source))
    if len(cities) < 2:
        raise ValueError("At least two OSM city coordinates are required for OSRM")
    coords = ";".join(f"{city['longitude']},{city['latitude']}" for city in cities)
    url = f"{OSRM_URL}/table/v1/driving/{coords}?annotations=duration,distance"
    response = json.loads(request_bytes(url))
    if response.get("code") != "Ok":
        raise ValueError(
            f"OSRM Table returned {response.get('code')}: {response.get('message', '')}"
        )
    calculated_at = datetime.now(UTC).isoformat(timespec="seconds")
    data_version = response.get("data_version")
    records = []
    durations = response["durations"]
    distances = response["distances"]
    for i, source in enumerate(cities):
        for j, destination in enumerate(cities):
            if i == j:
                continue
            records.append(
                {
                    "from_city_id": source["city_id"],
                    "from_city": source["name"],
                    "to_city_id": destination["city_id"],
                    "to_city": destination["name"],
                    "distance_m": distances[i][j],
                    "duration_sec": durations[i][j],
                    "router": OSRM_URL,
                    "calculated_at": calculated_at,
                    "data_version": data_version,
                }
            )
    write_csv(
        SAMPLES_DIR / "route_measurements.csv",
        [
            "from_city_id",
            "from_city",
            "to_city_id",
            "to_city",
            "distance_m",
            "duration_sec",
            "router",
            "calculated_at",
            "data_version",
        ],
        records,
    )
    return {"rows": len(records), "data_version": data_version, "calculated_at": calculated_at}


def create_public_transport_status() -> None:
    key_present = bool(os.environ.get("YANDEX_RASP_API_KEY"))
    result = {
        "source": "Yandex Schedule API",
        "documentation": "https://yandex.ru/dev/rasp/doc/",
        "request_date": datetime.now(UTC).date().isoformat(),
        "status": "key_present_but_not_fetched" if key_present else "not_fetched_no_api_key",
        "records": [],
        "note": (
            "No YANDEX_RASP_API_KEY was available during this extraction; "
            "no schedule data is included."
            if not key_present
            else "API access must be verified before publishing schedule records."
        ),
    }
    (SAMPLES_DIR / "public_transport_sample.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    sample_rows, dataset = fetch_dataset()
    osm = fetch_osm(sample_rows)
    routes = fetch_routes()
    create_public_transport_status()
    metadata = {
        "extracted_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "pilot_cities": PILOT_CITIES,
        "dataset": dataset,
        "osm": osm,
        "osrm": routes,
    }
    (SAMPLES_DIR / "extraction_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
