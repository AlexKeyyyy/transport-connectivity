"""Интеграционные тесты поиска с Elasticsearch. Запуск: pytest -m es."""

import csv
import os

import pytest
from elasticsearch import Elasticsearch

from transport_connectivity.search import (
    cities_near,
    city_document,
    create_index,
    index_cities,
    search_cities,
)

pytestmark = pytest.mark.es

ES_URL = os.environ.get("ELASTICSEARCH_URL")
INDEX = "cities_test"
CSV_PATH = "data/normalized/tochno_intercity_v20260629/cities.csv"


@pytest.fixture(scope="module")
def client():
    if not ES_URL:
        pytest.skip("ELASTICSEARCH_URL не задан")
    client = Elasticsearch(ES_URL)
    yield client
    client.indices.delete(index=INDEX, ignore_unavailable=True)


@pytest.fixture(scope="module")
def loaded_index(client):
    """Загружает полный cities.csv (180 городов) в тестовый индекс."""
    if not os.path.exists(CSV_PATH):
        pytest.skip(f"Файл {CSV_PATH} не найден. Сначала выполните download и process.")

    with open(CSV_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        docs = [city_document(row) for row in reader]

    create_index(client, INDEX)
    index_cities(client, INDEX, docs)
    yield


def test_search_exact(loaded_index, client):
    results = search_cities(client, "Брянск", limit=10)
    assert results[0]["city_id"] == "953"


def test_search_prefix(loaded_index, client):
    results = search_cities(client, "Нижн", limit=10)
    ids = [r["city_id"] for r in results]
    assert ids[:4] == ["1523", "2000", "2221", "385"]


def test_search_fuzzy(loaded_index, client):
    results = search_cities(client, "Брянкс", limit=10)
    assert results[0]["city_id"] == "953"


def test_search_yo_to_e(loaded_index, client):
    results = search_cities(client, "Орел", limit=10)
    assert results[0]["city_id"] == "1707"


def test_search_region_filter(loaded_index, client):
    results = search_cities(
        client, "Королев", region="Московская область", limit=10
    )
    assert results[0]["city_id"] == "1456"


def test_cities_near(loaded_index, client):
    results = cities_near(client, lat=55.7540584, lon=37.62049, radius_km=25, limit=20)
    assert len(results) == 11
    assert results[0]["city_id"] == "2539"  # Москва
    assert results[1]["city_id"] == "1475"  # Реутов