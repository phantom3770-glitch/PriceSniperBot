"""
chart_service.py — генератор графиков истории цен через QuickChart API.
"""

import json
from datetime import datetime
from urllib.parse import quote


def format_date_label(date_str: str) -> str:
    """
    Преобразует строку даты (ISO или YYYY-MM-DD HH:MM:SS) в формат DD.MM.
    """
    if not date_str:
        return ""
    
    # Убираем символы часового пояса для парсинга
    clean_str = str(date_str).replace("Z", "+00:00")
    
    try:
        dt = datetime.fromisoformat(clean_str)
        return dt.strftime("%d.%m")
    except Exception:
        pass

    try:
        dt = datetime.strptime(clean_str[:19], "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d.%m")
    except Exception:
        pass

    try:
        dt = datetime.strptime(clean_str[:10], "%Y-%m-%d")
        return dt.strftime("%d.%m")
    except Exception:
        pass

    return clean_str[:5]


def generate_chart_url(history_data: list[tuple[str, float]]) -> str:
    """
    Формирует URL динамического графика истории цен с использованием QuickChart API.
    history_data: список кортежей [(date_str, price), ...]
    """
    labels = [format_date_label(item[0]) for item in history_data]
    prices = [float(item[1]) for item in history_data]

    chart_config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": "Цена",
                    "data": prices,
                    "borderColor": "#22c55e",
                    "backgroundColor": "rgba(34, 197, 94, 0.15)",
                    "fill": True,
                    "tension": 0.3,
                    "pointRadius": 4,
                    "pointBackgroundColor": "#22c55e",
                    "pointBorderColor": "#ffffff",
                    "pointBorderWidth": 1.5,
                }
            ],
        },
        "options": {
            "legend": {"display": False},
            "title": {
                "display": True,
                "text": "Динамика изменения цены",
                "fontSize": 16,
                "fontColor": "#1e293b",
            },
            "scales": {
                "yAxes": [
                    {
                        "ticks": {"beginAtZero": False},
                        "scaleLabel": {
                            "display": True,
                            "labelString": "Цена",
                        },
                    }
                ]
            },
        },
    }

    config_str = json.dumps(chart_config, ensure_ascii=False)
    encoded_config = quote(config_str)
    return f"https://quickchart.io/chart?c={encoded_config}&w=600&h=400&bkg=white&devicePixelRatio=2"
