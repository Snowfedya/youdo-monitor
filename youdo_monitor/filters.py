"""Task classification and filtering rules.

Design notes
------------
Matching is done on *word boundaries* via pre-compiled regexes, never on raw
substring containment. Substring matching produced false tags with a real
dataset: the keyword ``бот`` matched inside ``обработку``, and the stem
``постоянн`` matched unrelated words. Structured metadata (subcategory) is
preferred over free-text heuristics wherever the platform provides it.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Pattern
import re

from youdo_monitor.config import MAX_OFFERS_LIMIT, MIN_DESCRIPTION_LENGTH, CATEGORIES


def _rx(words: List[str], prefix: bool = True) -> Pattern[str]:
    """Compile a word-boundary alternation.

    The left lookbehind stops a stem matching inside an unrelated word
    ('бот' must not match 'обработку'). The ``\\w*`` right tail is ON by
    default because Russian inflection appends endings to the stem — a bare
    ``\\b`` tail silently misses 'тильде', 'принтера', 'карточки товаров'.
    Turning the tail off is only correct for words that must match exactly.
    """
    escaped = sorted((re.escape(w) for w in words), key=len, reverse=True)
    body = "|".join(escaped)
    tail = r"\w*" if prefix else r"\b"
    return re.compile(rf"(?<![а-яёa-z0-9])(?:{body}){tail}", re.IGNORECASE)


@dataclass
class Decision:
    passed: bool
    reason: str
    tags: List[str] = field(default_factory=list)


class TaskClassifier:
    """Deterministic classifier deciding whether a YouDo task fits this executor."""

    ALLOWED_CATEGORIES = set(CATEGORIES)

    # --- Hard rejections ---------------------------------------------------

    # Constructors with closed/read-only APIs: work is manual drag-and-drop.
    BUILDER_RE = _rx(["tilda", "тильд", "wix", "викс", "flexbe", "флексби",
                      "megagroup", "мегагрупп", "zero block", "zeroblock", "sigmaweb"])

    # Long-running hourly engagements and staff recruitment, not project orders.
    HOURLY_VACANCY_RE = _rx([
        "вакансия", "вакансии", "стажер", "стажёр", "колл-центр", "call-центр",
        "холодные звонки", "холодный обзвон", "прозвон", "оператор",
        "почасовая", "почасовой", "постоянная поддержка", "постоянное сотрудничество",
        "набор в команду", "в команду", "ищу команду", "ищу партнера", "ищу партнёра",
        "партнера-разработчика", "партнёра-разработчика",
        "часов в неделю", "часов в месяц", "в месяц", "трудоустройство",
    ])

    # Hardware, physical presence, CAD/EDA, and non-software artefacts.
    NON_SOFTWARE_RE = _rx([
        "сборка пк", "собрать пк", "системный блок", "ремонт компьютер", "материнск",
        "жесткий диск", "ssd", "роутер", "вай-фай", "wi-fi", "принтер", "мфу",
        "autocad", "автокад", "coreldraw", "coral draw", "корел", "solidworks",
        "компас-3d", "печатн", "плата", "esp-32", "esp32", "arduino", "ардуино",
        "пайка", "паять", "трассировка", "микроконтроллер", "чертеж", "чертёж",
        "электропитбайк", "самокат", "гврисмод", "dark rp", "гта",
        "физически", "приехать в офис", "выезд на объект",
    ])

    # Data-entry / content-filling: not engineering work.
    CONTENT_ENTRY_RE = _rx([
        "заполнить карточки", "заполнение карточек", "карточки товара", "карточки товаров",
        "вбить товары", "залить товары", "наполнить сайт", "наполнение сайта",
        "выгрузка товаров", "менеджер маркетплейс", "озон", "вайлдберриз", "wildberries",
    ])

    # Placeholder descriptions that hide the real scope ("в ЛС", "см. описание").
    HIDDEN_SCOPE_RE = _rx([
        "в лс", "в личку", "в личные сообщения", "подробности в телеграм",
        "подробнее в тг", "описание в лс", "тз в лс", "напишу в лс",
    ])

    # --- Positive domain signal -------------------------------------------

    POSITIVE_RULES = [
        (_rx(["wordpress", "вордпресс", "wp", "битрикс", "bitrix", "opencart", "joomla",
              "modx", "shopify", "prestashop", "django", "laravel", "open Server"]), "cms"),
        (_rx(["бот", "bot", "telegram", "телеграм", "тг", "макс", "чат-бот", "чатбот"]), "bot"),
        (_rx(["сайт", "лендинг", "лендинг", "интернет-магазин", "веб", "верстк", "верстка",
              "figma", "фигма", "макет", "адаптив", "одностраничник", "страниц"]), "web"),
        (_rx(["ошибк", "500", "502", "504", "php", "логи", "починить", "наладить",
              "исправить", "доработ", "правк", "баг", "bugfix", "обновить"]), "bugfix"),
        (_rx(["сервер", "vps", "vds", "linux", "ubuntu", "debian", "docker", "nginx",
              "apache", "хостинг", "домен", "dns", "ssl", "база данных", "mysql",
              "mariadb", "postgres", "перенос", "миграц", "развернуть"]), "infra"),
        (_rx(["api", "rest", "парсер", "парсинг", "скрап", "вебхук", "webhook",
              "crm", "интеграц", "скрипт", "python", "автоматизац", "1с"]), "dev"),
        (_rx(["приложение", "мобильн", "ios", "android", "react", "vue", "flutter"]), "app"),
    ]

    # Budgets at or below this value are "за отзыв" placeholder tasks.
    MIN_BUDGET_RUB = 1000

    # --- Evaluation --------------------------------------------------------

    def evaluate(self, task: Dict[str, Any]) -> Decision:
        # 0. Detail availability — a failed lookup says nothing about the task.
        # Without this gate an empty payload falls through to the description
        # check and is misreported as the client's problem.
        detail_state = task.get("detail_status")
        if detail_state not in (None, "ok"):
            return Decision(passed=False, reason="no_detail_data")

        # 1. Category gate (structured metadata)
        category = (task.get("category") or "").strip().lower()
        if category and category not in self.ALLOWED_CATEGORIES:
            return Decision(passed=False, reason="unsupported_category")

        # 2. Remote-only
        if task.get("is_virtual") is False:
            return Decision(passed=False, reason="not_virtual")

        # 3. Already assigned
        if task.get("is_in_progress", False):
            return Decision(passed=False, reason="already_in_progress")

        # 4. Offer slots exhausted
        offers = task.get("offers_count")
        if offers is not None and offers >= MAX_OFFERS_LIMIT:
            return Decision(passed=False, reason="offers_limit_reached")

        # 5. Budget floor — "до 500 ₽ за отзыв" tasks carry no real scope
        budget_num = self._parse_budget(task.get("budget"))
        if budget_num is not None and budget_num < self.MIN_BUDGET_RUB:
            return Decision(passed=False, reason="budget_too_low")

        # 6. Description sanity
        name = (task.get("name") or "").strip()
        description = (task.get("description") or "").strip()
        if len(description) < MIN_DESCRIPTION_LENGTH:
            return Decision(passed=False, reason="description_too_short")

        text = f"{name} {description}"

        # 7. Hard rejections, cheapest signal first
        if self.BUILDER_RE.search(text):
            return Decision(passed=False, reason="builder_not_supported")
        if self.CONTENT_ENTRY_RE.search(text):
            return Decision(passed=False, reason="content_entry_task")
        if self.NON_SOFTWARE_RE.search(text):
            return Decision(passed=False, reason="non_software_task")
        if self.HOURLY_VACANCY_RE.search(text):
            return Decision(passed=False, reason="hourly_or_vacancy")
        if self.HIDDEN_SCOPE_RE.search(text):
            return Decision(passed=False, reason="hidden_scope")

        # 8. Positive domain match
        tags: List[str] = []
        for rx, tag in self.POSITIVE_RULES:
            if rx.search(text) and tag not in tags:
                tags.append(tag)

        if not tags:
            return Decision(passed=False, reason="no_positive_match")

        return Decision(passed=True, reason="ok", tags=tags)

    @staticmethod
    def _parse_budget(budget: Optional[str]) -> Optional[int]:
        """Extract the numeric ceiling from strings like 'до 22 000' / '30 000 ₽'."""
        if not budget:
            return None
        digits = re.sub(r"[^\d]", "", budget.replace("\u00a0", ""))
        if not digits:
            return None
        try:
            return int(digits)
        except ValueError:
            return None
