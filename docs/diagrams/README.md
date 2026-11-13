# Диаграммы

| Диаграмма | Исходник | Изображение | Описание |
|---|---|---|---|
| Модель данных (ER) | [data-model.mmd](data-model.mmd) | [data-model.svg](data-model.svg) | [Модель данных](../data-model.md) |
| Архитектура и потоки | [architecture.mmd](architecture.mmd) | [architecture.svg](architecture.svg) | [Архитектура](../architecture.md) |

Исходник — текст Mermaid; SVG получается только из него и вручную не правится.
После изменения `.mmd` изображение пересобирается в том же коммите.

## Как пересобрать

Нужен Node.js 18+; при первом запуске mermaid-cli скачивает Chromium. Из корня репозитория:

```bash
npx -y @mermaid-js/mermaid-cli@11.17.1 -i docs/diagrams/data-model.mmd -o docs/diagrams/data-model.svg
npx -y @mermaid-js/mermaid-cli@11.17.1 -i docs/diagrams/architecture.mmd -o docs/diagrams/architecture.svg
```

Версия закреплена, чтобы повторная сборка давала ту же раскладку.

## Обозначения

- ER: `PK` — первичный ключ, `FK` — внешний ключ, `UK` — уникальность; `||--o{` —
  «один ко многим», `|o--o{` — необязательная связь, `||--|{` — «один к одному или многим».
  Сущности с пометкой «проект» появятся после решения связанных задач.
- Архитектура: сплошная стрелка — поток данных, пунктир — запуск этапа (поток управления),
  цилиндр — хранилище.
