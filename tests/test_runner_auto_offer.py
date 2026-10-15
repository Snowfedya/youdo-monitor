"""Tests for runner auto-offer integration."""
from unittest.mock import MagicMock, patch
import pytest

from youdo_monitor.runner import run_once
from youdo_monitor.state import StateManager


class DummyClient:
    def __init__(self, *args, **kwargs):
        self._warmed_up = True
        self._page = MagicMock()

    def start(self):
        pass

    def close(self):
        pass

    def check_auth_status(self):
        return {"authenticated": True}

    def fetch_account_counts(self):
        return {"notifications": 0, "messages": 0}

    def fetch_feed(self, *args, **kwargs):
        return [{
            "id": 15299999,
            "name": "Настроить Nginx и SSL",
            "category": "webdevelopment",
            "budget": "до 5 000",
            "is_virtual": True
        }]

    def fetch_task_details(self, ids):
        return [{
            "id": 15299999,
            "description": "Нужно настроить Nginx на VPS Ubuntu и выпустить Let's Encrypt SSL.",
            "offers_count": 2,
            "is_in_progress": False,
            "is_virtual": True
        }]


def test_runner_auto_offer_integration_with_archive(tmp_path):
    """Test runner auto-offer integration with explicitly injected archive mock."""
    state_file = tmp_path / "state.json"

    with patch("youdo_monitor.runner.STATE_FILE", state_file), \
         patch("youdo_monitor.runner.YouDoClient", DummyClient), \
         patch("youdo_monitor.runner.OfferManager") as MockOfferMgr, \
         patch("youdo_monitor.runner.Notifier") as MockNotifier:

        mock_offer_inst = MockOfferMgr.return_value
        mock_offer_inst.submit_postpaid_offer.return_value = {
            "success": True,
            "task_id": 15299999,
            "price": 4250
        }

        mock_notifier_inst = MockNotifier.return_value
        mock_notifier_inst.send_tasks.return_value = 1
        mock_notifier_inst.failed_count = 0

        mock_archive = MagicMock()
        mock_archive.can_offer.return_value = True
        mock_archive.begin_offer.return_value = "att_15299999_test"
        mock_archive.sync_now.return_value = {"events": 1}

        stats = run_once(dry_run=False, enable_auto_offer=True, archive=mock_archive)

        assert stats["matched"] == 1
        assert stats["offers_submitted"] == 1
        assert mock_offer_inst.submit_postpaid_offer.called

        # Check call arguments
        call_args = mock_offer_inst.submit_postpaid_offer.call_args[1]
        assert call_args["task_id"] == 15299999
        assert call_args["price"] == 4250
        assert "nginx" in call_args["description"].lower()

        # Check archive hooks
        assert mock_archive.begin_offer.called
        assert mock_archive.finish_offer.called
        assert mock_archive.sync_now.called

        # Check state saved
        sm = StateManager(state_file=state_file)
        assert sm.is_offer_sent(15299999) is True


def test_runner_auto_offer_without_archive(tmp_path):
    """Test runner auto-offer fallback when archive is None and catalog is absent."""
    state_file = tmp_path / "state.json"

    with patch("youdo_monitor.runner.STATE_FILE", state_file), \
         patch("youdo_monitor.runner.YouDoClient", DummyClient), \
         patch("youdo_monitor.runner.OfferManager") as MockOfferMgr, \
         patch("youdo_monitor.runner.Notifier") as MockNotifier:

        mock_offer_inst = MockOfferMgr.return_value
        mock_offer_inst.submit_postpaid_offer.return_value = {
            "success": True,
            "task_id": 15299999,
            "price": 4250
        }

        mock_notifier_inst = MockNotifier.return_value
        mock_notifier_inst.send_tasks.return_value = 1
        mock_notifier_inst.failed_count = 0

        stats = run_once(dry_run=False, enable_auto_offer=True, archive=None)

        assert stats["matched"] == 1
        assert stats["offers_submitted"] == 1
        assert mock_offer_inst.submit_postpaid_offer.called

        sm = StateManager(state_file=state_file)
        assert sm.is_offer_sent(15299999) is True
