# Сквозной прогон и замеры

25 ноября 2026 года, МСК. Свежий клон с коммитом
[`67d7c23`](https://github.com/AlexKeyyyy/transport-connectivity/commit/67d7c23e656682306754dddd1b7272f40130706e),
новое окружение Python и пустые тома Docker Compose.
Windows 11 Pro, Intel Core i5-12400F (12 логических процессоров), 31,84 ГиБ,
Python 3.12.3. Сервисы: PostgreSQL 17.5, PostGIS 3.5.2, Neo4j 5.26.0,
GDS 2.13.2 и Elasticsearch 8.19.0.

Вход — `tochno_intercity_v20260629`, 96 660 записей в CSV объёмом
23 511 679 байт. [Манифест снимка](../deliverables/interim/evidence/source-manifest.json)

| Этап | Время, с | Пиковый рабочий набор Python, МиБ |
|---|---:|---:|
| `download --offline` | 1,077 | 191,32 |
| `process` | 25,215 | 622,52 |
| `migrate` | 0,749 | 63,98 |
| `load` | 2,875 | 69,10 |
| `load (повтор)` | 0,612 | 64,80 |
| `accessibility --include-air` | 3,737 | 136,15 |
| `build-graph --all` | 4,378 | 172,12 |
| `export-graph --all-ready` | 5,757 | 144,47 |
| `key-nodes` | 4,713 | 140,70 |
| `node-removal` | 61,206 | 76,59 |
| `link-scenarios` | 2,993 | 88,79 |
| `index-cities` | 1,254 | 63,59 |
| `Экспорт офлайн-карты` | 0,680 | 57,59 |

Один проход. Каждый этап запущен в отдельном процессе Python. Скрипт измеряет
`perf_counter` от входа в модуль до завершения и пиковую память клиентского
процесса. Память сервисов и запуск контейнеров в таблицу не входят.
Сетевое скачивание выполнено перед замером проверки кэша. Наибольшее время
заняло удаление узлов, наибольшая пиковая память — нормализация.

## Повторить

Выполните [команды README](../README.md#воспроизвести-проверенный-сценарий)
в отдельных пустых контейнерах. Для замера замените нужные вызовы на:

```powershell
python benchmarks/measure.py -m transport_connectivity.cli download --offline
python benchmarks/measure.py -m transport_connectivity.cli process
python benchmarks/measure.py -m transport_connectivity.cli load
python benchmarks/measure.py -m transport_connectivity.cli build-graph --all
python benchmarks/measure.py -m transport_connectivity.cli key-nodes
python benchmarks/measure.py -m transport_connectivity.cli node-removal
python benchmarks/measure.py scripts/dev/export_map_accessibility.py
```

## Контрольные значения

Нормализация записала 96 660 измерений и 0 отклонений. SHA-256
`route_measurements.csv` — `719f0d87035242548b72ee0764b9c7f4ddd35a077af708ecfec8bba04d2248e6`.
Применились четыре миграции. После первого и повторного `load`
контрольный запрос вернул один и тот же результат:

```sql
SELECT (SELECT count(*) FROM city) AS cities,
       (SELECT count(*) FROM city_external_id) AS external_ids,
       (SELECT count(*) FROM route_measurement) AS measurements,
       (SELECT count(*) FROM rejected_row) AS rejected,
       (SELECT count(*) FROM data_snapshot) AS snapshots,
       (SELECT count(*) FROM load_run) AS loads;
-- 180 | 360 | 96660 | 0 | 1 | 1
```

- `accessibility --include-air` опубликовала 12 пороговых запусков и 4 слоя времени.
- `build-graph --all` и `export-graph --all-ready` дали 9 версий по 180 вершин.
  Число рёбер каждой версии совпало в PostgreSQL и Neo4j.
  [Таблица по слоям и α](../docs/neo4j.md#проверка-gds)
- `key-nodes`, `node-removal` и `link-scenarios` создали 9, 12 и 23 запуска
  соответственно. Всего в аналитической базе 60 готовых запусков с учётом
  доступности. [Сценарии и методика](../docs/analysis-results.md)
- `index-cities` опубликовала 180 городов с координатами через псевдоним `cities`.
- Streamlit в режимах базы и офлайн показал 101 город из Москвы за 6 ч.
  В режиме базы «Брянкс» нашёл Брянск. Для ВСМ из Москвы получен прирост
  +224 286 жителей. В сценарии удаления значение для Москвы отсутствует.
  [Контрольный показ](../docs/map.md#контрольные-примеры)
