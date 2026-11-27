# Поиск городов: индекс Elasticsearch

Индекс проверен на Elasticsearch 8.19.0 и нормализованном снимке
`tochno_intercity_v20260629`. После первого и повторного запуска в `cities_v1`
было 180 документов с координатами. Все шесть проверок ниже выполнены
через псевдоним `cities`.

## Роль в системе

Elasticsearch хранит производный индекс поиска ([модель данных](data-model.md#neo4j-и-elasticsearch)).
В этой реализации индекс целиком собирается из нормализованного `cities.csv`.
Эти же данные загружаются в PostgreSQL. Поиск возвращает `city_id` проекта
(UUID), по которому приложение связывает результаты с городами в базе.

## Запуск и проверка

Команды из корня репозитория после установки пакета с зависимостями для разработки:

```bash
python -m transport_connectivity.cli download
python -m transport_connectivity.cli process
docker compose up -d --wait elasticsearch
python -m transport_connectivity.cli index-cities
```

При необходимости передайте `index-cities --cities-csv …`. Сервер и Python-клиент
закреплены на версии 8.19.0. Compose открывает порт только на `127.0.0.1`.
Пароль и HTTPS отключены для локальной разработки и тестов CI.
Для другого порта задайте `ELASTICSEARCH_PORT` для Compose и тот же порт в
`ELASTICSEARCH_URL` для CLI, приложения и тестов. Значение CLI по умолчанию —
`http://localhost:9200`.

Команда проверяет UUID и диапазоны координат до замены индекса, загружает документы с `_id = city_id`, сверяет их число
и публикует псевдоним `cities` на `cities_v1`. При пересборке поиск временно
недоступен, пока фиксированный индекс заменяется. При ошибке загрузки
псевдоним не публикуется и команда возвращает ненулевой код.
После устранения сбоя запустите индексацию повторно.

Для интеграционных тестов задайте `ELASTICSEARCH_URL=http://localhost:9200`
в окружении и выполните `python -m pytest -m es -v`. Тесты проверяют две загрузки
снимка в отдельный индекс, 180 документов с координатами и шесть запросов.
Параметр окружения `ELASTICSEARCH_CITIES_CSV` позволяет указать другой путь
к тому же полному снимку.

## Mapping

Поля — из модели данных. `dynamic: strict` запрещает поля, которых нет в mapping.

```json
PUT cities_v1
{
  "settings": {
    "analysis": {
      "char_filter": {
        "yo_to_e": { "type": "mapping", "mappings": ["ё => е", "Ё => Е"] }
      },
      "tokenizer": {
        "prefix_tokenizer": {
          "type": "edge_ngram", "min_gram": 2, "max_gram": 20,
          "token_chars": ["letter", "digit"]
        }
      },
      "analyzer": {
        "city_name": {
          "type": "custom", "tokenizer": "standard",
          "char_filter": ["yo_to_e"], "filter": ["lowercase"]
        },
        "city_prefix": {
          "type": "custom", "tokenizer": "prefix_tokenizer",
          "char_filter": ["yo_to_e"], "filter": ["lowercase"]
        }
      }
    }
  },
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "city_id":    { "type": "keyword" },
      "name": {
        "type": "text", "analyzer": "city_name",
        "fields": {
          "keyword": { "type": "keyword" },
          "prefix":  { "type": "text", "analyzer": "city_prefix", "search_analyzer": "city_name" }
        }
      },
      "aliases":    { "type": "text", "analyzer": "city_name" },
      "region":     { "type": "keyword" },
      "population": { "type": "integer" },
      "location":   { "type": "geo_point" }
    }
  }
}
```

Почему так:

- **`city_id` — `keyword` и он же `_id` документа.** Повторная загрузка перезаписывает документ, а не создаёт дубль.
- **`ё → е` при индексации и поиске.** В наборе есть Орёл, Королёв, Щёлково; человек обычно пишет «Орел».
- **`name.prefix` — частичный ввод.** `edge_ngram` индексирует начала слов («ни», «ниж», «нижн»…), а запрос анализируется обычным анализатором, чтобы «Нижн» не разбивался на n-граммы.
- **`name.keyword` — точное совпадение** и сортировка.
- **`region` — `keyword`:** регион используется как точный фильтр, а не для полнотекстового поиска.
- **`location` — `geo_point` объектом `{lat, lon}`:** так нельзя перепутать порядок, как в массиве `[lon, lat]`.
- **`aliases`** — другие написания (OSM, Яндекс). Пока пусто: сопоставление OSM и Яндекса с городами ещё не сделано.

Документ для Брянска:

```json
PUT cities_v1/_doc/83c80e94-954a-5182-b2dd-a4aa219e6400
{
  "city_id": "83c80e94-954a-5182-b2dd-a4aa219e6400",
  "name": "Брянск",
  "aliases": [],
  "region": "Брянская область",
  "population": 379152,
  "location": { "lat": 53.2420053, "lon": 34.3653002 }
}
```

Загрузка — пакетом через Bulk API с проверкой ошибки **каждого** элемента ответа: код 200 всего запроса не значит, что проиндексированы все документы.

## Результаты проверок

| № | Сценарий | Запрос | Результат (Sber ID) |
|---|---|---|---|
| 1 | Точное название | `term` по `name.keyword`: «Брянск» | Первый — Брянск (953) |
| 2 | Начало названия | `match` по `name.prefix`: «Нижн» | Нижний Тагил (2000), Нижнекамск (385), Нижний Новгород (1523), Нижневартовск (2221) |
| 3 | Опечатка | `match` по `name`, `fuzziness: AUTO`: «Брянкс» | Брянск (953) |
| 4 | «е» вместо «ё» | `match` по `name`: «Орел» | Орёл (1707) |
| 5 | Город в регионе | `match` по `name` + `filter` `term` по `region`: «Королев», «Московская область» | Королёв (1456) |
| 6 | Рядом с точкой | `geo_distance` 25 км от Москвы (55.7540584, 37.62049), сортировка по расстоянию | 11 городов: Москва (0 км), Реутов (15,11), Мытищи (18,84), Химки (19,12), Люберцы (19,41), Красногорск (20,15), Балашиха (20,43), Долгопрудный (21,65), Видное (23,13), Королёв (23,20), Одинцово (23,86) |

Расстояния округлены до сотых километра по ответу Elasticsearch. В результатах
поиска приходят UUID. Для сверки с таблицей тесты сопоставляют их с
`source_city_id` из того же CSV. Для «Нижн» проверяется состав четырёх ответов.

Запрос 3 — опечатка «Брянкс» отличается от «Брянск» перестановкой соседних букв: при `fuzzy_transpositions` (включено по умолчанию) это одна правка, а слово из 6 букв допускает две ([fuzziness](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/common-options)).

```json
GET cities/_search
{ "query": { "match": { "name": { "query": "Брянкс", "fuzziness": "AUTO" } } } }
```

```json
GET cities/_search
{
  "query": {
    "bool": {
      "must":   { "match": { "name": "Королев" } },
      "filter": { "term":  { "region": "Московская область" } }
    }
  }
}
```

```json
GET cities/_search
{
  "size": 20,
  "query": {
    "bool": {
      "filter": {
        "geo_distance": { "distance": "25km", "location": { "lat": 55.7540584, "lon": 37.62049 } }
      }
    }
  },
  "sort": [
    { "_geo_distance": { "location": { "lat": 55.7540584, "lon": 37.62049 }, "order": "asc", "unit": "km" } }
  ]
}
```
