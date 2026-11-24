# Замеры полного снимка

24 ноября 2026 года, 22:56 МСК. Чистый клон `61158ce` от `develop` `9465248`.
Windows 11 Pro, Intel Core i5-12400F (12 логических процессоров), 31,84 ГиБ
доступной системе физической памяти. Python 3.12.3, Docker Desktop 4.41.2,
PostgreSQL 17.5, PostGIS 3.5.2. Снимок `tochno_intercity_v20260629` содержит
96 660 строк в CSV объёмом 23 511 679 байт.
[Manifest и контрольная сумма](../deliverables/interim/evidence/source-manifest.json)

| Этап | Время по часам, с | Пик памяти Python, МиБ |
|---|---:|---:|
| `download --offline` | 0,770 | 163,47 |
| `process` | 25,230 | 595,71 |
| `migrate` в пустую базу | 0,370 | 36,23 |
| Первый `load` | 2,216 | 41,62 |
| Повторный `load` | 0,329 | 36,91 |
| Экспорт карты | 0,762 | 59,14 |

Один проход, по одному процессу Python на команду. Время измеряет
`perf_counter` от запуска Python-модуля или скрипта до выхода из него.
Память — пиковый рабочий набор процесса на Windows. На Linux/macOS скрипт
читает `ru_maxrss`. Таблица учитывает клиентский процесс. Запуск Docker,
память сервера PostgreSQL, интернет-загрузка и отрисовка карты в браузере
в замеры не входят. Наибольшие время и пиковое потребление памяти в этом
проходе пришлись на нормализацию.

## Повторить

Используйте [инструкцию запуска](../README.md#воспроизвести-проверенный-сценарий)
с отдельной пустой базой. Сначала выполните сетевой `download` и запустите
PostGIS, затем выполните команды ниже вместо соответствующих команд README.

```powershell
python benchmarks/measure.py -m transport_connectivity.cli download --offline
python benchmarks/measure.py -m transport_connectivity.cli process
python benchmarks/measure.py -m transport_connectivity.cli migrate
python benchmarks/measure.py -m transport_connectivity.cli load
python benchmarks/measure.py -m transport_connectivity.cli load
python benchmarks/measure.py scripts/dev/export_map_accessibility.py
```

Скрипт также принимает путь к Python-скрипту расчёта и его аргументы. Замеры
`build-graph`, расчётов с БД и проекции GDS появятся после интеграции
[#28](https://github.com/AlexKeyyyy/transport-connectivity/issues/28),
[#33](https://github.com/AlexKeyyyy/transport-connectivity/issues/33) и
[#34](https://github.com/AlexKeyyyy/transport-connectivity/issues/34).

## Контроль чистого клона

Первый `download` скачал архив из источника. Нормализация прочитала 96 660
строк, записала 96 660 измерений и 0 отклонений. SHA-256 нормализованного
`route_measurements.csv`:

```text
719f0d87035242548b72ee0764b9c7f4ddd35a077af708ecfec8bba04d2248e6
```

Применились четыре миграции, включая аналитический слой. Первый `load`
записал снимок, повторный сообщил `already loaded`. После каждого вызова
проверен один и тот же результат SQL в свежем контейнере.

```sql
SELECT (SELECT count(*) FROM city) AS cities,
       (SELECT count(*) FROM city_external_id) AS external_ids,
       (SELECT count(*) FROM route_measurement) AS measurements,
       (SELECT count(*) FROM rejected_row) AS rejected,
       (SELECT count(*) FROM data_snapshot) AS snapshots,
       (SELECT count(*) FROM load_run) AS loads;
-- 180 | 360 | 96660 | 0 | 1 | 1
```

Выгрузка карты успешно прочитана в Streamlit. Тест интерфейса выбрал Москву,
слой «Лучший из трёх», 6 часов и проверил 101 строку в таблице направлений.
[Инструкция и скриншот карты](../docs/map.md)