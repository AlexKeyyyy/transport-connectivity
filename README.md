# Transport Connectivity

Исследуем транспортную связность 180 городов России. Сколько городов и жителей
доступно за 3, 6 и 24 часа? Через какие города проходят маршруты в модели?
Как меняется доступность при удалении узла или добавлении связи?

## Данные и методика

| Источник | Что используем |
|---|---|
| «Если быть точным», снимок 20260629 | 96 660 измерений времени и расстояния для направленных пар городов и трёх видов транспорта |
| Яндекс Расписания, дата поездки 1 декабря 2026 года | Наблюдения прямых рейсов для отдельного анализа покрытия и связности |
| OpenStreetMap и OSRM | Города и расчёт автомобильных маршрутов в пилоте |

Команда `process` проверяет контрольные суммы и баланс строк, сопоставляет
города по внешним идентификаторам и приводит расстояния и время к метрам и
секундам. Для карты слой «Лучший из трёх» выбирает минимальное время для каждой
пары, добавляя к авиационному времени 3 часа. Доступное население — сумма
населения других городов, укладывающихся в выбранный порог.

Для оценки устойчивости нужна отдельная модель связей. Запись матрицы описывает
поездку между городами, которая может проходить через другие узлы. Методика
прореживания описана в [методике графа](docs/graph-methodology.md).
[Контракт данных](docs/data-contract.md) · [Ограничения источников](docs/constraints.md)

## От данных к результату

```text
Снимок → download → CSV и manifest → process → нормализованные CSV
                                                ├─→ load → PostgreSQL/PostGIS → accessibility → карта
                                                │                  └─→ build-graph → Neo4j/GDS
                                                │                        └─→ сценарии в PostgreSQL → карта
                                                ├─→ index-cities → Elasticsearch → поиск на карте
                                                └─→ экспорт JSON → офлайн-карта
```

PostgreSQL/PostGIS хранит нормализованные данные и результаты. Neo4j получает
копии версий графа для расчёта посредничества, Elasticsearch индексирует города
из того же нормализованного справочника. [Архитектура](docs/architecture.md)

## Воспроизвести проверенный сценарий

Нужны Git, Python 3.12+, Docker с Compose и интернет для первичной установки и
загрузки основного снимка. Команды выполняются из корня свежего клона. Порты
ниже предназначены для отдельного локального запуска. Если какой-то занят,
поменяйте его и в адресе соответствующего клиента.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,app,neo4j]"

python -m transport_connectivity.cli download
python -m transport_connectivity.cli process

$env:POSTGRES_HOST = "127.0.0.1"
$env:POSTGRES_PORT = "55439"
$env:POSTGRES_DB = "transport_connectivity"
$env:POSTGRES_USER = "postgres"
$env:POSTGRES_PASSWORD = "postgres"
$env:NEO4J_HTTP_PORT = "57439"
$env:NEO4J_BOLT_PORT = "57639"
$env:NEO4J_URI = "bolt://127.0.0.1:57639"
$env:NEO4J_USER = "neo4j"
$env:NEO4J_PASSWORD = "transport-local-demo"
$env:ELASTICSEARCH_PORT = "59339"
$env:ELASTICSEARCH_URL = "http://127.0.0.1:59339"
docker compose -p transport-defense up -d --wait postgres neo4j elasticsearch

python -m transport_connectivity.cli migrate
python -m transport_connectivity.cli load
python -m transport_connectivity.cli load
python -m transport_connectivity.cli accessibility --include-air
python -m transport_connectivity.cli build-graph --all
python -m transport_connectivity.cli export-graph --all-ready
python -m transport_connectivity.cli key-nodes
python -m transport_connectivity.cli node-removal
python -m transport_connectivity.cli link-scenarios
python -m transport_connectivity.cli index-cities
python -m streamlit run src/transport_connectivity/app/map_app.py -- --source db
```

Откройте адрес из вывода Streamlit. В Linux/macOS активируйте окружение через
`source .venv/bin/activate`, переменные задайте через `export`. В PowerShell можно
вызывать `.venv\Scripts\python.exe` вместо активации. После показа остановите
Streamlit и выполните `docker compose -p transport-defense stop`.

### Офлайн-показ

После первоначальной нормализации можно подготовить и показать карту с локальным
JSON. Для подложки OpenStreetMap нужен интернет, на случай его отсутствия есть
скриншот ниже. API-ключ Яндекс Расписаний для обоих показов карты не нужен.

```powershell
python scripts/dev/export_map_accessibility.py
python -m streamlit run src/transport_connectivity/app/map_app.py -- --source offline
```

## Контрольные результаты

| Проверка | Результат | Доказательство |
|---|---|---|
| Нормализация полного снимка | 180 городов, 96 660 измерений, 0 отклонённых строк | [Отчёт прогона](deliverables/interim/evidence/processing-run-1.json) |
| Первая и повторная загрузка в PostGIS | 180 городов, 360 внешних ID, 96 660 измерений, один запуск загрузки в базе | [Прогон и контрольный SQL](benchmarks/README.md) |
| Проекции и расчёты | 9 графов по 180 городов, готовые метрики и сценарии | [Сквозной прогон](benchmarks/README.md) |
| Карта, Москва, «Лучший из трёх», 6 часов | 101 достижимый город по матрице времени | [Проверка карты](docs/map.md) |
| Авиаграф Яндекс Расписаний на 1 декабря | 482 ребра. При удалении Москвы число достижимых направленных пар среди остальных городов снизилось с 4 900 до 3 660 | [Эксперимент](deliverables/predefense/yandex-air-resilience.md) и [покрытие](deliverables/predefense/yandex-air-coverage.md) |

![Карта достижимости из Москвы за шесть часов](docs/map-reachability.png)

Для карты выберите Москву через поиск, слой «Лучший из трёх» и порог 6 часов.
В режиме базы запрос «Брянкс» находит Брянск. При включении сценария ВСМ
изменение доступного населения из Москвы за 6 часов составляет +224 286 жителей.
В сценарии «Без Москвы» значение для удалённого города отсутствует. Для примера
стыковки по расписанию используйте отдельный авиационный эксперимент.

## Где что находится

| Каталог | Содержание |
|---|---|
| [`src/transport_connectivity/`](src/transport_connectivity/) | CLI, загрузка, обработка, PostgreSQL и карта |
| [`data/`](data/README.md) | Получение снимков, схема каталогов и контрольные запросы |
| [`docs/`](docs/README.md) | Модель данных, диаграммы, ограничения и план |
| [`tests/`](tests/) | Проверки преобразований, схемы и карты |
| [`benchmarks/`](benchmarks/README.md) | Замеры времени и памяти |
| [`deliverables/`](https://github.com/AlexKeyyyy/transport-connectivity/pull/52) | Отчёты, материалы и доказательства запусков |

### Вклад команды

| Участник | Работа и проверяемые изменения |
|---|---|
| Алексей | Сбор и обработка расписаний, аналитические расчёты и интеграция. [#42](https://github.com/AlexKeyyyy/transport-connectivity/pull/42), [#51](https://github.com/AlexKeyyyy/transport-connectivity/pull/51), [#56](https://github.com/AlexKeyyyy/transport-connectivity/pull/56) |
| Елисей | Миграции, транзакционная загрузка и аналитическая схема PostgreSQL. [#41](https://github.com/AlexKeyyyy/transport-connectivity/pull/41), [#47](https://github.com/AlexKeyyyy/transport-connectivity/pull/47) |
| Лина | Методика и код построения прореженного графа. [#40](https://github.com/AlexKeyyyy/transport-connectivity/pull/40) |
| Мадина | Поиск городов в Elasticsearch. [#48](https://github.com/AlexKeyyyy/transport-connectivity/pull/48) |
| Ранис | Офлайн-карта доступности и интерфейс демонстрации. [#49](https://github.com/AlexKeyyyy/transport-connectivity/pull/49) |

[Чек-лист финальной защиты](docs/defense-checklist.md)

## Как работаем

| Ветка | От чего | Куда вливается | Назначение |
|---|---|---|---|
| `main` | — | — | Выпуски с тегами |
| `develop` | — | — | Интеграция задач |
| `feature/<issue>-<short>` | `develop` | `develop` | Одна задача |
| `release/<version>` | `develop` | `main`, затем `develop` | Подготовка выпуска |
| `hotfix/<issue>-<short>` | `main` | `main` и `develop` | Срочное исправление выпуска |

Задача проходит проверки, рецензирование и squash merge через PR.
[Правила разработки и проверки](docs/contributing.md)

PDF планируем отправить 28–29 ноября. Финальная защита — 4 декабря 2026 года.