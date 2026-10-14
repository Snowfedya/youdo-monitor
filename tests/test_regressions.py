"""Regression tests for bugs found during filter calibration.

Every case here is a real row from the frozen feed snapshot
(tests/fixtures_dataset.json, 105 webdevelopment tasks, 2026-09-30).
Keep these even if the rules change — they encode specific ways the
classifier silently produced wrong verdicts.
"""
import pytest

from youdo_monitor.filters import TaskClassifier


@pytest.fixture
def classifier():
    return TaskClassifier()


def _task(**kw):
    base = {
        "id": 1,
        "name": "",
        "category": "webdevelopment",
        "budget": "до 10 000",
        "offers_count": 5,
        "is_in_progress": False,
        "is_virtual": True,
        "description": "Разработка сайта на WordPress с нуля, адаптивная верстка.",
    }
    base.update(kw)
    return base


def test_bot_not_matched_inside_obrabotku(classifier):
    """REGRESSION: substring 'бот' matched inside 'обработку' and tagged a
    document-editing task as a Telegram bot job."""
    d = classifier.evaluate(_task(
        id=15273096,
        name="Внести корректировку в документ согласие на обработку",
        offers_count=14, budget="до 1 000",
        description="Внести небольшую корректировку в согласие об обработке пд и ещё 3 документа.",
    ))
    assert d.passed is False
    assert "bot" not in d.tags


def test_autocad_is_not_software_development(classifier):
    """REGRESSION: CAD drafting matched generic dev keywords and passed as a
    software task."""
    d = classifier.evaluate(_task(
        id=15272182,
        name="Разработать технические чертежи через AutoCAD",
        offers_count=3, budget="до 7 000",
        description="Необходимо сделать исполнительные чертежи через автокад. Нужны схема и эскиз.",
    ))
    assert d.passed is False
    assert d.reason == "non_software_task"


def test_esp32_hardware_job_rejected(classifier):
    """REGRESSION: an ESP-32 firmware job passed because 'разработчик' and
    'бэкенд' read as software keywords."""
    d = classifier.evaluate(_task(
        id=15271218,
        name="Разработчик ESP-32 (только Москва и Московская область)",
        offers_count=3, budget="до 7 000",
        description="Продолжить разработку устройства на основе ESP-32. Нужно сделать "
                    "подключение компрессора и вентилятора к ESP32.",
    ))
    assert d.passed is False
    assert d.reason == "non_software_task"


def test_card_filling_is_not_engineering(classifier):
    """REGRESSION: 'заполнить карточки товара' was accepted as a web task."""
    d = classifier.evaluate(_task(
        id=15270025, name="Заполнить карточки товара на сайте", offers_count=11,
        budget="до 7 000",
        description="Заполнить карточки товара на сайте, около 200 позиций. Контент предоставлю.",
    ))
    assert d.passed is False
    assert d.reason == "content_entry_task"


def test_dark_rp_server_is_not_web_development(classifier):
    """REGRESSION: a game-server admin job passed as web infrastructure."""
    d = classifier.evaluate(_task(
        id=15265873, name="Создание сервера Dark RP для гврисмод", offers_count=14,
        budget="до 1 000",
        description="Нужен человек, который настроит сервер Dark RP для Garry's Mod.",
    ))
    assert d.passed is False
    assert d.reason == "non_software_task"


def test_partner_search_is_not_an_order(classifier):
    """REGRESSION: 'ищу партнера-разработчика' is a staff search, not a task."""
    d = classifier.evaluate(_task(
        id=15257001, name="Ищу партнера-разработчика для создания wellness-платформы",
        offers_count=4, budget="до 100 000",
        description="Ищу партнёра-разработчика для создания wellness-платформы на постоянной основе.",
    ))
    assert d.passed is False
    assert d.reason == "hourly_or_vacancy"


def test_inflected_form_matches_with_word_boundaries(classifier):
    """REGRESSION: a bare \\b tail missed inflected forms, so 'на тильде' and
    'принтера' escaped their keyword rules."""
    d = classifier.evaluate(_task(
        id=15270746, name="Сделать сайт на тильде", offers_count=5,
        description="Нужен лендинг на Тильде для студии, адаптив под мобильные.",
    ))
    assert d.passed is False
    assert d.reason == "builder_not_supported"


def test_stem_matching_still_hits_inflections(classifier):
    """Guard the other direction: stems must still match inflected forms."""
    d = classifier.evaluate(_task(
        id=999, name="Доработка сайта на WordPress", offers_count=3,
        description="Требуются регулярные доработки сайта на WordPress.",
    ))
    assert d.passed is True
    assert "cms" in d.tags


def test_low_budget_placeholder_task_rejected(classifier):
    """REGRESSION: 'до 500 ₽ за отзыв' tasks passed and flooded notifications."""
    d = classifier.evaluate(_task(
        id=15274321, name="Сделать скрипт для ТГ", offers_count=3, budget="до  500",
        description="Сделать скрипт для ТГ, который ищет в сообщениях ключевые слова.",
    ))
    assert d.passed is False
    assert d.reason == "budget_too_low"
