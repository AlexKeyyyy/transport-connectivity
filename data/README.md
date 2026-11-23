# Каталог данных

- `raw/` — исходные выгрузки и временные файлы; в Git не попадают.
- `processed/` — локальные промежуточные данные; в Git не попадают.
- `samples/` — небольшие описанные срезы, которые можно хранить в Git. Состав и поля —
  в [`data/samples/README.md`](samples/README.md).

## Полный набор «Если быть точным»

Команда `download` скачивает закреплённую версию 20260629, проверяет архив и CSV
и пишет `manifest.json` рядом с файлами:

```bash
python -m transport_connectivity.cli download            # первый запуск, нужна сеть
python -m transport_connectivity.cli download --offline  # повтор только из кэша
```

Результат лежит в `data/raw/tochno_intercity_v20260629/`: ZIP, извлечённый CSV и manifest
(URL, версия, время загрузки UTC, SHA-256 и размер архива и CSV, кодировка, разделитель,
число записей, 22 колонки, число городов по Sber ID, сравнение с опубликованным объёмом).

- Архив и CSV проверяются в памяти до записи на диск: целостность ZIP, ровно один CSV,
  UTF-8, точный заголовок из [контракта данных](../docs/data-contract.md), одинаковое
  число полей в каждой записи. Непрошедший проверку файл не сохраняется как кэш.
- Повторный запуск берёт проверенный кэш без сети. Повреждённый кэш скачивается заново,
  а с `--offline` запуск завершается ошибкой.
- Сетевые сбои, таймауты и ответы 408/429/5xx дают до трёх попыток; остальные
  ошибки HTTP (например, 404) завершают загрузку сразу.
- Manifest записывается последним: каталог без него не считается готовым снимком.

Копия manifest реальной загрузки —
[`deliverables/interim/evidence/source-manifest.json`](../deliverables/interim/evidence/source-manifest.json).

## Нормализация по контракту

Команда `process` читает снимок, проверенный `download`, и применяет
[контракт данных](../docs/data-contract.md):

```bash
python -m transport_connectivity.cli process
```

- До чтения данных SHA-256 CSV сверяется с manifest, заголовок — с 22 колонками контракта,
  затем число записей — с manifest.
  При расхождении запуск завершается ошибкой (статус `failed`) и ничего не публикует.
- Результат пишется во временный каталог и целиком заменяет
  `data/normalized/tochno_intercity_v20260629/`: `cities.csv`, `route_measurements.csv`,
  `rejected_rows.csv` и `run-report.json`. Каталог в Git не попадает.
- Повторный запуск на том же входе даёт байт-в-байт те же три CSV и не накапливает записи.
  Отличается только блок `service` отчёта (время, память, версия кода).
- Баланс: каждая входная запись попадает ровно в один файл —
  `route_measurements.csv` или `rejected_rows.csv`. Удалённые точные повторы
  (`exact_duplicate`) лежат в `rejected_rows.csv` и отдельно посчитаны в блоке `duplicates`.

Отчёты двух полных прогонов —
[`processing-run-1.json`](../deliverables/interim/evidence/processing-run-1.json) и
[`processing-run-2.json`](../deliverables/interim/evidence/processing-run-2.json).

## Прямые рейсы Яндекс Расписаний

Команда `rasp-download` собирает прямые поезда и самолёты между нашими городами на одну
дату D ([план и правила](../docs/yandex-schedules-plan.md)). Сбор занимает несколько дней
из-за суточной квоты ключа, поэтому команду запускают раз в сутки, пока manifest не
покажет `"complete": true`:

```bash
export YANDEX_RASP_API_KEY=...   # или: set -a; . ./.env; set +a
python -m transport_connectivity.cli rasp-download --date 2026-12-01
```

- Вход — `data/normalized/tochno_intercity_v20260629/cities.csv` (после `process`).
- Каждый ответ API сохраняется как есть вместе с запросом в
  `data/raw/yandex_rasp_20261201/` (`stations_list.json`, `schedule/`, `thread/`).
  Повторный запуск берёт уже полученные ответы с диска и запрашивает только недостающие.
- Квота: счётчик `data/raw/yandex_rasp_quota.json` по московским суткам, не больше 500
  вызовов, повторные попытки тоже считаются. `--max-requests N` ограничивает один запуск.
  У пилотного скрипта ниже свой счётчик — в дни сбора его с тем же ключом не запускать.
- Ответ 404 («рейс не ходит», «станция не найдена») — ответ источника, он сохраняется.
  Другие ошибки 4xx и сбои после трёх попыток останавливают запуск; ничего недополученного
  не помечается как готовое.
- `manifest.json` обновляется после каждого запуска, в том числе завершившегося ошибкой:
  `complete`, причина остановки,
  сопоставление городов, число запросов по методам и статусам, SHA-256 всех ответов.
  `city_matching.csv` — таблица «город → населённый пункт Яндекса» для ручной сверки.
- `--offline` ничего не запрашивает: проверяет, что сохранённых ответов хватает на весь план.
- В материалах с этими данными указывается источник — Яндекс Расписания (условие API).

### Нормализация прямых связей

После завершения сбора запустите обработку сохранённых ответов. Ключ и сеть ей не нужны.

```bash
python -m transport_connectivity.cli rasp-process --date 2026-12-01
```

Команда принимает только полный снимок и сверяет количество и SHA-256 ответов,
а также SHA-256 `cities.csv` с manifest. Необязательные параметры
`--raw-root`, `--cities` и `--output-root` позволяют указать каталоги исходных
снимков, файл городов и каталог результатов.

Результат в `data/normalized/yandex_rasp_20261201/`:

| Файл | Содержание |
|---|---|
| `stations.csv` | Коды железнодорожных станций и аэропортов Яндекса, код населённого пункта Яндекса, UUID города проекта и вид транспорта. |
| `services.csv` | По одному варианту поездки между станциями для ключа `transport_type, uid, departure_date, from_station, to_station`. Время посадки содержит местное смещение. `departure_response` и `arrival_response` указывают на исходные JSON относительно каталога снимка. Для поездов второй JSON содержит нитку. |
| `direct_links.csv` | Направленная пара `from_city_id, to_city_id`, вид `rail\|air`, число станционных вариантов `services`, минимальное время `min_duration_sec` и `snapshot_id`. |
| `run-report.json` | Исходные и выходные хэши, баланс отправлений и пар, известные пропуски и служебные данные запуска. |

Обработка заменяет результат целиком после успешных проверок. На том же входе
три CSV совпадают побайтно. В отчёте меняются время, длительность и память запуска.
`services` учитывает разные станции одного города отдельно, поэтому один
физический рейс может дать несколько вариантов для одной городской пары.

### Авиационный сценарий «Без Москвы»

```bash
python -m transport_connectivity.cli rasp-air-resilience --date 2026-12-01
```

Команда сверяет CSV с отчётами нормализации, считает достижимые направленные
пары и печатает JSON. Для снимков вне репозитория укажите `--cities-dir` и
`--rasp-dir`. [Отчёт и схема для предзащиты](../deliverables/predefense/yandex-air-resilience.md).

## Пилотный срез и его воспроизведение

Пилот M0-1 использует набор «Если быть точным», OSM через Overpass API и публичный
OSRM Table. Обновить файлы:

```bash
py scripts/data/download_samples.py
py scripts/data/profile_samples.py
```

Необязательные переменные окружения: `OVERPASS_URL`, `OSRM_URL`, `YANDEX_RASP_API_KEY`
и `YANDEX_RASP_DAILY_LIMIT` (не больше 500). Скрипт читает их и из игнорируемого
локального `.env`. С ключом Яндекса он находит 10 пилотных городов и запрашивает прямые
рейсы между Москвой и остальными городами в обе стороны (не больше 28 запросов).
`YANDEX_RASP_BYPASS_PROXY=1` отключает настроенные HTTP(S)-прокси для этих запросов.
JSON хранит число запросов и указание источника. Полные региональные PBF для пилота
не скачиваются.

Большие PBF, исходные выгрузки, данные OSRM и промежуточные файлы в Git не добавляются;
это обеспечивают правила `.gitignore`.

## Загрузка в базу

Из корня репозитория подготовьте снимок командами `download` и `process`,
скопируйте `.env.example` в `.env` для Docker Compose и задайте те же
`POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER` и
`POSTGRES_PASSWORD` в окружении CLI, если меняете значения по умолчанию.
Укажите свободный `POSTGRES_PORT`, если 5432 занят.

```powershell
docker compose up -d --wait postgres
python -m transport_connectivity.cli migrate
python -m transport_connectivity.cli load
```

Если снимок лежит в другом каталоге, передайте `load --raw-dir … --normalized-dir …`.
Загрузчик сверяет исходную и выходные контрольные суммы. Повторные вызовы
`migrate` и `load` пропускают применённые миграции и уже загруженный снимок.

Откройте `docker compose exec postgres psql -U postgres -d transport_connectivity`
с вашим пользователем и именем базы. Контрольные запросы для чистой базы:

```sql
SELECT (SELECT count(*) FROM city) AS city,
       (SELECT count(*) FROM city_external_id) AS city_external_id,
       (SELECT count(*) FROM route_measurement) AS route_measurement,
       (SELECT count(*) FROM rejected_row) AS rejected_row,
       (SELECT count(*) FROM data_snapshot) AS data_snapshot,
       (SELECT count(*) FROM load_run) AS load_run;
-- 180 | 360 | 96660 | 0 | 1 | 1

SELECT count(*) FROM city
WHERE ST_DWithin(location::geography,
      ST_SetSRID(ST_MakePoint(37.62049, 55.7540584), 4326)::geography, 25000);
-- 11

SELECT city_id FROM city_external_id
WHERE id_namespace = 'sber_territory' AND external_id = '953';
-- 83c80e94-954a-5182-b2dd-a4aa219e6400
```

Если в локальной базе уже применялась предварительная схема из PR #41,
используйте новую пустую базу и загрузите снимок повторно. Миграции отмечаются
по имени файла, а исходные номера строк восстанавливаются из нормализованного CSV.

Для интеграционных тестов задайте `RUN_DB_TESTS=1` и `POSTGRES_HOST` вместе
с остальными параметрами `POSTGRES_*` отдельной тестовой базы.
Команда запуска: `python -m pytest -m db -v`.
