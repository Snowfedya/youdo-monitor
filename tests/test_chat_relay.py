"""Tests for the chat-message relay path and the two pure parsers it depends on.

The runner tests use STRICT fakes: any attribute the production code reaches for
but the fake does not declare raises immediately instead of returning a Mock, so
a severed chain fails loudly rather than passing on auto-generated attributes.
"""
import json
import time

import pytest

from youdo_monitor import runner as runner_mod
from youdo_monitor.runner import run_once
from youdo_monitor.state import StateManager
from youdo_monitor.client import parse_private_messages, notification_dedup_key


# --- real payload shape, captured from the live account on 2026-10-06 --------

REAL_PRIVATE_MESSAGES = {
    "ResultObject": {
        "InformationMessages": [
            {
                "Id": 18102277,
                "Text": "Рекомендуем не переводить общение в сторонние мессенджеры",
            }
        ],
        "Messages": [
            {
                "Id": 161599347,
                "UserId": 14316146,
                "RecipientId": 4645800,
                "Date": "2026-10-06T01:19:13.533+00:00",
                "Text": "Здравствуйте \nНапишите ваш тг",
            },
            {
                "Id": 161601759,
                "UserId": 4645800,
                "RecipientId": 14316146,
                "Date": "2026-10-06T06:46:56.027+00:00",
                "Text": "Здравствуйте, @MarkFrost47",
            },
        ],
        "Sender": {"Id": 4645800, "UserName": "Фёдор П."},
        "Recipient": {"Id": 14316146, "UserName": "Анна Ч."},
    },
    "IsSuccess": True,
}

PEER = REAL_PRIVATE_MESSAGES["ResultObject"]["Recipient"]


# --- pure parsers ------------------------------------------------------------

def test_parse_keeps_only_the_peer_authored_message():
    """Incoming = authored by the peer. Our own reply must not come back."""
    msgs = parse_private_messages(REAL_PRIVATE_MESSAGES, PEER)
    assert [m["id"] for m in msgs] == [161599347]
    assert msgs[0]["sender_name"] == "Анна Ч."
    assert msgs[0]["text"] == "Здравствуйте \nНапишите ваш тг"


def test_parse_drops_service_notices_and_blanks():
    payload = {
        "ResultObject": {
            "Messages": [
                {"Id": 1, "UserId": 14316146, "Text": "   "},
                {"Id": "not-a-number", "UserId": 14316146, "Text": "x"},
                {"Id": 0, "UserId": 14316146, "Text": "x"},
                {"Id": 5, "UserId": 14316146, "Text": "живое"},
            ]
        }
    }
    assert [m["id"] for m in parse_private_messages(payload, PEER)] == [5]


def test_parse_survives_empty_payload():
    assert parse_private_messages({}, PEER) == []
    assert parse_private_messages({"ResultObject": None}, PEER) == []


def test_dedup_key_uses_permanent_id_not_relative_time():
    """The defect: keying on '26 мин. назад' made every cycle re-forward."""
    a = notification_dedup_key({"id": 137161933, "when": "26 мин. назад", "text": "Анна"})
    b = notification_dedup_key({"id": 137161933, "when": "1 ч. 21 мин. назад", "text": "Анна"})
    assert a == b == "id:137161933"


def test_dedup_key_falls_back_to_text_without_id():
    key = notification_dedup_key({"when": "1 ч. назад", "text": "Счет пополнен"})
    assert key == "text:Счет пополнен"


# --- runner chain -----------------------------------------------------------

class StrictNotifier:
    """Only the methods the runner may call. No __getattr__ magic."""

    def __init__(self, succeeds=True):
        self.succeeds = succeeds
        self.sent = []
        self.chat_sent = []
        self.failed_count = 0

    def send_raw(self, text):
        self.sent.append(text)
        return self.succeeds

    def send_status(self, text):
        self.sent.append(text)
        return True

    def send_tasks(self, tasks):
        return 0

    def send_chat_messages(self, msgs):
        self.chat_sent.extend(msgs)
        return len(msgs) if self.succeeds else 0


class StrictClient:
    """Records chat fetches. Missing methods raise AttributeError loudly."""

    def __init__(self, messages=0, chat_messages=None, authed=True):
        self._warmed_up = True
        self._messages = messages
        self._chat = chat_messages or []
        self._authed = authed
        self.chat_fetch_calls = 0
        self.mark_calls = []

    def start(self):
        pass

    def close(self):
        pass

    def check_auth_status(self):
        return {"authenticated": self._authed}

    def fetch_account_counts(self):
        return {"notifications": 0, "messages": self._messages}

    def fetch_notifications(self, limit=15):
        return []

    def fetch_chat_messages(self):
        self.chat_fetch_calls += 1
        return self._chat

    def fetch_feed(self, **kw):
        return []

    def fetch_task_details(self, ids):
        return []

    def mark_all_notifications_read(self):
        self.mark_calls.append(True)
        return True


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(runner_mod, "LOG_FILE", tmp_path / "run.log")
    (tmp_path / "state.json").write_text(
        json.dumps({"seen_notifications": {"_baselined": {"seen_at": time.time()}}}),
        encoding="utf-8",
    )
    holder = {}

    def make_client(headless=True):
        holder["client"] = StrictClient(**holder.get("client_kw", {}))
        return holder["client"]

    def make_notifier(dry_run=False):
        holder["notifier"] = StrictNotifier(**holder.get("notifier_kw", {}))
        return holder["notifier"]

    monkeypatch.setattr(runner_mod, "YouDoClient", make_client)
    monkeypatch.setattr(runner_mod, "Notifier", make_notifier)
    return holder


INCOMING = [
    {
        "id": 161599347,
        "text": "Здравствуйте \nНапишите ваш тг",
        "sender_name": "Анна Ч.",
        "sender_id": 14316146,
        "date": "2026-10-06T01:19:13.533+00:00",
    }
]


def test_unread_chat_counter_triggers_a_fetch(wired):
    """unread_messages>0 must actually reach the chat endpoint."""
    wired["client_kw"] = {"messages": 1, "chat_messages": INCOMING}
    run_once()
    assert wired["client"].chat_fetch_calls == 1


def test_zero_unread_skips_the_fetch(wired):
    wired["client_kw"] = {"messages": 0, "chat_messages": INCOMING}
    run_once()
    assert wired["client"].chat_fetch_calls == 0


def test_chat_message_is_delivered_to_the_notifier(wired):
    """The severed-link test: runner -> client -> notifier, asserted end to end."""
    wired["client_kw"] = {"messages": 1, "chat_messages": INCOMING}
    stats = run_once()

    assert wired["notifier"].chat_sent == INCOMING
    assert stats["chat_messages_forwarded"] == 1


def test_delivered_chat_message_is_marked_seen(wired):
    wired["client_kw"] = {"messages": 1, "chat_messages": INCOMING}
    run_once()

    state = StateManager(runner_mod.STATE_FILE)
    assert state.is_chat_message_seen(161599347) is True


def test_failed_delivery_does_not_mark_seen(wired):
    """Same contract as notifications: a telegram outage must resurface."""
    wired["client_kw"] = {"messages": 1, "chat_messages": INCOMING}
    wired["notifier_kw"] = {"succeeds": False}
    stats = run_once()

    state = StateManager(runner_mod.STATE_FILE)
    assert state.is_chat_message_seen(161599347) is False
    assert stats["chat_messages_forwarded"] == 0


def test_already_seen_chat_message_is_not_refetched_as_new(wired, tmp_path):
    """Dedup by integer id — the run after the one that delivered it is quiet."""
    (tmp_path / "state.json").write_text(
        json.dumps({
            "seen_notifications": {"_baselined": {"seen_at": time.time()}},
            "seen_chat_messages": {"161599347": {"seen_at": time.time()}},
        }),
        encoding="utf-8",
    )
    wired["client_kw"] = {"messages": 1, "chat_messages": INCOMING}
    stats = run_once()

    assert wired["notifier"].chat_sent == []
    assert stats["chat_messages_forwarded"] == 0


def test_guest_session_touches_no_chat(wired, monkeypatch):
    """Account chat endpoints 404 for a guest; no fetch, no forward."""
    def guest(headless=True):
        client = StrictClient(messages=3, chat_messages=INCOMING, authed=False)
        return client

    monkeypatch.setattr(runner_mod, "YouDoClient", guest)
    stats = run_once()

    assert wired.get("client") is None
    assert "chat_messages_forwarded" not in stats


def test_dry_run_does_not_mark_chat_seen(wired):
    wired["client_kw"] = {"messages": 1, "chat_messages": INCOMING}
    stats = run_once(dry_run=True)

    state = StateManager(runner_mod.STATE_FILE)
    assert state.is_chat_message_seen(161599347) is False
    assert "chat_messages_forwarded" not in stats


def test_chat_failure_is_surfaced_in_stats_not_swallowed(wired, monkeypatch):
    """A broken chat endpoint must be visible in the run stats."""
    def boom(headless=True):
        client = StrictClient(messages=1)
        client.fetch_chat_messages = lambda: (_ for _ in ()).throw(RuntimeError("chat down"))
        return client

    monkeypatch.setattr(runner_mod, "YouDoClient", boom)
    stats = run_once()

    assert stats.get("chat_error") == "chat down"
