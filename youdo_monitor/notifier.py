"""Notification delivery for matched YouDo tasks."""
import logging
import subprocess
from typing import Any, Dict, List

from youdo_monitor.config import TELEGRAM_TARGET

logger = logging.getLogger(__name__)


class Notifier:
    """Formats and delivers task cards via the Hermes messaging bridge (`hermes send`)."""

    def __init__(self, target: str = TELEGRAM_TARGET, dry_run: bool = False):
        self.target = target
        self.dry_run = dry_run
        self.sent_count = 0
        self.failed_count = 0

    @staticmethod
    def format_card(task: Dict[str, Any]) -> str:
        """Render a single task as a compact Telegram card."""
        tid = task.get("id")
        name = (task.get("name") or "Без названия").strip()
        budget = (task.get("budget") or "не указан").strip()
        offers = task.get("offers_count")
        offers_txt = f"{offers}/30" if offers is not None else "?"
        cat = task.get("subcategory_name") or task.get("category_name") or task.get("category") or "—"
        desc = " ".join((task.get("description") or "").split())
        if len(desc) > 420:
            desc = desc[:417] + "..."

        lines = [
            f"🎯 Новый заказ YouDo: {name}",
            f"💰 Бюджет: {budget} | Откликов: {offers_txt}",
            f"🏷 {cat}",
        ]
        if task.get("is_b2b"):
            lines.append("🏢 Бизнес-задание")
        if task.get("tags"):
            lines.append(f"🔖 {', '.join(task['tags'])}")
        if desc:
            lines.append(f"📝 {desc}")

        offer_info = task.get("auto_offer")
        if offer_info:
            if offer_info.get("success"):
                mode_icon = "🟢 прямо" if offer_info.get("mode") == "green" else "🟡 рескоуп"
                price = offer_info.get("price")
                lines.append(f"⚡ Автоотклик отправлен: постоплата 0 ₽ | ставка {price} ₽ | {mode_icon}")
                pitch = offer_info.get("pitch")
                if pitch:
                    short_pitch = pitch if len(pitch) <= 300 else pitch[:297] + "..."
                    lines.append(f"💬 Отклик:\n«{short_pitch}»")
            else:
                reason = offer_info.get("reason") or offer_info.get("error")
                if reason == "postpayment_unavailable":
                    lines.append("⚠️ Автоотклик пропущен: доступен только платный отклик")
                elif reason == "already_offered":
                    lines.append("ℹ️ Автоотклик пропущен: отклик уже был подан ранее")
                else:
                    lines.append(f"⚠️ Автоотклик не отправлен: {reason}")

        lines.append(f"🔗 https://youdo.com/t{tid}")
        return "\n".join(lines)

    @staticmethod
    def format_chat_card(msg: Dict[str, Any]) -> str:
        """Render one incoming chat message as a Telegram card.

        Chat text is a live negotiation, so the card keeps the whole message
        (up to a hard cap) and links to the task the dialog belongs to, when
        the payload carries one.
        """
        sender = (msg.get("sender_name") or "Неизвестный отправитель").strip()
        text = (msg.get("text") or "").strip()
        # A chat bubble has no budget field to justify trimming; only the
        # pathological case (an auto-inserted wall of text) gets clipped.
        if len(text) > 1500:
            text = text[:1497] + "..."

        lines = [f"💬 Новое сообщение в чате YouDo от {sender}"]
        task_title = msg.get("task_title")
        if task_title:
            lines.append(f"🏷 Задание: {task_title}")
        lines.append(f"💬 {text}" if text else "💬 (сообщение без текста)")
        task_id = msg.get("task_id")
        if task_id:
            lines.append(f"🔗 https://youdo.com/t{task_id}")
        else:
            lines.append(f"🔗 https://youdo.com/u{msg.get('sender_id')}")
        return "\n".join(lines)

    def send_raw(self, text: str) -> bool:
        """Send a raw text message through `hermes send`."""
        if self.dry_run:
            logger.info("[DRY-RUN] Would send:\n%s", text)
            print(text)
            print("-" * 60)
            return True
        try:
            proc = subprocess.run(
                ["hermes", "send", "-t", self.target, text],
                capture_output=True, text=True, timeout=60
            )
            if proc.returncode != 0:
                logger.error("hermes send failed (rc=%d): %s", proc.returncode, proc.stderr.strip()[:300])
                self.failed_count += 1
                return False
            self.sent_count += 1
            return True
        except subprocess.TimeoutExpired:
            logger.error("hermes send timed out")
            self.failed_count += 1
            return False
        except FileNotFoundError:
            logger.error("`hermes` binary not found in PATH")
            self.failed_count += 1
            return False

    def send_tasks(self, tasks: List[Dict[str, Any]]) -> int:
        """Send one message per matched task. Returns count of successfully delivered."""
        delivered = 0
        for task in tasks:
            if self.send_raw(self.format_card(task)):
                delivered += 1
        return delivered

    def send_chat_messages(self, msgs: List[Dict[str, Any]]) -> int:
        """Send one card per incoming chat message. Returns delivered count."""
        delivered = 0
        for msg in msgs:
            if self.send_raw(self.format_chat_card(msg)):
                delivered += 1
        return delivered

    def send_status(self, text: str) -> bool:
        """Send a service/status message (errors, auth required, etc.)."""
        return self.send_raw(f"⚙️ YouDo Monitor\n{text}")
