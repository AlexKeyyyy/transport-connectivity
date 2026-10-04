# Transport Connectivity

Анализ транспортной связности городов России на основе графовых методов.

Учебный проект (M0 — Данные и архитектура). Цель M0: заложить основу репозитория,
определить архитектуру, получить первый реальный срез данных и подготовить
материалы для преподавателя. Финальный продукт на этом этапе не строится.

## Источники данных

- OpenStreetMap (PBF-выгрузки — основной источник геоданных);
- Overpass API — только небольшие выборочные запросы;
- OSRM — расчёт автомобильных маршрутов, времени и расстояний;
- API Яндекс Расписаний — данные о междугороднем транспорте.

## Стек (план)

Python 3.12+, PostgreSQL + PostGIS (source of truth), Neo4j (графовая проекция),
Elasticsearch (поисковый индекс), OSRM, FastAPI, Streamlit (позже),
Docker Compose, pytest, Ruff.

> Архитектурный принцип: Neo4j и Elasticsearch — производные представления,
> восстанавливаемые из PostgreSQL. Независимыми источниками истины не являются.

## Структура

```text
src/transport_connectivity/  — исходный код пакета
tests/                       — тесты (pytest)
scripts/                     — вспомогательные скрипты
notebooks/                   — эксперименты и разбор данных
data/raw/                    — сырые данные (не коммитятся, см. data/README.md)
data/processed/              — обработанные данные (не коммитятся)
data/samples/                — небольшие демо-файлы для преподавателя
docs/                        — архитектура, модель данных, ограничения
.github/                     — шаблоны Issue и PR
```

## Быстрый старт

```bash
py -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # Linux/macOS
py -m pip install -e ".[dev]"
py -m pytest
py -m ruff check src tests
```

Требуется Python 3.12 (см. `.python-version`).
Скопируйте `.env.example` в `.env` для локальной настройки, секреты не коммитятся.

## Workflow

`Issue → отдельная ветка → реализация → Pull Request → review → squash merge в main`.
Прямая работа в `main` запрещена. Имена веток: `feat/<issue>-<name>`,
`fix/<issue>-<name>`, `docs/<issue>-<name>`, `research/<issue>-<name>`.

## Документация

- `docs/architecture.md` — текущие архитектурные принципы (M0);
- `docs/README.md` — указатель планируемых документов;
- `data/README.md` — политика работы с данными;
- `AGENTS.md` — правила для coding-агентов.
