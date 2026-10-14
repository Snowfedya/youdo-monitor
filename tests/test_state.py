"""Unit tests for StateManager (persistence and deduplication)."""
import json
import time
from pathlib import Path
import pytest
from youdo_monitor.state import StateManager


@pytest.fixture
def tmp_state_file(tmp_path):
    return tmp_path / "state.json"


def test_initial_state_empty(tmp_state_file):
    sm = StateManager(state_file=tmp_state_file)
    assert not tmp_state_file.exists()
    assert sm.is_task_seen(123) is False
    assert sm.get_seen_count() == 0


def test_mark_and_check_task_seen(tmp_state_file):
    sm = StateManager(state_file=tmp_state_file)
    sm.mark_task_seen(15264606, status="passed", extra={"title": "Figma to WP"})
    sm.save()

    assert tmp_state_file.exists()
    assert sm.is_task_seen(15264606) is True
    assert sm.is_task_seen(99999999) is False

    # Reload from disk in a fresh instance
    sm2 = StateManager(state_file=tmp_state_file)
    assert sm2.is_task_seen(15264606) is True
    assert sm2.get_seen_count() == 1


def test_atomic_save(tmp_state_file):
    sm = StateManager(state_file=tmp_state_file)
    sm.mark_task_seen(100, status="new")
    sm.save()

    data = json.loads(tmp_state_file.read_text(encoding="utf-8"))
    assert "seen_tasks" in data
    assert "100" in data["seen_tasks"]


def test_rotation_old_entries(tmp_state_file):
    sm = StateManager(state_file=tmp_state_file, ttl_days=14)
    # Inject an entry from 20 days ago
    old_ts = time.time() - (20 * 86400)
    recent_ts = time.time() - 3600

    sm.state["seen_tasks"]["101"] = {"seen_at": old_ts, "status": "old"}
    sm.state["seen_tasks"]["102"] = {"seen_at": recent_ts, "status": "recent"}
    sm.prune_expired()
    sm.save()

    assert sm.is_task_seen(101) is False
    assert sm.is_task_seen(102) is True
    assert sm.get_seen_count() == 1


def test_mark_and_check_offer_sent(tmp_state_file):
    sm = StateManager(state_file=tmp_state_file)
    assert sm.is_offer_sent(15290010) is False

    sm.mark_offer_sent(15290010, price=8500, pitch="Тестовый отклик", mode="green")
    assert sm.is_task_seen(15290010) is True
    assert sm.is_offer_sent(15290010) is True

    # Reload from disk
    sm.save()
    sm2 = StateManager(state_file=tmp_state_file)
    assert sm2.is_offer_sent(15290010) is True
    assert sm2.state["seen_tasks"]["15290010"]["extra"]["price"] == 8500
