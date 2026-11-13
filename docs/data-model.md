# Модель данных

Статус: проект схемы PostgreSQL/PostGIS для этапа хранения. Таблицы, миграции и
загрузка реализуются отдельной задачей; здесь зафиксированы сущности, ключи, типы,
ограничения и связи. Правила преобразования полного CSV «Если быть точным» — в
контракте данных ([PR #17](https://github.com/AlexKeyyyy/transport-connectivity/pull/17),
`docs/data-contract.md`, версия `1.0-draft`); модель им следует и не дублирует их.

Схема: [ER-диаграмма](diagrams/data-model.svg) ([исходник](diagrams/data-model.mmd)).
Компоненты и потоки — в [архитектуре](architecture.md).

## Слои

| Слой | Что хранит | Сущности | Откуда появляется |
|---|---|---|---|
| Исходный | Откуда и когда получены данные, проверка входа | DataSource, DataSnapshot, LoadRun, RejectedRow | Загрузчик сохраняет файл и manifest |
| Нормализованный | Города и транспортные наблюдения в единых единицах | City, CityExternalId, RouteMeasurement, RoadSegment, Station, TransportConnection | Обработчик по контракту источника |
| Аналитический | Версия графа и результаты расчётов | GraphSnapshot, GraphSnapshotInput, GraphNode, GraphEdge, AnalysisRun, MetricResult | Построение графа по методике и графовые расчёты |

Исходные файлы лежат вне базы (`data/raw/`); база хранит их метаданные и контрольные суммы.
Neo4j и Elasticsearch строятся из аналитического и нормализованного слоёв и не являются
источником истины ([ADR 0001](adr/0001-postgresql-primary-store.md)).

## Общие соглашения

- Имена таблиц и полей — `snake_case` в единственном числе, как в ER-диаграмме.
- Ключи сущностей, которые воспроизводятся из входа, — `uuid` версии 5 от закреплённого
  пространства имён ([ADR 0002](adr/0002-city-identity.md)). Повторная загрузка того же
  снимка даёт те же ключи и не создаёт дубликатов. Служебные записи запусков — `uuid`,
  выдаваемый при запуске.
- Перечислимые значения — `text` с `CHECK`, а не тип `enum`: список видов транспорта или
  статусов расширяется миграцией без пересоздания типа.
- Время — `timestamptz` в UTC. Расстояние — метры, длительность — секунды, тип `numeric`
  без округления: значения переводятся из км и часов точно, как требует контракт.
- Координаты — `geometry(Point, 4326)` и `geometry(LineString, 4326)`, порядок осей
  (долгота, широта), как в GeoJSON. Расстояния в метрах считаются через приведение к
  `geography` или в обоснованной метрической проекции, а не в градусах.
- Каждый внешний ключ сопровождается индексом; пространственные поля — индексом GiST.
- `NULL` означает «значение неизвестно или отсутствует в источнике». Отсутствие маршрута
  по данным источника — это `NULL` в числах с заполненной причиной, а не `0`.

Обозначения в таблицах ниже: **PK** — первичный ключ, **FK** — внешний ключ,
**UK** — уникальность, «да» в колонке NULL — поле может быть пустым.

## Исходный слой

### data_source — источник данных

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| source_id | text | нет | PK; короткий код: `tochno_intercity`, `osm`, `osrm_public`, `yandex_rasp` |
| name | text | нет | Название для отчёта |
| url | text | да | Страница или документация источника |
| license | text | да | Например, `CC BY 4.0`, `ODbL 1.0` |
| attribution | text | да | Обязательная подпись при публикации |

### data_snapshot — зафиксированный снимок источника

Один скачанный и проверенный файл или ответ API. Именно на снимок ссылаются все
нормализованные записи, поэтому всегда известно, из какой версии они получены.

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| snapshot_id | text | нет | PK; например, `tochno_intercity_v20260629` |
| source_id | text | нет | FK → data_source |
| source_version | text | да | Версия публикации (`20260629`) или `data_version` API, если источник её сообщает |
| retrieved_at | timestamptz | нет | Момент получения, UTC |
| file_name | text | нет | Имя файла внутри `data/raw/`, без абсолютного пути |
| sha256 | text | нет | UK; `CHECK (sha256 ~ '^[0-9a-f]{64}$')`, хеш точных байтов файла |
| size_bytes | bigint | нет | `CHECK (size_bytes > 0)` |
| record_count | integer | да | Фактическое число записей по manifest |
| manifest | jsonb | нет | Manifest целиком: URL, кодировка, разделитель, колонки |

### load_run — запуск нормализации снимка

Отчёт запуска из контракта (`run-report.json`) в табличном виде. Неуспешный запуск
сохраняется для диагностики, но его данные не публикуются.

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| load_run_id | uuid | нет | PK |
| snapshot_id | text | нет | FK → data_snapshot |
| contract_version | text | нет | Версия контракта, по которому шла обработка |
| code_commit | text | нет | Commit кода обработчика |
| status | text | нет | `CHECK (status IN ('running', 'success', 'success_with_rejections', 'failed'))` |
| started_at, finished_at | timestamptz | нет / да | `CHECK (finished_at IS NULL OR finished_at >= started_at)` |
| rows_read, rows_written, rows_rejected | integer | да | Заполняются по завершении; для успешного запуска `CHECK (rows_read = rows_written + rows_rejected)` |
| report | jsonb | да | Полный отчёт запуска: идентичность, повторы, значения `missing_reason`, время и память |

Опубликованный набор снимка — записи последнего успешного `load_run` этого снимка.
Повторная загрузка того же снимка заменяет записи в одной транзакции, поэтому
читатели никогда не видят частично загруженный набор.

### rejected_row — отклонённая строка входа

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| load_run_id | uuid | нет | PK (1/2), FK → load_run |
| source_row_number | integer | нет | PK (2/2); номер физической строки начала записи, заголовок — строка 1 |
| reject_reason | text | нет | Код из контракта: `invalid_number`, `partial_measurement`, `missing_without_reason`, `unknown_transport_type`, `self_pair`, `city_identity_missing`, `exact_duplicate`, `key_collision` |
| detail | text | да | Пояснение `поле=значение` |
| raw | json | нет | Исходная запись без изменений, ключи в порядке колонок файла; `json`, а не `jsonb`, сохраняет порядок и позволяет выгрузить `rejected_rows.csv` как есть |

## Нормализованный слой

### city — город

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| city_id | uuid | нет | PK; `uuid5(NAMESPACE_CITY, 'sber_territory:<Sber ID>')`, см. ADR 0002 |
| name | text | да | Название; не ключ, одноимённые города различаются по `city_id` |
| region | text | да | Субъект РФ |
| population | integer | да | Человек; `CHECK (population >= 0)` |
| location | geometry(Point, 4326) | да | Центр города; `NULL`, если у источника нет согласованной пары координат |
| attributes_snapshot_id | text | нет | FK → data_snapshot; снимок, из которого взяты атрибуты |

Индексы: GiST по `location`; B-tree по `attributes_snapshot_id`.
Признак региональной столицы (`is_capital_*`) по контракту пока не переносится.

### city_external_id — внешний идентификатор города

Внешние коды хранятся отдельно вместе с пространством имён: у одного города может быть
Sber ID, ОКТМО, OSM-объект и код Яндекса. Название никогда не используется как ключ.

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| id_namespace | text | нет | PK (1/2); `CHECK (id_namespace IN ('sber_territory', 'oktmo', 'osm', 'yandex_settlement'))` |
| external_id | text | нет | PK (2/2); значение как в источнике, строкой, с ведущими нулями (ОКТМО `15701000001`, OSM `node/1686293227`) |
| city_id | uuid | нет | FK → city; индекс; `UK (city_id, id_namespace)` — у города не больше одного кода в пространстве имён |
| snapshot_id | text | нет | FK → data_snapshot; где соответствие получено |

Первичный ключ `(id_namespace, external_id)` запрещает привязать один внешний код к двум
городам. Неоднозначное соответствие (например, конфликт Sber ID ↔ ОКТМО по контракту)
не записывается, а попадает в отчёт запуска. Соответствие OSM-объектов пилота городам
(crosswalk) — отдельная задача; до неё данные OSM и OSRM пилота в эту модель не загружаются.

### route_measurement — измерение поездки между городами

Направленное наблюдение «из A в B одним видом транспорта» из конкретного снимка.
Это не ребро графа ([ADR 0003](adr/0003-measurement-vs-graph-edge.md)).

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| measurement_id | uuid | нет | PK; `uuid5(NAMESPACE_MEASUREMENT, '<snapshot_id>\|<from_city_id>\|<to_city_id>\|<transport_type>')` |
| snapshot_id | text | нет | FK → data_snapshot |
| source_row_number | integer | нет | Строка исходного файла; `UK (snapshot_id, source_row_number)` |
| from_city_id | uuid | нет | FK → city |
| to_city_id | uuid | нет | FK → city; `CHECK (from_city_id <> to_city_id)` |
| transport_type | text | нет | `CHECK (transport_type IN ('road', 'rail', 'air'))` |
| distance_m | numeric | да | Метры, `> 0` |
| duration_sec | numeric | да | Секунды, `> 0`; оценка источника по средней скорости, не расписание |
| missing_reason_raw | text | да | Причина или код источника дословно (`CD`, `Маршрут не найден`) |
| source_raw | text | да | Пояснение источника дословно (`OSM`, `OSRM`, `SberIndex`, `ND`) |
| comment_raw | text | да | Комментарий источника дословно |

Ограничения:
- `UK (snapshot_id, from_city_id, to_city_id, transport_type)` — ключ записи из контракта;
- `CHECK ((distance_m IS NULL) = (duration_sec IS NULL))` — частичное измерение не хранится;
- `CHECK (distance_m IS NOT NULL OR missing_reason_raw IS NOT NULL)` — пустое измерение
  без причины отклоняется ещё при обработке;
- индексы по `from_city_id`, `to_city_id`.

Измерения OSRM пилота укладываются в ту же таблицу (`transport_type = 'road'`,
снимок `osrm_public_…`), когда появится соответствие OSM-объектов городам.

### road_segment — участок дороги OSM

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| road_segment_id | uuid | нет | PK; `uuid5` от `(snapshot_id, osm_way_id)` |
| snapshot_id | text | нет | FK → data_snapshot |
| osm_way_id | bigint | нет | `UK (snapshot_id, osm_way_id)` |
| highway | text | нет | Класс дороги OSM (`primary`, `tertiary`, …) |
| name | text | да | |
| oneway | text | да | Тег OSM дословно (`yes`, `no`, `-1`); направление трактует методика графа |
| maxspeed_raw | text | да | Тег дословно: бывают значения вида `RU:urban` |
| surface | text | да | |
| geom | geometry(LineString, 4326) | нет | `CHECK (ST_IsValid(geom))`; индекс GiST |

В пилоте 150 участков в небольшом прямоугольнике Москвы — это не связная сеть между
городами. Многоконтурные объекты при массовой загрузке PBF потребуют `MultiLineString`;
решение принимается в задаче загрузки OSM.

### station — остановочный пункт (проект)

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| station_id | uuid | нет | PK; `uuid5` от `(id_namespace, external_code)` |
| snapshot_id | text | нет | FK → data_snapshot; снимок, из которого получена станция |
| id_namespace | text | нет | Например, `yandex_station` |
| external_code | text | нет | Код станции у источника; `UK (id_namespace, external_code)` |
| name | text | нет | |
| station_type | text | да | Вокзал, аэропорт, автостанция — значения источника |
| city_id | uuid | да | FK → city; `NULL`, пока станция не сопоставлена городу |
| time_zone | text | нет | Часовой пояс IANA (`Europe/Moscow`); по нему восстанавливаются местное время и `service_date` |
| location | geometry(Point, 4326) | да | Индекс GiST |

### transport_connection — рейс по расписанию (проект)

Прямое отправление из расписания на конкретную дату. В отличие от RouteMeasurement
описывает фактический рейс со временем отправления, а не оценку поездки.

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| connection_id | uuid | нет | PK; `uuid5` от ключа уникальности рейса |
| snapshot_id | text | нет | FK → data_snapshot |
| from_station_id, to_station_id | uuid | нет | FK → station; `CHECK (from_station_id <> to_station_id)` |
| transport_type | text | нет | Вид транспорта источника (`train`, `suburban`, `bus`, `plane`) |
| thread_uid | text | да | Идентификатор нитки рейса у источника |
| route_number, carrier | text | да | |
| service_date | date | нет | Дата отправления по местному времени станции |
| departure_at, arrival_at | timestamptz | нет | Момент в UTC; `CHECK (arrival_at > departure_at)`; местное время — через `station.time_zone` |
| duration_sec | integer | нет | `CHECK (duration_sec > 0)` |

Ключ уникальности рейса и полнота кодов станций — предмет [#15](https://github.com/AlexKeyyyy/transport-connectivity/issues/15):
сохранённые записи пилота не содержат кодов станций и ниток, поэтому сейчас из них
нельзя надёжно отличить повтор от разных рейсов. До решения #15 сущность остаётся проектом.

## Аналитический слой

Методика построения графа (смысл ребра, вес, правила пропусков) ещё не утверждена.
Схема ниже фиксирует, как хранится любая выбранная версия графа, но не выбирает методику.

### graph_snapshot — версия графа

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| graph_snapshot_id | uuid | нет | PK |
| methodology_version | text | нет | Версия документа методики, по которой построен граф |
| parameters | jsonb | нет | Параметры построения: виды транспорта, вес, пороги |
| is_directed | boolean | нет | Направленный ли граф |
| status | text | нет | `CHECK (status IN ('building', 'ready', 'superseded', 'failed'))`; новый анализ — только по `ready` |
| code_commit | text | нет | |
| created_at | timestamptz | нет | |

### graph_snapshot_input — входные снимки версии графа

Связь «многие ко многим»: одна версия графа может использовать несколько снимков
(например, CSV связности и расписания), один снимок — входить в несколько версий.

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| graph_snapshot_id | uuid | нет | PK (1/2), FK → graph_snapshot |
| snapshot_id | text | нет | PK (2/2), FK → data_snapshot; индекс |
| load_run_id | uuid | нет | FK → load_run; опубликованная загрузка снимка, из которой построен граф: фиксирует версию контракта и кода |

Данные снимка, на которые опирается версия графа со статусом `ready`, не заменяются:
ссылки `graph_edge → route_measurement` действуют без каскадного удаления, и загрузчик
отказывается перезагружать такой снимок. Чтобы применить новую версию контракта, версию
графа переводят в `superseded`, перезагружают снимок и строят новую версию графа.

### graph_node — вершина версии графа

Состав вершин хранится явно: иначе изолированный город (без рёбер) пропадает из анализа.

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| graph_snapshot_id | uuid | нет | PK (1/2), FK → graph_snapshot |
| city_id | uuid | нет | PK (2/2), FK → city; индекс |

### graph_edge — ребро версии графа

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| graph_edge_id | uuid | нет | PK; `uuid5` от `(graph_snapshot_id, from_city_id, to_city_id, transport_type)` |
| graph_snapshot_id | uuid | нет | FK → graph_snapshot |
| from_city_id, to_city_id | uuid | нет | `FK (graph_snapshot_id, from_city_id) → graph_node` и то же для `to_city_id`: ребро не может ссылаться на город вне версии; `CHECK (from_city_id <> to_city_id)` |
| transport_type | text | нет | Вид транспорта или `multimodal`, если методика объединяет виды |
| weight | numeric | нет | `CHECK (weight > 0)`; стоимость: меньше — лучше |
| weight_unit | text | нет | `CHECK (weight_unit IN ('sec', 'm'))` |
| measurement_id | uuid | да | FK → route_measurement; измерение, из которого получено ребро, если методика строит его из одного измерения |

`UK (graph_snapshot_id, from_city_id, to_city_id, transport_type)`. В ненаправленной
версии (`is_directed = false`) ребро хранится один раз в канонической ориентации
`from_city_id < to_city_id`. Её обеспечивает построитель графа и проверяет тест загрузки:
`CHECK` не может прочитать `is_directed` из другой таблицы. Если методика
выводит ребро из нескольких наблюдений, связь «ребро — измерения» выносится в отдельную
таблицу при утверждении методики.

### analysis_run — запуск расчёта

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| analysis_run_id | uuid | нет | PK |
| graph_snapshot_id | uuid | нет | FK → graph_snapshot; индекс |
| algorithm | text | нет | Например, `dijkstra`, `betweenness` |
| engine | text | нет | Где выполнено и какой версии: `neo4j-gds 2.x`, `networkx 3.x` |
| parameters | jsonb | нет | |
| scenario | jsonb | да | Изменение графа для сценария устойчивости (удалённые/добавленные рёбра); `NULL` — базовый граф |
| code_commit | text | нет | |
| status | text | нет | `CHECK (status IN ('running', 'success', 'failed'))` |
| started_at, finished_at | timestamptz | нет / да | |

### metric_result — результат расчёта

| Поле | Тип | NULL | Ограничения и смысл |
|---|---|---|---|
| metric_result_id | bigint | нет | PK, `generated always as identity` |
| analysis_run_id | uuid | нет | FK → analysis_run; индекс |
| metric | text | нет | Имя показателя: `betweenness`, `travel_time_sec`, `reachable` |
| city_id | uuid | да | FK → city; город, к которому относится значение; `NULL` — показатель всего графа (число компонент, размер наибольшей) |
| target_city_id | uuid | да | FK → city; второй город для показателей пары (время A → B) |
| value | double precision | да | `NULL` — значение не определено, например, город недостижим |

`UK (analysis_run_id, metric, city_id, target_city_id) NULLS NOT DISTINCT` (PostgreSQL 15+);
`CHECK (target_city_id IS NULL OR city_id IS NOT NULL)`. Что города результата — вершины
версии графа этого расчёта, проверяет запись результатов: составной ключ потребовал бы
повторять `graph_snapshot_id` в каждой строке.

## Связи и кратность

| Связь | Кратность | Смысл |
|---|---|---|
| data_source → data_snapshot | 1 : 0..N | У источника много снимков |
| data_snapshot → load_run | 1 : 0..N | Снимок можно обработать повторно |
| load_run → rejected_row | 1 : 0..N | |
| city → city_external_id | 1 : 1..N | У города минимум Sber ID |
| city → route_measurement | 1 : 0..N дважды | Город отправления и назначения — две разные связи |
| data_snapshot → route_measurement, road_segment, station, transport_connection | 1 : 0..N | Происхождение каждой записи |
| city → station | 0..1 : 0..N | Станция может быть ещё не сопоставлена городу |
| station → transport_connection | 1 : 0..N дважды | |
| graph_snapshot ↔ data_snapshot | N : M через graph_snapshot_input | Каждый вход закреплён за конкретным load_run |
| graph_snapshot → graph_node → graph_edge | 1 : N, вершина 1 : 0..N рёбер дважды | |
| route_measurement → graph_edge | 0..1 : 0..N | Ребро может опираться на измерение |
| graph_snapshot → analysis_run → metric_result | 1 : 0..N : 0..N | |

## Пример: реальная строка в модели

Строка 2 файла `data/samples/intercity_connectivity_sample.csv` (первая запись данных):

```text
Брянск;Брянская область;15701000001;953;1;379152;53.2420053;34.3653002;
Владимир;Владимирская область;17701000001;986;1;349951;56.1280444;40.4082983;
road;694.0;9.8;CD;OSM;CD
```

| Куда | Значение |
|---|---|
| data_snapshot | `tochno_intercity_v20260629`, source_id `tochno_intercity` |
| city (отправление) | city_id `83c80e94-954a-5182-b2dd-a4aa219e6400` = uuid5(NAMESPACE_CITY, `sber_territory:953`); name `Брянск`; region `Брянская область`; population `379152`; location `POINT(34.3653002 53.2420053)` |
| city_external_id | (`sber_territory`, `953`) и (`oktmo`, `15701000001`) → этот city_id |
| city (назначение) | city_id `7e9d63d7-2778-54ed-bd76-e000209493d6` = uuid5(NAMESPACE_CITY, `sber_territory:986`); `Владимир`, (`oktmo`, `17701000001`) |
| route_measurement | measurement_id `fb7e015a-513c-5f3b-849a-a5a886ad08a8`; source_row_number `2`; transport_type `road`; distance_m `694000`; duration_sec `35280`; missing_reason_raw `CD`; source_raw `OSM`; comment_raw `CD` |
| не переносится | `is_capital_from = 1`, `is_capital_to = 1` (остаются в исходном файле) |

Для той же пары `air` источник даёт пустые числа и `Маршрут не найден`: запись
сохраняется с `distance_m = NULL`, `duration_sec = NULL`, `source_raw = ND`.
Идентификаторы посчитаны по формулам контракта; константы пространств имён проверены.

## Производные представления

### Neo4j — проекция версии графа

Строится из `graph_node` и `graph_edge` одной версии со статусом `ready`.

- Одна проекция содержит одну версию графа; смена версии — пересборка проекции.
  Идентификатор версии хранится в служебной вершине `(:GraphSnapshot)`.
- Вершина `(:City {city_id, name, region, population})` создаётся для каждой записи
  `graph_node`, включая изолированные; ключ `city_id` с ограничением уникальности.
- Связь `(:City)-[:CONNECTED {graph_edge_id, graph_snapshot_id, transport_type, weight, weight_unit}]->(:City)`.
  Свойства хранятся на связи; промежуточный узел для них не нужен.
- Результаты расчётов записываются обратно в `analysis_run` и `metric_result`.

### Elasticsearch — индекс `cities`

Строится из `city` и `city_external_id`; подробный mapping и запросы — в задаче поиска
([#5](https://github.com/AlexKeyyyy/transport-connectivity/issues/5), `docs/elasticsearch.md`).

| Поле документа | Откуда | Тип в индексе |
|---|---|---|
| `_id`, `city_id` | city.city_id | keyword |
| `name` | city.name | text с подполем keyword |
| `aliases` | Другие написания из источников (появятся при сопоставлении OSM/Яндекса) | text |
| `region` | city.region | keyword |
| `population` | city.population | integer |
| `location` | city.location | geo_point, объект `{lat, lon}` |
| `external_ids` | city_external_id | keyword, вида `oktmo:15701000001` |

## Что модель не решает

- Методику графа: смысл ребра, вес и допустимые сценарии устойчивости.
- Соответствие OSM-объектов пилота городам по Sber ID (crosswalk).
- Ключ уникальности рейса расписаний (#15).
- Историю изменения атрибутов города между снимками: хранится значение из последнего
  загруженного снимка и ссылка на него. Загрузка более старого снимка после нового
  откатит атрибуты, а проекции старой версии графа покажут текущие название и население.
  Когда появится второй снимок того же источника, атрибуты выносятся в таблицу по снимкам.
- Состояние «ошибка источника» для измерений из API (OSRM, Яндекс): в CSV связности его
  нет. Решается при загрузке этих источников журналом запросов с исходом «маршрут найден /
  маршрута нет / ошибка», а не строкой с пустыми числами.
