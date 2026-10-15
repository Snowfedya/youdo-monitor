"""Tests for runner integration with ArchiveCapture and outbox sync."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, Mock, patch
import pytest

from youdo_monitor import runner as runner_mod
from youdo_monitor.archive_outbox import ArchiveBlocked
from youdo_monitor.runner import run_once
from youdo_monitor.state import StateManager


class DummyClient:
    def __init__(self, *args, **kwargs) -> None:
        self._warmed_up = True
        self._page = MagicMock()
        self._authed = kwargs.get("authenticated", True)
        self.feed_items = kwargs.get("feed_items", [{
            "id": 15299999,
            "name": "Настроить Nginx и SSL",
            "category": "webdevelopment",
            "budget": "до 5 000",
            "is_virtual": True,
        }])
        self.detail_items = kwargs.get("detail_items", [{
            "id": 15299999,
            "description": "Нужно настроить Nginx на VPS Ubuntu и выпустить Let's Encrypt SSL.",
            "offers_count": 2,
            "is_in_progress": False,
            "is_virtual": True,
        }])
        self.start_calls = 0
        self.close_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def check_auth_status(self) -> Dict[str, Any]:
        return {"authenticated": self._authed}

    def fetch_account_counts(self) -> Dict[str, int]:
        return {"notifications": 0, "messages": 0}

    def fetch_feed(self, *args, **kwargs) -> List[Dict[str, Any]]:
        return list(self.feed_items)

    def fetch_task_details(self, ids: List[int]) -> List[Dict[str, Any]]:
        return [d for d in self.detail_items if d["id"] in ids]


class RunnerHarness:
    """Test harness mounting isolated runner dependencies and recording call ordering."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.calls: List[str] = []
        self.ArchiveBlocked = ArchiveBlocked

        self.state_file = tmp_path / "state.json"
        self.log_file = tmp_path / "run.log"
        self.catalog_file = tmp_path / "archive_catalog.json"

        self.monkeypatch.setattr(runner_mod, "STATE_FILE", self.state_file)
        self.monkeypatch.setattr(runner_mod, "LOG_FILE", self.log_file)
        if hasattr(runner_mod, "ARCHIVE_CATALOG_FILE"):
            self.monkeypatch.setattr(runner_mod, "ARCHIVE_CATALOG_FILE", self.catalog_file)

        # Mock client
        self.client = DummyClient()
        self.client_factory = lambda *a, **kw: self.client
        self.monkeypatch.setattr(runner_mod, "YouDoClient", self.client_factory)

        # Mock notifier
        self.notifier = MagicMock()
        self.notifier.send_tasks.return_value = 1
        self.notifier.failed_count = 0
        self.monkeypatch.setattr(runner_mod, "Notifier", lambda *a, **kw: self.notifier)

        # Mock OfferManager
        self.offer = MagicMock()
        def _submit_postpaid(*a, **kw):
            self.calls.append("submit_postpaid_offer")
            return {
                "success": True,
                "task_id": kw.get("task_id", 15299999),
                "price": kw.get("price", 4250),
            }
        self.offer.submit_postpaid_offer.side_effect = _submit_postpaid
        self.monkeypatch.setattr(runner_mod, "OfferManager", lambda *a, **kw: self.offer)

        # Mock Archive
        self.archive = MagicMock()
        self.archive.can_offer.side_effect = lambda tid: (self.calls.append("can_offer") or True)
        self.archive.begin_offer.side_effect = lambda tid, price, pitch: (
            self.calls.append("begin_offer") or f"att_{tid}_123"
        )
        self.archive.finish_offer.side_effect = lambda att_id, res: self.calls.append("finish_offer")
        self.archive.observe_feed.side_effect = lambda feed: self.calls.append("observe_feed")
        self.archive.observe_detail.side_effect = lambda s, d, dec: self.calls.append("observe_detail")
        self.archive.sync_now.side_effect = lambda: (self.calls.append("sync_now") or {"events": 2})

        outbox = MagicMock()
        outbox.usage.return_value = {"total_bytes": 1024, "warning": False, "full": False}
        outbox.pending.return_value = [{"event_id": "e1"}]
        self.archive.outbox = outbox

        sync = MagicMock()
        sync.last_verified_sync = "2026-10-15T12:00:00Z"
        self.archive.sync = sync

    def run(self, **kwargs) -> Dict[str, Any]:
        if "archive" not in kwargs:
            kwargs["archive"] = self.archive
        return run_once(**kwargs)


@pytest.fixture
def runner_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunnerHarness:
    return RunnerHarness(tmp_path, monkeypatch)


# -----------------------------------------------------------------------------
# 1. Ordering and Safety Guard Tests
# -----------------------------------------------------------------------------

def test_offer_call_is_preceded_by_persisted_attempt(runner_harness: RunnerHarness) -> None:
    h = runner_harness
    stats = h.run()
    assert "begin_offer" in h.calls
    assert "submit_postpaid_offer" in h.calls
    assert "finish_offer" in h.calls
    assert h.calls.index("begin_offer") < h.calls.index("submit_postpaid_offer")
    assert h.calls.index("submit_postpaid_offer") < h.calls.index("finish_offer")
    assert stats["offers_submitted"] == 1


def test_archive_block_prevents_offer(runner_harness: RunnerHarness) -> None:
    h = runner_harness
    h.archive.begin_offer.side_effect = h.ArchiveBlocked("full")
    stats = h.run()
    h.offer.submit_postpaid_offer.assert_not_called()
    assert stats["offers_submitted"] == 0
    assert stats["offers_skipped"] == 1
    assert stats["auto_offers_blocked_archive"] == 1


def test_archive_can_offer_false_prevents_offer(runner_harness: RunnerHarness) -> None:
    h = runner_harness
    h.archive.can_offer.side_effect = None
    h.archive.can_offer.return_value = False
    stats = h.run()
    h.offer.submit_postpaid_offer.assert_not_called()
    h.archive.begin_offer.assert_not_called()
    assert stats["offers_submitted"] == 0
    assert stats["offers_skipped"] == 1
    assert stats["auto_offers_blocked_archive"] == 1


def test_observe_feed_called_immediately_after_fetch(runner_harness: RunnerHarness) -> None:
    h = runner_harness
    h.run()
    assert "observe_feed" in h.calls
    assert "observe_detail" in h.calls
    assert h.calls.index("observe_feed") < h.calls.index("observe_detail")
    assert h.calls.index("observe_feed") < h.calls.index("begin_offer")


def test_observe_detail_called_inside_fresh_loop(runner_harness: RunnerHarness) -> None:
    h = runner_harness
    h.run()
    assert h.archive.observe_detail.called
    args = h.archive.observe_detail.call_args[0]
    summary, detail, decision = args
    assert summary["id"] == 15299999
    assert "Nginx" in detail["description"]
    assert decision.passed is True


# -----------------------------------------------------------------------------
# 2. Sync Execution Under Diverse Exit Conditions
# -----------------------------------------------------------------------------

def test_sync_called_on_normal_exit(runner_harness: RunnerHarness) -> None:
    h = runner_harness
    stats = h.run()
    assert "sync_now" in h.calls
    assert stats["archive_synced_events"] == 2
    assert stats["archive_pending"] == 1
    assert stats["archive_bytes"] == 1024
    assert stats["archive_warning"] is False
    assert stats["archive_errors"] == 0


def test_sync_called_when_no_fresh_tasks(runner_harness: RunnerHarness) -> None:
    h = runner_harness
    # Pre-mark task as seen in state
    sm = StateManager(h.state_file)
    sm.mark_task_seen(15299999, status="matched")
    sm.save()

    stats = h.run()
    assert stats["new"] == 0
    assert "sync_now" in h.calls
    assert stats["archive_synced_events"] == 2


def test_sync_called_when_browser_start_fails(runner_harness: RunnerHarness) -> None:
    h = runner_harness
    h.client.start = Mock(side_effect=RuntimeError("Browser launch crash"))
    stats = h.run()
    assert stats.get("fatal") is not None
    assert "sync_now" in h.calls
    assert stats["archive_synced_events"] == 2


def test_sync_called_when_feed_fetch_fails(runner_harness: RunnerHarness) -> None:
    h = runner_harness
    h.client.fetch_feed = Mock(side_effect=RuntimeError("YouDo 502 Bad Gateway"))
    stats = h.run()
    assert stats["errors"] >= 1
    assert "sync_now" in h.calls


# -----------------------------------------------------------------------------
# 3. Adverse & Edge Cases: Dry Run, Unauth, API Errors, Init Errors
# -----------------------------------------------------------------------------

def test_dry_run_mode_does_not_save_simulated_success(runner_harness: RunnerHarness) -> None:
    h = runner_harness
    h.offer.submit_postpaid_offer.side_effect = lambda *a, **kw: {
        "success": True,
        "task_id": 15299999,
        "price": 4250,
        "dry_run": True,
    }
    stats = h.run(dry_run=True)
    assert stats["offers_submitted"] == 0
    assert stats["offers_skipped"] >= 1

    sm = StateManager(h.state_file)
    assert sm.is_offer_sent(15299999) is False


def test_unauthenticated_session_does_not_submit_offers(runner_harness: RunnerHarness) -> None:
    h = runner_harness
    h.client.check_auth_status = lambda: {"authenticated": False}
    stats = h.run()
    h.offer.submit_postpaid_offer.assert_not_called()
    h.archive.begin_offer.assert_not_called()
    assert stats["offers_submitted"] == 0


def test_no_auto_offer_flag_prevents_offers(runner_harness: RunnerHarness) -> None:
    h = runner_harness
    stats = h.run(enable_auto_offer=False)
    h.offer.submit_postpaid_offer.assert_not_called()
    h.archive.begin_offer.assert_not_called()
    assert stats["offers_submitted"] == 0


def test_finish_offer_failure_still_records_state_success(runner_harness: RunnerHarness) -> None:
    h = runner_harness
    h.archive.finish_offer.side_effect = ArchiveBlocked("disk full during finish")
    stats = h.run()

    # Offer was actually sent to YouDo
    assert h.offer.submit_postpaid_offer.called
    assert stats["offers_submitted"] == 1
    assert stats["archive_errors"] == 1

    # State still records offer sent to avoid duplicate YouDo response
    sm = StateManager(h.state_file)
    assert sm.is_offer_sent(15299999) is True


def test_offer_api_exception_handles_safely(runner_harness: RunnerHarness) -> None:
    h = runner_harness
    h.offer.submit_postpaid_offer.side_effect = RuntimeError("YouDo network timeout")
    stats = h.run()

    assert stats["offers_submitted"] == 0
    assert stats["offers_skipped"] == 1
    # Run should survive and perform sync
    assert "sync_now" in h.calls


def test_archive_init_error_blocks_offers_and_allows_read_only(runner_harness: RunnerHarness) -> None:
    h = runner_harness
    # Write invalid catalog to trigger init failure when archive=None
    h.catalog_file.write_text("invalid json {{{", encoding="utf-8")

    stats = h.run(archive=None)
    # Auto-offers must be blocked
    h.offer.submit_postpaid_offer.assert_not_called()
    assert stats["auto_offers_blocked_archive"] == 1
    assert stats["offers_skipped"] == 1
    assert stats["archive_errors"] >= 1
    # Notifications and run still proceed safely
    assert stats["matched"] == 1
    assert stats["notified"] == 1


def test_google_offline_below_limit_survives_and_records_warning(runner_harness: RunnerHarness) -> None:
    """When Google Drive/Sheets is unreachable but outbox is below limit, sync fails gracefully."""
    h = runner_harness
    h.archive.sync_now.side_effect = RuntimeError("Google 503 Service Unavailable")
    stats = h.run()

    # Offer should have been submitted normally
    assert stats["offers_submitted"] == 1
    # Telemetry should note the sync failure
    assert stats["archive_errors"] >= 1
    assert "Google 503" in str(stats.get("archive_warning", ""))
    # Outbox telemetry should still be captured
    assert stats["archive_bytes"] == 1024


def test_storage_exact_limit_blocks_offers_and_warns(runner_harness: RunnerHarness) -> None:
    """When outbox reaches exact max budget limit, offers are blocked and warning flag is set."""
    h = runner_harness
    h.archive.can_offer.side_effect = None
    h.archive.can_offer.return_value = False
    h.archive.outbox.usage.return_value = {
        "total_bytes": 67108864,
        "max_bytes": 67108864,
        "warning": True,
        "full": True,
    }
    stats = h.run()

    h.offer.submit_postpaid_offer.assert_not_called()
    assert stats["auto_offers_blocked_archive"] == 1
    assert stats["offers_skipped"] == 1
    assert stats["archive_bytes"] == 67108864
    assert stats["archive_warning"] is True


def test_default_archive_initialization_when_catalog_active(runner_harness: RunnerHarness) -> None:
    """When catalog.json is present and active=True, default ArchiveCapture is loaded."""
    h = runner_harness
    cat_payload = {
        "schema_version": 1,
        "active": True,
        "root_folder_id": "root_folder_123",
        "catalog_file_id": "cat_file_456",
        "spreadsheet_id": "sheet_789",
    }
    h.catalog_file.write_text(json.dumps(cat_payload), encoding="utf-8")

    with patch("youdo_monitor.archive_outbox.Outbox") as MockOutbox, \
         patch("youdo_monitor.archive_google.GoogleArchive") as MockGoogle, \
         patch("youdo_monitor.archive_sheets.SheetsIndex") as MockSheets, \
         patch("youdo_monitor.archive_sync.ArchiveSync") as MockSync, \
         patch("youdo_monitor.archive_capture.ArchiveCapture") as MockCapture:

        mock_cap_inst = MockCapture.return_value
        mock_cap_inst.can_offer.return_value = True
        mock_cap_inst.begin_offer.return_value = "att_default_1"
        mock_cap_inst.sync_now.return_value = {"events": 0}
        mock_cap_inst.outbox = MockOutbox.return_value
        mock_cap_inst.outbox.usage.return_value = {"total_bytes": 500, "warning": False}
        mock_cap_inst.outbox.pending.return_value = []

        stats = h.run(archive=None)

        assert MockOutbox.called
        assert MockGoogle.called
        assert MockSheets.called
        assert MockSync.called
        assert MockCapture.called
        assert stats["offers_submitted"] == 1
        assert stats["archive_bytes"] == 500

