# Промежуточная сдача

С чего начать проверку промежуточного этапа.

[Материалы финальной защиты](../predefense/README.md) включают рабочие PDF и сценарий демонстрации.

1. [Промежуточный отчёт](../interim-report.md) — что сделано, чем подтверждено, кто что делал и что дальше.
2. [Пакет M0](../m0/README.md) — данные, модель, ограничения и поиск.
3. Доказательства полного прогона — в [`evidence/`](evidence/):

| Файл | Что это |
|---|---|
| [`source-manifest.json`](evidence/source-manifest.json) | Manifest реальной загрузки полного набора: URL, версия, SHA-256, объём, колонки |
| [`processing-run-1.json`](evidence/processing-run-1.json), [`processing-run-2.json`](evidence/processing-run-2.json) | Отчёты двух полных прогонов нормализации |
| [`quality-summary.json`](evidence/quality-summary.json) | Сводка качества, собранная из этих отчётов |
| [`verification.md`](evidence/verification.md) | Команды, commit и результаты проверок |

Большие файлы (исходный CSV, нормализованные таблицы) в Git не хранятся: они
воспроизводятся командами из [data/README.md](../../data/README.md).
