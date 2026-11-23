# Проверка полного прогона

Что запускалось, на каком коде и что получилось. Все JSON в этом каталоге — копии
файлов реальных запусков 15 ноября 2026 года; `quality-summary.json` собран из них скриптом.

## Код и окружение

- Commit: `637e053360b8e6e74687b81295f631e7867a5925` (ветка `feature/8-processor`, слита в `develop` как #24); дерево без локальных изменений.
- Python 3.12, Windows 11; зависимостей, кроме стандартной библиотеки, у загрузки и нормализации нет.

## Команды

```bash
python -m transport_connectivity.cli download             # загрузка из сети
python -m transport_connectivity.cli download --offline   # повтор из кэша без сети
python -m transport_connectivity.cli process              # прогон 1
python -m transport_connectivity.cli process              # прогон 2
python scripts/dev/check.py                               # проверки кода
```

## Результаты

| Проверка | Результат | Где видно |
|---|---|---|
| Набор выгружен | 96 660 записей × 22 поля, 180 городов; совпадает с опубликованным объёмом | [source-manifest.json](source-manifest.json) |
| Целостность входа | SHA-256 CSV `aed84633…16d53` совпадает в manifest и обоих прогонах | manifest, `input.sha256` отчётов |
| Повтор без сети | `download --offline` взял проверенный кэш | вывод команды: `Verified cached archive` |
| Баланс | 96 660 прочитано = 96 660 измерений + 0 отклонено | `balance` в [processing-run-1.json](processing-run-1.json) |
| Повторяемость | SHA-256 трёх выходных CSV совпали в двух прогонах | `output` в [processing-run-1.json](processing-run-1.json) и [processing-run-2.json](processing-run-2.json) |
| Качество идентификаторов | Пустых Sber ID, конфликтов ОКТМО и атрибутов нет | `identity`, [quality-summary.json](quality-summary.json) |
| Время и память | 21,8 с на прогон, пик 313 МБ (`tracemalloc`) | `service` отчётов |
| Тесты | 79 прошли, 1 пропущен: коннектор (10), обработчик (55), остальное — пакет и harness | `python scripts/dev/check.py` |
| Независимая проверка | Отдельный скрипт построчно сверил все 96 660 записей с выходом обработчика | рецензирование перед слиянием #24 |

## Что не проверялось

- Загрузка в PostgreSQL, граф, Neo4j и Elasticsearch — следующие этапы.
- Поведение на другой версии источника: обработчик рассчитан на контракт версии 20260629.
