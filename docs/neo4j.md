# Экспорт графа в Neo4j

PostgreSQL хранит определение и веса графа. Neo4j получает копию готовой
версии для работы GDS. Вершина `:City` хранит `graph_snapshot_id`,
`city_id`, имя, регион и население. Направленная связь `:CONNECTED`
хранит вид транспорта, вес и его единицу. UUID города может встречаться
в разных версиях. Пара `(graph_snapshot_id, city_id)` уникальна.
[Модель данных](data-model.md#neo4j-и-elasticsearch)

## Запуск

Сервис Compose использует Neo4j Community 5.26.0 с GDS. По умолчанию он
публикует HTTP на 7474 и Bolt на 7687, оба на loopback. Если эти порты
заняты, переопределите `NEO4J_HTTP_PORT` и `NEO4J_BOLT_PORT`. Начальный
пользователь пустого тома — `neo4j`. Задайте `NEO4J_PASSWORD` перед
первым запуском. Проверка готовности выполняет `RETURN gds.version()`.
[Плагин GDS в Docker](https://neo4j.com/docs/graph-data-science/current/installation/installation-docker/)

В окружении Python задайте `NEO4J_URI`, `NEO4J_USER` и `NEO4J_PASSWORD`.
Python читает переменные процесса. При локальном запуске используйте
тот же пароль, что при настройке Compose.

```powershell
python -m pip install -e ".[neo4j]"
docker compose up -d --wait neo4j
python -m transport_connectivity.cli export-graph --all-ready
# Для одной версии передайте UUID из build-graph:
# python -m transport_connectivity.cli export-graph --graph-snapshot <uuid>
```

В PostgreSQL нужны версии `ready` из команды `build-graph`.
[Подготовка графов](graph-methodology.md)

Экспорт пересобирает выбранную версию пакетами в одной транзакции
Neo4j. Перед фиксацией команда сверяет UUID и атрибуты вершин, направление,
вид, вес и единицу каждого ребра с PostgreSQL. Расхождение откатывает
перестройку. Другие версии остаются доступны. Повторный экспорт сохраняет
состав графа. Внутренние ID Neo4j могут меняться, для ссылок используется
UUID города вместе с UUID версии.

## Проверка GDS

В Neo4j Browser или через драйвер передайте `snapshot` — UUID готовой
версии `road` с α = 0,05. Параметр `graph` — имя временной проекции,
например `proof-road-005`. Выполните следующие запросы по очереди.

```cypher
MATCH (a:City {graph_snapshot_id: $snapshot})
OPTIONAL MATCH (a)-[r:CONNECTED]->(b:City {graph_snapshot_id: $snapshot})
WITH gds.graph.project($graph, a, b, {relationshipProperties: r {.weight}}) AS g
RETURN g.nodeCount AS nodes, g.relationshipCount AS edges
```

```cypher
CALL gds.betweenness.stream($graph, {relationshipWeightProperty: 'weight'})
YIELD nodeId, score
RETURN gds.util.asNode(nodeId).city_id AS city_id, score
```

```cypher
CALL gds.graph.drop($graph, false) YIELD graphName
RETURN graphName
```

Контрольный прогон 25 ноября 2026 года по московскому времени использовал
плагин GDS 2.13.2. После первого и повторного экспорта
`--all-ready` числа в Neo4j совпали с PostgreSQL:

| Слой | α | Вершины | Рёбра |
|---|---:|---:|---:|
| road | 0,05 | 180 | 3 866 |
| road | 0,10 | 180 | 2 224 |
| road | 0,20 | 180 | 1 198 |
| rail | 0,05 | 180 | 934 |
| rail | 0,10 | 180 | 702 |
| rail | 0,20 | 180 | 488 |
| best | 0,05 | 180 | 5 642 |
| best | 0,10 | 180 | 3 218 |
| best | 0,20 | 180 | 1 350 |

Для `road`, α = 0,05, GDS вернул 180 строк. Сохранение
аналитических результатов выполняется в задаче
[#35](https://github.com/AlexKeyyyy/transport-connectivity/issues/35).

Для автоматической проверки используйте отдельный тестовый Neo4j.
После установки `.[dev,neo4j]` и настройки подключения запустите
`python -m pytest -m neo4j -v`. Тест проверяет повторный экспорт,
изоляцию версий и сохранение предыдущей копии при ошибке сверки.
