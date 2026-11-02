# Skills для проекта

Подбор проверен 3 ноября 2026 года по исходным SKILL.md и списку openai/skills.
Это кандидаты для установки; в рамках уточнения архитектуры ничего не установлено.
Skills помогают вести работу, но не являются зависимостями приложения.

| Skill и источник | Для чего | Когда нужен |
|---|---|---|
| [supabase-postgres-best-practices](https://github.com/supabase/agent-skills/tree/main/skills/supabase-postgres-best-practices) | Схема PostgreSQL, SQL, индексы и диагностика | Проектирование и реализация БД; применим и без Supabase |
| [neo4j-modeling-skill](https://github.com/neo4j-contrib/neo4j-skills/tree/main/neo4j-modeling-skill) | Модель узлов/связей, ограничения и ошибки моделирования | Проектирование проекции Neo4j |
| [neo4j-gds-skill](https://github.com/neo4j-contrib/neo4j-skills/tree/main/neo4j-gds-skill) | GDS, проекции, алгоритмы и оценка памяти | После определения методики и подключения Neo4j/GDS |
| [networkx](https://github.com/K-Dense-AI/scientific-agent-skills/tree/main/skills/networkx) | Маленькие эталонные графы и проверка алгоритмов Python | При необходимости независимой проверки графовых результатов |
| [geopandas](https://github.com/K-Dense-AI/scientific-agent-skills/tree/main/skills/geopandas) | CRS, геометрии, spatial joins и геофайлы | Когда появятся реальные преобразования геометрий |
| [jupyter-notebook](https://github.com/openai/skills/tree/main/skills/.curated/jupyter-notebook) | Воспроизводимые исследовательские notebooks | Эксперименты и объяснение методики; основная логика остаётся в пакете |
| [gh-fix-ci](https://github.com/openai/skills/tree/main/skills/.curated/gh-fix-ci) | Разбор падений GitHub Actions | После подключения CI; требуется GitHub CLI (`gh`) |

## Как выбирать

Сначала полезны PostgreSQL и Neo4j modeling: они соответствуют текущей задаче #4.
GDS подключается на этапе аналитики. NetworkX и GeoPandas — сторонние кандидаты
K-Dense, а не официальные skills разработчиков этих библиотек; использовать только
при реальной задаче и сверять примеры с документацией установленной версии.
Не добавлять NetworkX/GeoPandas в приложение только ради skill.

В текущем окружении уже доступны встроенные skills для PDF, документов, таблиц и презентаций — их
достаточно для оформления отчёта; дублирующая установка не требуется.
Для CLI обработки достаточно правил проекта и обычных Python-инструментов.

## Установка и сопровождение

Для каждого выбранного skill проверить его актуальный SKILL.md, нужные references
и scripts; зафиксировать источник и commit при установке. Не переносить из него
чужие требования к инфраструктуре, версиям или подтверждениям механически.
Совместимость Neo4j/GDS и клиентских библиотек проверяется отдельно.

Skills из GitHub можно устанавливать через встроенный skill-installer, выбирая
конкретный каталог. Команде передавать один и тот же источник и commit.
Установка всего каталога научных skills для этого проекта не нужна.

Собственный skill для транспортной методики имеет смысл после её утверждения
и первых проверенных расчётов. Пока правила предметной области живут в документации.
