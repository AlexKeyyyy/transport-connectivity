"""Поиск городов в Elasticsearch."""

from __future__ import annotations

from typing import Any

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk

# Settings и mappings в точности из docs/elasticsearch.md
INDEX_SETTINGS: dict[str, Any] = {
    "settings": {
        "analysis": {
            "char_filter": {
                "yo_to_e": {
                    "type": "mapping",
                    "mappings": ["ё => е", "Ё => Е"],
                }
            },
            "tokenizer": {
                "prefix_tokenizer": {
                    "type": "edge_ngram",
                    "min_gram": 2,
                    "max_gram": 20,
                    "token_chars": ["letter", "digit"],
                }
            },
            "analyzer": {
                "city_name": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "char_filter": ["yo_to_e"],
                    "filter": ["lowercase"],
                },
                "city_prefix": {
                    "type": "custom",
                    "tokenizer": "prefix_tokenizer",
                    "char_filter": ["yo_to_e"],
                    "filter": ["lowercase"],
                },
            },
        }
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "city_id": {"type": "keyword"},
            "name": {
                "type": "text",
                "analyzer": "city_name",
                "fields": {
                    "keyword": {"type": "keyword"},
                    "prefix": {
                        "type": "text",
                        "analyzer": "city_prefix",
                        "search_analyzer": "city_name",
                    },
                },
            },
            "aliases": {"type": "text", "analyzer": "city_name"},
            "region": {"type": "keyword"},
            "population": {"type": "integer"},
            "location": {"type": "geo_point"},
        },
    },
}


def city_document(row: dict[str, Any]) -> dict[str, Any]:
    """Преобразует строку cities.csv в документ Elasticsearch.

    Чистая функция, не зависящая от клиента.
    """
    doc: dict[str, Any] = {
        "city_id": row["city_id"],
        "name": row["name"],
        "aliases": row.get("aliases", []),
        "region": row["region"],
    }

    # population: int или отсутствует
    population = row.get("population")
    if population is not None and str(population).strip() != "":
        doc["population"] = int(population)

    # location: объект {lat, lon} или отсутствует
    lat = row.get("lat")
    lon = row.get("lon")
    if lat is not None and lon is not None and str(lat).strip() != "" and str(lon).strip() != "":
        doc["location"] = {"lat": float(lat), "lon": float(lon)}

    return doc


def create_index(client: Elasticsearch, name: str) -> None:
    """Создаёт индекс с заданным именем и настройками."""
    if client.indices.exists(index=name):
        client.indices.delete(index=name)
    client.indices.create(index=name, body=INDEX_SETTINGS)


def index_cities(client: Elasticsearch, name: str, documents: list[dict[str, Any]]) -> None:
    """Загружает документы через bulk, проверяет ошибки каждого элемента."""
    actions = [
        {
            "_index": name,
            "_id": doc["city_id"],
            "_source": doc,
        }
        for doc in documents
    ]

    success, failed = bulk(client, actions, raise_on_error=False, refresh=True)

    if failed:
        # Собираем понятное сообщение об ошибке
        error_details = []
        for item in failed:
            error_details.append(f"city_id={item['index']['_id']}: {item['index'].get('error')}")
        raise RuntimeError(
            f"Не удалось проиндексировать {len(failed)} документ(ов):\n" + "\n".join(error_details)
        )


def search_cities(
    client: Elasticsearch,
    text: str,
    region: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Поиск городов по тексту (точное, префикс, опечатка) с опциональным фильтром по региону.

    Возвращает список словарей с city_id, name, region, score.
    """
    must: list[dict[str, Any]] = []

    # Основной запрос: комбинируем точное совпадение, префикс и fuzzy
    must.append(
        {
            "bool": {
                "should": [
                    {"term": {"name.keyword": {"value": text, "boost": 10}}},
                    {"match": {"name.prefix": {"query": text, "boost": 5}}},
                    {"match": {"name": {"query": text, "fuzziness": "AUTO"}}},
                ],
                "minimum_should_match": 1,
            }
        }
    )

    query: dict[str, Any] = {"bool": {"must": must}}

    if region is not None:
        query["bool"]["filter"] = [{"term": {"region": region}}]

    body = {
        "size": limit,
        "query": query,
        "_source": ["city_id", "name", "region"],
    }

    response = client.search(index="cities", body=body)
    return [
        {
            "city_id": hit["_source"]["city_id"],
            "name": hit["_source"]["name"],
            "region": hit["_source"]["region"],
            "score": hit["_score"],
        }
        for hit in response["hits"]["hits"]
    ]


def cities_near(
    client: Elasticsearch,
    lat: float,
    lon: float,
    radius_km: float,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Поиск городов в радиусе radius_km от точки, сортировка по расстоянию.

    Возвращает список словарей с city_id, name, region, distance_km.
    """
    body = {
        "size": limit,
        "query": {
            "bool": {
                "filter": {
                    "geo_distance": {
                        "distance": f"{radius_km}km",
                        "location": {"lat": lat, "lon": lon},
                    }
                }
            }
        },
        "sort": [
            {
                "_geo_distance": {
                    "location": {"lat": lat, "lon": lon},
                    "order": "asc",
                    "unit": "km",
                }
            }
        ],
        "_source": ["city_id", "name", "region"],
    }

    response = client.search(index="cities", body=body)
    results = []
    for hit in response["hits"]["hits"]:
        distance = hit["sort"][0] if "sort" in hit else None
        results.append(
            {
                "city_id": hit["_source"]["city_id"],
                "name": hit["_source"]["name"],
                "region": hit["_source"]["region"],
                "distance_km": distance,
            }
        )
    return results
