"""Тесты city_document и INDEX_SETTINGS без Elasticsearch."""

from transport_connectivity.search import INDEX_SETTINGS, city_document


def test_city_document_with_location_and_population():
    row = {
        "city_id": "953",
        "name": "Брянск",
        "region": "Брянская область",
        "population": "400000",
        "lat": "53.2420053",
        "lon": "34.3653002",
    }
    doc = city_document(row)
    assert doc["city_id"] == "953"
    assert doc["name"] == "Брянск"
    assert doc["aliases"] == []
    assert doc["region"] == "Брянская область"
    assert doc["population"] == 400000
    assert doc["location"] == {"lat": 53.2420053, "lon": 34.3653002}


def test_city_document_without_coordinates():
    row = {
        "city_id": "1707",
        "name": "Орёл",
        "region": "Орловская область",
        "population": "300000",
        "lat": "",
        "lon": "",
    }
    doc = city_document(row)
    assert "location" not in doc


def test_city_document_without_population():
    row = {
        "city_id": "385",
        "name": "Нижнекамск",
        "region": "Республика Татарстан",
        "population": None,
        "lat": "55.633333",
        "lon": "51.816667",
    }
    doc = city_document(row)
    assert "population" not in doc


def test_index_settings_match_mapping():
    """Проверяем, что mapping содержит все нужные поля и типы."""
    props = INDEX_SETTINGS["mappings"]["properties"]
    assert props["city_id"]["type"] == "keyword"
    assert props["name"]["type"] == "text"
    assert props["name"]["fields"]["keyword"]["type"] == "keyword"
    assert props["name"]["fields"]["prefix"]["type"] == "text"
    assert props["aliases"]["type"] == "text"
    assert props["region"]["type"] == "keyword"
    assert props["population"]["type"] == "integer"
    assert props["location"]["type"] == "geo_point"
    assert INDEX_SETTINGS["mappings"]["dynamic"] == "strict"
