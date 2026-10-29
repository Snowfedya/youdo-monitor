"""Offline regression tests for offer outcomes and cron safety."""
from pathlib import Path
from unittest.mock import Mock

import pytest
from youdo_monitor.offers import OfferManager


@pytest.mark.parametrize("response, success, status", [
    ({"ok": True, "status": 200, "data": {"IsSuccess": False, "ResultObject": {"Message": "Недостаточно средств"}}}, False, "failed"),
    ({"ok": True, "status": 200, "data": {"ResultObject": {"Message": "refused"}}}, False, "unknown"),
    ({"ok": True, "status": 200, "data": {"IsSuccess": True, "ResultObject": {}}}, True, "success"),
    ({"ok": False, "status": 500, "data": {}}, False, "unknown"),
    ({"ok": False, "status": 400, "data": {"IsSuccess": False, "Message": "refused"}}, False, "failed"),
])
def test_offer_response_classification(response, success, status):
    page = Mock()
    page.evaluate.return_value = response
    manager = OfferManager()
    manager.check_postpaid_status = Mock(return_value={"postpaid_available": True})
    result = manager.submit_postpaid_offer(Mock(_page=page), 123, 1000, "pitch")
    assert result["success"] is success
    assert result["status"] == status


def test_post_exception_is_unknown():
    page = Mock()
    page.evaluate.side_effect = TimeoutError("timed out")
    manager = OfferManager()
    manager.check_postpaid_status = Mock(return_value={"postpaid_available": True})
    result = manager.submit_postpaid_offer(Mock(_page=page), 123, 1000, "pitch")
    assert result["success"] is False
    assert result["status"] == "unknown"


@pytest.mark.parametrize("path", [Path('/root/youdo-monitor/youdo_monitor_cron.sh'), Path('/root/.hermes/scripts/youdo_monitor_cron.sh')])
def test_cron_wrapper_disables_auto_offer(path):
    line = next(line for line in path.read_text().splitlines() if '-m youdo_monitor.runner' in line)
    assert '--no-auto-offer' in line.split()
