"""Tests for account-notification deduplication in StateManager."""
import json

from youdo_monitor.state import StateManager


def test_new_state_has_no_notifications(tmp_path):
    sm = StateManager(tmp_path / "state.json")
    assert sm.state.get("seen_notifications") == {}
    assert sm.is_notification_seen("x") is False


def test_mark_then_seen(tmp_path):
    sm = StateManager(tmp_path / "state.json")
    key = "вчера 21:35|Задание изменено."
    assert sm.is_notification_seen(key) is False
    sm.mark_notification_seen(key)
    assert sm.is_notification_seen(key) is True
    sm.save()
    reloaded = StateManager(tmp_path / "state.json")
    assert reloaded.is_notification_seen(key) is True


def test_baselined_marker_survives_reload(tmp_path):
    """The runner uses a '_baselined' key to skip the first-run flood; if it
    did not persist, every restart would re-send years of stale history."""
    sm = StateManager(tmp_path / "state.json")
    sm.mark_notification_seen("_baselined")
    sm.save()
    reloaded = StateManager(tmp_path / "state.json")
    assert "_baselined" in reloaded.state["seen_notifications"]


def test_prune_keeps_recent_drops_stale(tmp_path):
    sm = StateManager(tmp_path / "state.json", ttl_days=14)
    sm.mark_notification_seen("recent")
    sm.state["seen_notifications"]["ancient"] = {"seen_at": 1.0, "status": "notified"}
    sm.state["seen_notifications"]["_baselined"] = {"seen_at": 1.0, "status": "notified"}
    sm.prune_expired()
    notes = sm.state["seen_notifications"]
    assert "recent" in notes
    assert "ancient" not in notes
    # The baseline marker must survive pruning — it is infrastructure, not history.
    assert "_baselined" in notes


def test_prune_does_not_break_on_missing_key(tmp_path):
    sm = StateManager(tmp_path / "state.json")
    sm.prune_expired()
    assert sm.state["seen_notifications"] == {}


def test_save_is_atomic_json(tmp_path):
    sm = StateManager(tmp_path / "state.json")
    sm.mark_notification_seen("k")
    sm.save()
    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "k" in data["seen_notifications"]
