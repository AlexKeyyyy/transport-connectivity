# Скрипты

Из корня репозитория, в Python 3.12+ окружении:

```bash
python scripts/dev/check.py
```

Общая проверка без сети запускает проверку локальных ссылок Markdown, Ruff и pytest.
Установите зависимости командой `python -m pip install -e ".[dev]"`.

```bash
python scripts/dev/check_docs.py
```

Проверка локальных ссылок работает на стандартной библиотеке. Обе команды
поддерживают `--help`.

Скрипты `data/download_samples.py` и `data/profile_samples.py` получают и проверяют
пилот; их сетевые запросы и настройки описаны в [data/README.md](../data/README.md).
Общая проверка не обращается к внешним источникам. Запуск на полном снимке описан в [README проекта](../README.md).
