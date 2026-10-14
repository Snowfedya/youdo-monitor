"""Unit tests for task classifier and filter rules."""
import pytest
from youdo_monitor.filters import TaskClassifier, Decision


@pytest.fixture
def classifier():
    return TaskClassifier()


def test_figma_to_wordpress_accepted(classifier):
    task = {
        "id": 15264606,
        "name": "Перенос дизайн сайта",
        "category": "webdevelopment",
        "budget": "до 30 000 ₽",
        "offers_count": 9,
        "is_in_progress": False,
        "is_virtual": True,
        "description": "Перенос дизайна сайта из Figma на платформу WordPress. Адаптивная верстка, формы обратной связи, возможность редактировать контент."
    }
    decision = classifier.evaluate(task)
    assert decision.passed is True
    assert "wordpress" in decision.tags or "web" in decision.tags


def test_telegram_bot_accepted(classifier):
    task = {
        "id": 15272036,
        "name": "Чат-бот в ТГ и МАКС",
        "category": "webdevelopment",
        "budget": "до 10 000 ₽",
        "offers_count": 14,
        "is_in_progress": False,
        "is_virtual": True,
        "description": "Разработать чат-бота для приема заказов сети ресторанов доставки в Telegram и интеграция с CRM через вебхуки."
    }
    decision = classifier.evaluate(task)
    assert decision.passed is True
    assert "bot" in decision.tags or "telegram" in decision.tags


def test_wp_bugfix_accepted(classifier):
    task = {
        "id": 15273447,
        "name": "Наладить работу сайта на WordPress",
        "category": "webdevelopment",
        "budget": "до 1 000 ₽",
        "offers_count": 14,
        "is_in_progress": False,
        "is_virtual": True,
        "description": "На сайте появилась ошибка 500 после обновления плагинов. Нужно разобраться в логах PHP и восстановить работоспособность."
    }
    decision = classifier.evaluate(task)
    assert decision.passed is True
    assert "wordpress" in decision.tags or "bugfix" in decision.tags


def test_offers_limit_rejected(classifier):
    task = {
        "id": 15272734,
        "name": "Сайт интернет магазина Wordpress",
        "category": "webdevelopment",
        "budget": "30 000 ₽",
        "offers_count": 30,
        "is_in_progress": False,
        "is_virtual": True,
        "description": "Создание интернет магазина под ключ."
    }
    decision = classifier.evaluate(task)
    assert decision.passed is False
    assert decision.reason == "offers_limit_reached"


def test_in_progress_rejected(classifier):
    task = {
        "id": 15270127,
        "name": "Услуга программирования",
        "category": "webdevelopment",
        "budget": "29 900 ₽",
        "offers_count": 12,
        "is_in_progress": True,
        "is_virtual": True,
        "description": "Сайт для строительной компании на 10-15 страниц."
    }
    decision = classifier.evaluate(task)
    assert decision.passed is False
    assert decision.reason == "already_in_progress"


def test_not_virtual_rejected(classifier):
    task = {
        "id": 15270999,
        "name": "Настройка сервера",
        "category": "webdevelopment",
        "budget": "5 000 ₽",
        "offers_count": 2,
        "is_in_progress": False,
        "is_virtual": False,
        "description": "Приехать в офис в Москве и физически настроить сервер."
    }
    decision = classifier.evaluate(task)
    assert decision.passed is False
    assert decision.reason == "not_virtual"


def test_tilda_builder_rejected(classifier):
    task = {
        "id": 15270746,
        "name": "Сделать сайт на тильде",
        "category": "webdevelopment",
        "budget": "14 000 ₽",
        "offers_count": 5,
        "is_in_progress": False,
        "is_virtual": True,
        "description": "Нужно собрать лендинг на Tilda с Zero Block и красивой адаптивной анимацией."
    }
    decision = classifier.evaluate(task)
    assert decision.passed is False
    assert "builder_not_supported" in decision.reason or "negative_keyword" in decision.reason


def test_hidden_spec_rejected(classifier):
    task = {
        "id": 15269932,
        "name": "Написать софт",
        "category": "webdevelopment",
        "budget": "14 000 ₽",
        "offers_count": 16,
        "is_in_progress": False,
        "is_virtual": True,
        "description": "Лс"
    }
    decision = classifier.evaluate(task)
    assert decision.passed is False
    assert "description_too_short" in decision.reason or "no_positive_match" in decision.reason


def test_garbage_content_rejected(classifier):
    task = {
        "id": 15270025,
        "name": "Карточки товара на сайте",
        "category": "webdevelopment",
        "budget": "7 000 ₽",
        "offers_count": 10,
        "is_in_progress": False,
        "is_virtual": True,
        "description": "Хочу эелектропитбайк"
    }
    decision = classifier.evaluate(task)
    assert decision.passed is False
    assert decision.reason == "content_entry_task"


def test_hourly_support_rejected(classifier):
    task = {
        "id": 15271870,
        "name": "Доработка Shopify",
        "category": "webdevelopment",
        "budget": "7 000 ₽",
        "offers_count": 9,
        "is_in_progress": False,
        "is_virtual": True,
        "description": "Требуется постоянная почасовая поддержка 3 интернет-магазинов на Shopify, 20 часов в неделю."
    }
    decision = classifier.evaluate(task)
    assert decision.passed is False
    assert decision.reason == "hourly_or_vacancy"


def test_unsupported_category_rejected(classifier):
    task = {
        "id": 15270262,
        "name": "Собрать системный блок",
        "category": "computerhelp",
        "budget": "20 000 ₽",
        "offers_count": 3,
        "is_in_progress": False,
        "is_virtual": True,
        "description": "Сборка ПК из комплектующих."
    }
    decision = classifier.evaluate(task)
    assert decision.passed is False
    assert decision.reason == "unsupported_category"
