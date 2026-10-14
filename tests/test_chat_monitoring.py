"""Tests for YouDo chat message monitoring and notification deduplication."""
import json
from unittest.mock import MagicMock, patch

from youdo_monitor.state import StateManager
from youdo_monitor.notifier import Notifier


def test_chat_message_state_dedup(tmp_path):
    sm = StateManager(tmp_path / "state.json")
    msg_id = 161599347
    
    assert sm.is_chat_message_seen(msg_id) is False
    sm.mark_chat_message_seen(msg_id, extra={"sender": "Анна Ч."})
    assert sm.is_chat_message_seen(msg_id) is True
    
    # Check persistence
    sm.save()
    reloaded = StateManager(tmp_path / "state.json")
    assert reloaded.is_chat_message_seen(msg_id) is True
    assert reloaded.state["seen_chat_messages"][str(msg_id)]["extra"]["sender"] == "Анна Ч."


def test_chat_message_prune(tmp_path):
    sm = StateManager(tmp_path / "state.json", ttl_days=14)
    sm.mark_chat_message_seen(101)
    sm.state.setdefault("seen_chat_messages", {})["102"] = {"seen_at": 1.0}
    
    sm.prune_expired()
    seen = sm.state["seen_chat_messages"]
    assert "101" in seen
    assert "102" not in seen


def test_format_chat_card():
    msg = {
        "id": 161599347,
        "sender_name": "Анна Ч.",
        "sender_id": 14316146,
        "text": "Здравствуйте \nНапишите ваш тг",
        "date": "2026-10-06T01:19:13.533+00:00",
        "task_title": "Создать бота для telegram",
    }
    card = Notifier.format_chat_card(msg)
    assert "💬 Новое сообщение в чате YouDo" in card
    assert "Анна Ч." in card
    assert "Напишите ваш тг" in card
    assert "https://youdo.com" in card


def test_send_chat_messages():
    notifier = Notifier(dry_run=True)
    msgs = [
        {
            "id": 161599347,
            "sender_name": "Анна Ч.",
            "text": "Здравствуйте \nНапишите ваш тг",
        }
    ]
    delivered = notifier.send_chat_messages(msgs)
    assert delivered == 1


def test_send_chat_messages_reports_failure():
    """A failed send must be visible, not silently counted as delivered."""
    notifier = Notifier(dry_run=False)
    notifier.send_raw = lambda text: False
    delivered = notifier.send_chat_messages([{"id": 1, "text": "x"}])
    assert delivered == 0


def test_format_chat_card_without_task_uses_sender_link():
    card = Notifier.format_chat_card(
        {"id": 2, "sender_name": "Анна Ч.", "sender_id": 14316146, "text": "тест"}
    )
    assert "https://youdo.com/u14316146" in card


def test_format_chat_card_links_task_when_known():
    card = Notifier.format_chat_card(
        {"id": 3, "sender_name": "Анна Ч.", "text": "тест", "task_id": 15290773}
    )
    assert "https://youdo.com/t15290773" in card
