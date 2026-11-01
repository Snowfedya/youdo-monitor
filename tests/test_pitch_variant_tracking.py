"""Pitch variant tracking (spec §3 node 4 + AC5 'трекинг вариантов').

The strategist can only learn which pitch strategy converts if the variant id
lands in the funnel ledger. These tests pin that behaviour end to end (full
runner pass -> funnel JSONL), keep legacy rows readable, and assert the cron
wrapper actually enables the hybrid path without hardcoding a secret.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from youdo_monitor import funnel as funnel_mod
from youdo_monitor import generator as gen_mod
from youdo_monitor import runner as runner_mod
from youdo_monitor.generator import (
    ScopeAnalysis,
    generate_hybrid_pitch,
    get_last_pitch_variant_id,
)

KNOWN_VARIANT_IDS = {
    "det_rescope_v2",
    "det_green_infra_v2",
    "det_green_bot_v2",
    "det_green_cms_v2",
    "det_green_web_v2",
    "llm_hybrid_v2",
}

CRON_WRAPPER = Path(__file__).resolve().parent.parent / "youdo_monitor_cron.sh"


class DummyClient:
    _warmed_up = True

    def __init__(self, *args, **kwargs):
        self._page = MagicMock()

    def start(self):
        pass

    def close(self):
        pass

    def check_auth_status(self):
        return {"authenticated": True}

    def fetch_account_counts(self):
        return {"notifications": 0, "messages": 0}

    def fetch_notifications(self, limit=15):
        return []

    def fetch_chat_messages(self, max_dialogs=5):
        return []

    def mark_all_notifications_read(self):
        return True

    def fetch_feed(self, *args, **kwargs):
        return [{
            "id": 15300001,
            "name": "Настроить Nginx и SSL",
            "category": "webdevelopment",
            "budget": "до 5 000",
            "is_virtual": True,
        }]

    def fetch_task_details(self, ids):
        return [{
            "id": 15300001,
            "description": "Нужно настроить Nginx на VPS Ubuntu и выпустить Let's Encrypt SSL.",
            "offers_count": 2,
            "is_in_progress": False,
            "is_virtual": True,
        }]


def test_funnel_offer_attempt_carries_pitch_variant_id(tmp_path, monkeypatch):
    """A full runner pass persists the variant id on the offer_attempt row."""
    funnel_file = tmp_path / "funnel.jsonl"
    monkeypatch.setattr(runner_mod, "FUNNEL_FILE", funnel_file)
    monkeypatch.setattr(runner_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(runner_mod, "LOG_FILE", tmp_path / "run.log")
    monkeypatch.setattr(runner_mod, "YouDoClient", DummyClient)
    monkeypatch.setattr(runner_mod, "Notifier", MagicMock(return_value=MagicMock(
        send_tasks=MagicMock(return_value=1), failed_count=0,
        send_status=MagicMock(return_value=True),
    )))
    monkeypatch.delenv("YOUDO_HYBRID_LLM", raising=False)

    mock_offer_mgr = MagicMock()
    mock_offer_mgr.return_value.submit_postpaid_offer.return_value = {
        "success": True, "task_id": 15300001, "price": 4250,
    }
    monkeypatch.setattr(runner_mod, "OfferManager", mock_offer_mgr)

    archive = MagicMock()
    archive.can_offer.return_value = True
    archive.begin_offer.return_value = "att_15300001_test"
    archive.sync_now.return_value = {"events": 1}

    stats = runner_mod.run_once(dry_run=False, enable_auto_offer=True, archive=archive)
    assert stats["offers_submitted"] == 1

    rows = [json.loads(line) for line in funnel_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    attempts = [r for r in rows if r["event"] == "offer_attempt"]
    assert len(attempts) == 1
    assert attempts[0]["pitch_variant_id"] in KNOWN_VARIANT_IDS
    assert attempts[0]["pitch_variant_id"].startswith("det_")

    # The archive record carries the same id (additive kwarg, legacy callers fine).
    _, kwargs = archive.begin_offer.call_args
    assert kwargs["pitch_variant_id"] == attempts[0]["pitch_variant_id"]


def test_get_last_pitch_variant_id_tracks_generator_choice():
    task = {"id": 1, "name": "x", "description": "y", "budget": "1"}
    analysis = ScopeAnalysis(mode="green", domain="infra", detected_tech=["Nginx"])
    pitch, variant = generate_hybrid_pitch(task, analysis, 3000)
    assert isinstance(pitch, str) and pitch
    assert variant == "det_green_infra_v2"
    assert get_last_pitch_variant_id() == variant

    good = "Здравствуйте! Сделаю. Настрою nginx и SSL, бэкап конфигов, проверю логи. Цена 3000 ₽, срок 2 дня."
    pitch2, variant2 = generate_hybrid_pitch(task, analysis, 3000, llm_fn=lambda _p: good)
    assert variant2 == "llm_hybrid_v2"
    assert get_last_pitch_variant_id() == "llm_hybrid_v2"
    assert isinstance(pitch2, str) and pitch2


def test_legacy_offer_attempt_without_variant_still_parses(tmp_path):
    """Old ledger lines must stay readable: the key is additive only."""
    path = tmp_path / "funnel.jsonl"
    path.write_text(
        json.dumps({"ts": "2026-10-01T10:00:00+0300", "event": "offer_attempt",
                    "task_id": 1, "price": 2000}, ensure_ascii=False) + "\n"
        + json.dumps({"ts": "2026-10-01T10:00:01+0300", "event": "offer_result",
                      "task_id": 1, "result": "confirmed_success"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    rows = funnel_mod.load(path)
    assert len(rows) == 2
    assert "pitch_variant_id" not in rows[0]
    summary = funnel_mod.summarize(path)
    assert isinstance(summary, dict) and summary["offer_attempt"]["total"] == 1


def test_cron_wrapper_enables_hybrid_without_secrets():
    text = CRON_WRAPPER.read_text(encoding="utf-8")
    assert "export YOUDO_HYBRID_LLM=1" in text
    assert "http://127.0.0.1:20128/v1" in text
    # Never hardcode a credential: no bearer-looking literal in the wrapper.
    assert "sk-" not in text.replace("shellcheck", "")
    assert "Bearer " not in text


def test_hybrid_env_falls_back_when_endpoint_unreachable(monkeypatch):
    """Enabling the wrapper's env must not bypass the deterministic safety net."""
    monkeypatch.setenv("YOUDO_HYBRID_LLM", "1")
    monkeypatch.setenv("OMNIROUTE_KEY_GEMINI", "test-key")
    monkeypatch.setenv("YOUDO_OMNIROUTE_URL", "http://127.0.0.1:9/v1/chat/completions")
    monkeypatch.setenv("YOUDO_HYBRID_MODEL", "omni/pro")

    task = {"id": 2, "name": "Настроить nginx", "description": "VPS Ubuntu nginx SSL", "budget": "5000"}
    analysis = ScopeAnalysis(mode="green", domain="infra", detected_tech=["Nginx"])
    pitch, variant = generate_hybrid_pitch(task, analysis, 3500, timeout_s=1.0)
    assert variant in KNOWN_VARIANT_IDS and variant.startswith("det_")
    ok, reason = gen_mod.validate_pitch_safety_and_voice(pitch)
    assert ok, reason
