"""Interactive map of transport connectivity (#36)."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from html import escape

import folium
import streamlit as st
from psycopg import Error as DatabaseError
from streamlit_folium import st_folium

from transport_connectivity.app import data

LAYERS = {
    "best": "Лучший из трёх",
    "multimodal": "Лучший из трёх",
    "road": "Автомобиль",
    "rail": "Поезд",
    "air": "Самолёт",
}
THRESHOLDS = {3 * 3600: "3 часа", 6 * 3600: "6 часов", 24 * 3600: "24 часа"}
POPULATION_BREAKS = (1, 100_000, 1_000_000, 5_000_000)
POPULATION_COLORS = ("#e2e8f0", "#9ed8d6", "#47a9a9", "#187b83", "#074651")
SCENARIO_BREAKS = (-99_999, 0, 1, 100_001)
SCENARIO_COLORS = ("#aa3527", "#e29168", "#e2e8f0", "#62b8ae", "#08776c")
NO_VALUE = "#a1a8b3"


def format_value(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f}".replace(",", " ")


def tooltip_text(value: str) -> str:
    # Folium interpolates the tooltip HTML into a JavaScript template literal.
    return escape(value).replace("`", "&#96;").replace("$", "&#36;").replace("\\", "&#92;")


def draw_map(
    cities: list[data.City],
    selected: data.City | None,
    betweenness: dict[str, float | None],
    values: dict[str, float | None],
    reached: set[str],
    scenario_active: bool,
) -> None:
    overview = (61.0, 96.0)
    center = overview
    zoom = 3
    if selected and selected.latitude is not None and selected.longitude is not None:
        center = (selected.latitude, selected.longitude)
        zoom = 7
    map_view = folium.Map(location=overview, zoom_start=3, tiles="OpenStreetMap")
    markers = folium.FeatureGroup(name="Города")
    breaks, colors = (
        (SCENARIO_BREAKS, SCENARIO_COLORS)
        if scenario_active
        else (POPULATION_BREAKS, POPULATION_COLORS)
    )
    metric_label = "Изменение доступного населения" if scenario_active else "Доступное население"
    for city in sorted(cities, key=lambda city: city == selected):
        if city.latitude is None or city.longitude is None:
            continue
        city_id = city.city_id
        active = city == selected
        centrality = betweenness.get(city_id)
        centrality_label = "—" if centrality is None else f"{centrality:.3f}"
        value = values.get(city_id)
        fill = NO_VALUE if value is None else colors[bisect_right(breaks, value)]
        reachable = city_id in reached
        outline = "#d4770b" if active else "#213d50" if reachable else "#526779"
        folium.CircleMarker(
            location=(city.latitude, city.longitude),
            radius=4 + 12 * (centrality or 0) ** 0.5,
            color=outline,
            weight=3 if active or reachable else 1,
            fill=True,
            fill_color=fill,
            fill_opacity=0.95,
            tooltip=(
                f"<b>{tooltip_text(city.name)}</b><br>{tooltip_text(city.region)}"
                f"<br>Население: {format_value(city.population)}"
                f"<br>Посредничество: {centrality_label}"
                f"<br>{metric_label}: {format_value(value)}"
            ),
        ).add_to(markers)
    st_folium(
        map_view,
        key="transport-map",
        center=center,
        zoom=zoom,
        height=540,
        use_container_width=True,
        returned_objects=[],
        feature_group_to_add=markers,
    )


def show_legend(scenario_active: bool, has_centrality: bool) -> None:
    if scenario_active:
        title = "Изменение доступного населения"
        colors = SCENARIO_COLORS
        labels = ("≤ −100 000", "−99 999…−1", "0", "+1…+100 000", "> +100 000")
    else:
        title = "Доступное население"
        colors = POPULATION_COLORS
        labels = ("0", "< 100 тыс.", "100 тыс. – 1 млн", "1–5 млн", "≥ 5 млн")
    items = " · ".join(
        f'<span style="white-space: nowrap"><span style="color: {color}">●</span> {label}</span>'
        for color, label in zip((*colors, NO_VALUE), (*labels, "нет значения"), strict=True)
    )
    st.html(f'<p style="font-size: 0.875rem"><b>{title}</b> · {items}</p>')
    if has_centrality:
        st.caption("Размер точки показывает посредничество.")
    st.caption("Тёмная обводка отмечает достижимые города.")


def show_destinations(destinations: list[data.Reachable]) -> None:
    if not destinations:
        st.caption("В пределах порога доступных городов нет.")
        return
    rows = []
    for item in destinations:
        hours, minutes = divmod(int(item.travel_time_sec // 60), 60)
        rows.append({"Город": item.city.name, "В пути": f"{hours} ч {minutes:02d} мин"})
    st.caption(f"Доступно городов: {len(rows)}")
    st.dataframe(rows, hide_index=True, width="stretch", height=360)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=data.SOURCES, default="offline")
    source = parser.parse_args().source

    st.set_page_config(page_title="Транспортная связность городов", layout="wide")
    st.title("Транспортная связность городов")
    st.caption(
        "Доступность городов по оценкам времени в пути"
        if source == "offline"
        else "Доступность городов России, ключевые узлы и сценарии изменения сети"
    )
    try:
        show_map(source)
    except FileNotFoundError:
        st.error("Подготовьте файл доступности по инструкции docs/map.md.")
    except DatabaseError:
        st.error("Не удалось прочитать PostgreSQL. Проверьте подключение по docs/map.md.")


def show_map(source: str) -> None:
    cities = data.load_cities(source)
    scenarios = data.load_scenarios(source)
    available_layers = data.available_layers(source)
    if source == "db" and (not cities or "best" not in available_layers):
        st.info("Загрузите снимок и выполните команду accessibility по docs/map.md.")
        return
    warning = data.source_warning(source)
    if warning and source == "sample":
        st.warning(warning)
    elif warning:
        st.info(warning)

    scenario_column, layer_column, threshold_column = st.columns(3)
    scenario_index = scenario_column.selectbox(
        "Сценарий",
        options=range(len(scenarios) + 1),
        format_func=lambda i: "Исходная сеть" if i == 0 else scenarios[i - 1]["name"],
        disabled=not scenarios,
    )
    scenario = scenarios[scenario_index - 1] if scenario_index else None
    combined = "multimodal" if source == "sample" else "best"
    layer = layer_column.selectbox(
        "Транспорт",
        options=available_layers if scenario is None else (combined,),
        format_func=LAYERS.__getitem__,
        disabled=scenario is not None,
    )
    threshold = threshold_column.selectbox(
        "Порог времени", options=THRESHOLDS, format_func=THRESHOLDS.__getitem__
    )

    map_column, cities_column = st.columns([3, 1], gap="medium")
    cities_by_id = {city.city_id: city for city in cities}
    with cities_column:
        query = st.text_input("Поиск по названию") if source == "db" else ""
        matches, search_message = data.find_cities(query, cities)
        if search_message:
            st.info(search_message)
        if query.strip() and not matches:
            st.caption("Города по этому запросу не найдены.")
        selected_id = st.selectbox(
            "Найти город",
            options=[city.city_id for city in matches],
            index=0 if query.strip() and len(matches) == 1 else None,
            placeholder="Введите название",
            format_func=lambda city_id: (
                f"{cities_by_id[city_id].name} · {cities_by_id[city_id].region}"
            ),
        )
        selected = cities_by_id.get(selected_id)
        st.subheader("Куда доеду")
        destinations = []
        if selected is None:
            st.caption("Выберите город отправления.")
        else:
            if scenario:
                st.caption("Время в пути показано для исходной сети.")
            try:
                destinations = data.reachable_within(selected.city_id, layer, threshold, source)
            except data.ResultNotFound:
                st.info("Для этого города время в пути отсутствует в выбранном наборе.")
            else:
                show_destinations(destinations)

    metric = "scenario_reach_population_delta" if scenario else "reach_population"
    try:
        values = data.load_metric(
            metric, {"layer": layer, "threshold_sec": threshold}, scenario, source
        )
    except data.ResultNotFound:
        st.info("Для выбранного слоя и порога пока нет опубликованного расчёта.")
        values = {}
    try:
        betweenness = data.load_metric("betweenness", {"layer": combined}, source=source)
    except data.ResultNotFound:
        betweenness = {}
    reached = {item.city.city_id for item in destinations}
    with map_column:
        draw_map(cities, selected, betweenness, values, reached, scenario is not None)
        show_legend(scenario is not None, bool(betweenness))

    st.caption(
        "Источники данных · [Если быть точным]"
        "(https://tochno.st/datasets/intercity_connectivity) (CC BY 4.0) · "
        "[OpenStreetMap contributors](https://www.openstreetmap.org/copyright) · "
        "[Яндекс Расписания](https://rasp.yandex.ru/)"
    )


if __name__ == "__main__":
    main()
