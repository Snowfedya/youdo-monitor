"""Task scope analysis, price calculation, and personalized pitch generation."""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ScopeAnalysis:
    mode: str  # "green" or "rescope"
    detected_tech: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    domain: str = "web"
    is_ecommerce: bool = False
    is_migration: bool = False


TECH_KEYWORDS = [
    ("WordPress", [r"\bwordpress\b", r"\bвордпресс\b", r"\bwp\b"]),
    ("Joomla", [r"\bjoomla\b", r"\bджумл\w*"]),
    ("WooCommerce", [r"\bwoocommerce\b", r"\bвукоммерс\b"]),
    ("JoomShopping", [r"\bjoomshopping\b"]),
    ("SP Page Builder", [r"\bsp page builder\b", r"\bpage builder\b"]),
    ("Bitrix", [r"\bбитрикс\w*", r"\bbitrix\w*"]),
    ("OpenCart", [r"\bopencart\b", r"\bопенкарт\b"]),
    ("Shopify", [r"\bshopify\b", r"\bшопифай\b"]),
    ("Nginx", [r"\bnginx\b", r"\bнжинкс\b"]),
    ("Apache", [r"\bapache\b", r"\bапач\w*"]),
    ("Docker", [r"\bdocker\b", r"\bдокер\b", r"\bcompose\b"]),
    ("Linux / VPS", [r"\bvps\b", r"\bvds\b", r"\blinux\b", r"\bubuntu\b", r"\bdebian\b", r"\bсервер\w*"]),
    ("SSL / HTTPS", [r"\bssl\b", r"\bhttps\b", r"\bсертификат\w*"]),
    ("DNS / Домены", [r"\bdns\b", r"\bдомен\w*"]),
    ("Telegram Bot", [r"\btelegram\b", r"\bтелеграм\b", r"\bбот\w*", r"\bbot\b"]),
    ("Python", [r"\bpython\b", r"\bпайтон\b", r"\bпитон\b"]),
    ("PHP", [r"\bphp\b", r"\bпхп\b"]),
    ("MySQL / MariaDB", [r"\bmysql\b", r"\bmariadb\b", r"\bбаз\w* данн\w*"]),
]


def extract_budget_number(budget_str: Optional[str]) -> Optional[int]:
    """Extract numeric value from budget description (e.g. 'до 10 000' -> 10000)."""
    if not budget_str:
        return None
    cleaned = budget_str.replace("\u00a0", " ").replace(" ", "")
    digits = re.findall(r"\d+", cleaned)
    if digits:
        try:
            return int("".join(digits))
        except ValueError:
            return None
    return None


def calculate_offer_price(task: Dict[str, Any]) -> int:
    """Calculate offer price: 85% of budget, floor 1 500 ₽, capped by budget."""
    budget_raw = task.get("budget") or (task.get("price") or {}).get("OwnBudget")
    if isinstance(budget_raw, (int, float)):
        budget_num = int(budget_raw)
    else:
        budget_num = extract_budget_number(str(budget_raw) if budget_raw else None)

    if not budget_num or budget_num <= 0:
        return 2500

    calc = int(round(budget_num * 0.85))

    # Floor at 1500, but do not exceed budget if budget is smaller
    if calc < 1500:
        calc = 1500

    if calc > budget_num:
        calc = budget_num

    # Round to nearest 50 for clean appearance
    if calc >= 1000:
        calc = (calc // 50) * 50

    return calc


def analyze_task_scope(task: Dict[str, Any]) -> ScopeAnalysis:
    """Evaluate whether task fits standard budget or requires rescope / decomposition."""
    name = (task.get("name") or "").strip()
    desc = (task.get("description") or "").strip()
    text = f"{name} {desc}".lower()

    detected_tech: List[str] = []
    for tech_name, patterns in TECH_KEYWORDS:
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                if tech_name not in detected_tech:
                    detected_tech.append(tech_name)
                break

    budget_num = extract_budget_number(task.get("budget")) or 0

    risks: List[str] = []
    is_ecommerce = bool(re.search(r"магазин|joomshopping|woocommerce|товары|корзин|каталог", text))
    is_migration = bool(re.search(r"перенос|миграц|перейти с|перенести", text))
    has_builder = bool(re.search(r"builder|конструктор|elementor|дизайн|шаблон|адаптив", text))

    if is_ecommerce:
        risks.append("Интернет-магазин: требуется выгрузка базы товаров, категорий, клиентов и заказов")
    if has_builder:
        risks.append("Визуальный конструктор / редизайн: требуется ручная перевёрстка страниц")
    if is_migration and ("joomla" in text or "bitrix" in text) and "wordpress" in text:
        risks.append("Межплатформенная миграция: конвертация реляционных баз данных и сохранение SEO 301-редиректов")

    # High complexity vs low budget trigger
    rescope_triggered = False
    if is_ecommerce and budget_num < 25000:
        rescope_triggered = True
    elif is_migration and has_builder and budget_num < 20000:
        rescope_triggered = True
    elif len(risks) >= 2 and budget_num <= 15000:
        rescope_triggered = True

    domain = "web"
    tags = task.get("tags") or []
    if "infra" in tags or "vps" in text:
        domain = "infra"
    elif "bot" in tags or "telegram" in text:
        domain = "bot"
    elif "cms" in tags:
        domain = "cms"

    mode = "rescope" if rescope_triggered else "green"

    return ScopeAnalysis(
        mode=mode,
        detected_tech=detected_tech,
        risks=risks,
        domain=domain,
        is_ecommerce=is_ecommerce,
        is_migration=is_migration,
    )


def generate_pitch(task: Dict[str, Any], analysis: ScopeAnalysis, price: int) -> str:
    """Generate a high-converting, personalized proposal text."""
    name = (task.get("name") or "").strip()
    tech_str = ", ".join(analysis.detected_tech[:4]) if analysis.detected_tech else "веб-технологиями и серверами"

    if analysis.mode == "rescope":
        # Pitch with scope explanation & 2-stage breakdown
        lines = [
            f"Здравствуйте! Задача понятна: {name.lower() if name else 'выполнение проекта'}. "
            f"Регулярно работаю с {tech_str} и знаю все нюансы таких задач.",
            "",
            "Обратите внимание на скрытые сложности проекта:"
        ]
        for r in analysis.risks[:3]:
            lines.append(f"— {r};")

        lines.extend([
            "Реализовать весь этот объём «под ключ» в рамках указанного бюджета без потери качества и данных технически нереально.",
            "",
            "Предлагаю разбить работу на 2 прозрачных этапа:",
            f"1. Базовый этап (в рамках бюджета ~{price:,} ₽): детальный аудит, перенос/настройка основной структуры, контента и сохранение ссылок (SEO 301-редиректы).".replace(",", " "),
            "2. Функциональный этап: поэтапный перенос базы данных, каталога и дополнительных модулей.",
            "",
            "Готов провести первичный аудит и согласовать точный план действий. "
            "На связи постоянно, не пропадаю после оплаты. Гарантия на выполненную работу."
        ])
        return "\n".join(lines)

    # Standard GREEN pitch
    if analysis.domain == "infra":
        intro_role = "Ведущий инженер техподдержки хостинга, администрирование серверов и сетей — моя ежедневная работа."
        steps = (
            "1. Диагностика и проверка доступов к серверу.\n"
            "2. Настройка конфигураций, сервисов и безопасности (бэкапы перед изменениями).\n"
            "3. Тестирование под нагрузкой и совместная приёмка."
        )
    elif analysis.domain == "bot":
        intro_role = "Разработчик бэкенда и Telegram-ботов. Создаю надёжные решения с развёртыванием на сервере 24/7."
        steps = (
            "1. Согласование сценариев логики и структуры команд/кнопок.\n"
            "2. Разработка логики бота и подключение базы данных/API.\n"
            "3. Развёртывание на сервере, тестирование и передача исходников с инструкцией."
        )
    else:
        intro_role = "Ведущий инженер техподдержки хостинга, регулярно работаю с сайтами, CMS, базами данных и доработками функционала."
        steps = (
            "1. Анализ текущего сайта и согласование точного списка правок.\n"
            "2. Аккуратное внесение изменений (с предварительным бэкапом файлов и БД).\n"
            "3. Совместная проверка результата на всех устройствах."
        )

    lines = [
        f"Здравствуйте! {intro_role}",
        f"Стек по вашей задаче ({tech_str}) отлично знаком.",
        "",
        "Порядок работы:",
        steps,
        "",
        "Готов приступить после уточнения деталей. На связи постоянно, не пропадаю после оплаты. Гарантия на результат."
    ]
    return "\n".join(lines)
