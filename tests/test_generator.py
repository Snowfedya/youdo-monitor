"""Tests for task scope analysis, price calculation, and personalized pitch generation."""
import pytest
from youdo_monitor.generator import (
    calculate_offer_price,
    analyze_task_scope,
    generate_pitch,
    ScopeAnalysis,
)


def test_calculate_offer_price_standard():
    # 85% of 10 000 = 8 500
    assert calculate_offer_price({"budget": "до 10 000"}) == 8500
    # 85% of 70 000 = 59 500
    assert calculate_offer_price({"budget": "70 000 ₽"}) == 59500


def test_calculate_offer_price_floor():
    # 85% of 1 500 = 1 275 -> floor 1 500, but capped by budget 1 500 -> 1500
    assert calculate_offer_price({"budget": "до 1 500"}) == 1500
    # 85% of 1 700 = 1 445 -> floor 1 500 -> 1500
    assert calculate_offer_price({"budget": "1 700"}) == 1500
    # Small budget below 1 500: cap at budget
    assert calculate_offer_price({"budget": "до 1 000"}) == 1000
    # No budget: default 2 500
    assert calculate_offer_price({"budget": None}) == 2500


def test_analyze_task_scope_green():
    task = {
        "id": 15286903,
        "name": "Настроить VPS сервер и Nginx",
        "description": "Нужно настроить VPS на Ubuntu 22.04, поднять Nginx, настроить SSL и Docker контейнеры.",
        "budget": "до 7 000",
        "tags": ["infra"]
    }
    analysis = analyze_task_scope(task)
    assert isinstance(analysis, ScopeAnalysis)
    assert analysis.mode == "green"
    assert "nginx" in [t.lower() for t in analysis.detected_tech]
    assert "docker" in [t.lower() for t in analysis.detected_tech]


def test_analyze_task_scope_rescope():
    task = {
        "id": 15290715,
        "name": "Перенос Joomla на WordPress",
        "description": (
            "Задача: Joomla 3 перенести на WordPress. "
            "Перенести контент и функционал с сохранением контента и ссылок. "
            "Основные компоненты: AcyMailing, JoomGallery, JoomShopping, Phoca Guestbook, RSForm! Pro, SP Page Builder. "
            "Улучшить дизайн сайта под современные реалии."
        ),
        "budget": "до 10 000",
        "tags": ["cms", "web"]
    }
    analysis = analyze_task_scope(task)
    assert analysis.mode == "rescope"
    assert len(analysis.risks) >= 2
    # Should detect e-commerce or complex modules
    assert any("joomshopping" in r.lower() or "магазин" in r.lower() or "модул" in r.lower() for r in analysis.risks)


def test_generate_pitch_green():
    task = {
        "id": 15286903,
        "name": "Настроить VPS сервер и Nginx",
        "description": "Нужно настроить VPS на Ubuntu, поднять Nginx и выпустить SSL сертификат.",
        "budget": "до 5 000",
        "tags": ["infra"]
    }
    analysis = analyze_task_scope(task)
    price = calculate_offer_price(task)
    pitch = generate_pitch(task, analysis, price)

    assert "Здравствуйте!" in pitch
    # Must contain trust signals & core background
    assert "инженер" in pitch.lower() or "хостинг" in pitch.lower() or "сервер" in pitch.lower()
    # Must contain anti-fear guarantee
    assert "не пропадаю" in pitch.lower() or "гарантия" in pitch.lower()
    # Must mention relevant tech
    assert "nginx" in pitch.lower() or "vps" in pitch.lower()


def test_generate_pitch_rescope():
    task = {
        "id": 15290715,
        "name": "Перенос Joomla на WordPress",
        "description": (
            "Joomla 3 на WordPress. Перенести контент. "
            "Компоненты: JoomShopping, SP Page Builder, RSForm! Pro. Дизайн и адаптивность."
        ),
        "budget": "до 10 000",
        "tags": ["cms"]
    }
    analysis = analyze_task_scope(task)
    price = calculate_offer_price(task)
    pitch = generate_pitch(task, analysis, price)

    assert "Здравствуйте!" in pitch
    # Rescope pitch must suggest breaking down into stages
    assert "этап" in pitch.lower() or "базовый перенос" in pitch.lower()
    # Must mention bottlenecks
    assert "joomla" in pitch.lower() or "wordpress" in pitch.lower()
    assert "не пропадаю" in pitch.lower() or "гарантия" in pitch.lower()
