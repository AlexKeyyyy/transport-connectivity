"""Проверки поиска на полном снимке. Запуск: pytest -m es."""

import csv
import os
from pathlib import Path
from uuid import uuid4

import pytest
from elasticsearch import Elasticsearch

from transport_connectivity.search import (
    cities_near,
    city_document,
    rebuild_city_index,
    search_cities,
)

pytestmark = pytest.mark.es

DEFAULT_CITIES_CSV = "data/normalized/tochno_intercity_v20260629/cities.csv"


@pytest.fixture(scope="module")
def loaded_index():
    url = os.environ.get("ELASTICSEARCH_URL")
    if not url:
        pytest.skip("ELASTICSEARCH_URL не задан")

    path = Path(os.environ.get("ELASTICSEARCH_CITIES_CSV", DEFAULT_CITIES_CSV))
    if not path.is_file():
        pytest.fail(f"Файл {path} не найден. Сначала выполните download и process.")

    with path.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    documents = [city_document(row) for row in rows]
    source_ids = {row["city_id"]: row["source_city_id"] for row in rows}
    assert len(documents) == len(source_ids) == 180

    alias = f"cities-test-{uuid4().hex}"
    index = f"{alias}-v1"
    with Elasticsearch(url) as client:
        try:
            for _ in range(2):
                assert rebuild_city_index(client, documents, index=index, alias=alias) == 180
                assert client.count(index=alias)["count"] == 180
            assert (
                client.count(index=alias, query={"exists": {"field": "location"}})["count"] == 180
            )
            yield client, alias, source_ids
        finally:
            client.indices.delete(index=index, ignore_unavailable=True)


def test_search_exact(loaded_index):
    client, alias, source_ids = loaded_index
    results = search_cities(client, "Брянск", alias=alias)
    assert source_ids[results[0]["city_id"]] == "953"


def test_search_prefix(loaded_index):
    client, alias, source_ids = loaded_index
    results = search_cities(client, "Нижн", alias=alias)
    assert {source_ids[result["city_id"]] for result in results} == {"1523", "2000", "2221", "385"}


def test_search_fuzzy(loaded_index):
    client, alias, source_ids = loaded_index
    results = search_cities(client, "Брянкс", alias=alias)
    assert source_ids[results[0]["city_id"]] == "953"


def test_search_yo_to_e(loaded_index):
    client, alias, source_ids = loaded_index
    results = search_cities(client, "Орел", alias=alias)
    assert source_ids[results[0]["city_id"]] == "1707"


def test_search_region_filter(loaded_index):
    client, alias, source_ids = loaded_index
    region = "Московская область"
    results = search_cities(client, "Королев", region=region, alias=alias)
    assert source_ids[results[0]["city_id"]] == "1456"
    assert all(result["region"] == region for result in results)


def test_cities_near(loaded_index):
    client, alias, source_ids = loaded_index
    results = cities_near(client, lat=55.7540584, lon=37.62049, radius_km=25, alias=alias)
    assert len(results) == 11
    assert [source_ids[result["city_id"]] for result in results[:2]] == ["2539", "1475"]
    distances = [result["distance_km"] for result in results]
    assert distances == sorted(distances)
    assert distances[-1] <= 25
