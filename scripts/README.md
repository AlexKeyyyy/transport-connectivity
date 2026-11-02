# Скрипты

Из корня репозитория, в Python 3.12+ окружении:

```bash
python scripts/dev/check.py
```

Общая offline-проверка запускает harness checks, Ruff lint/format и pytest. Предварительно
нужны dev-зависимости из `python -m pip install -e ".[dev]"`.

```bash
python scripts/dev/check_harness.py
```

Проверяет установленный набор skills, hash/provenance и локальные ссылки;
использует только стандартную библиотеку. Обе команды поддерживают `--help`.

Скрипты `data/download_samples.py` и `data/profile_samples.py` получают и проверяют
пилот; их сетевые запросы и настройки описаны в [data/README.md](../data/README.md).
Общая проверка их не запускает. Полный датасет и обработчик реализуются в #7/#8.
