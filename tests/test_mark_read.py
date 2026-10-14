"""Tests for marking account notifications as read on YouDo.

The unread counter must drop to 0 after `mark_all_notifications_read` and a
failed call must be reported instead of silently swallowed.
"""
import pytest

from youdo_monitor.client import YouDoClient, extract_max_notification_id


def test_extract_max_notification_id_picks_largest():
    """`maxId` sent to markallnotificationsasvisited must be the NEWEST
    notification, i.e. the largest id in the panel — not the first element."""
    html = (
        '<div class="NotificationItem_item__tOGk7" data-id="137041382"></div>'
        '<div class="NotificationItem_item__tOGk7" data-id="137096245"></div>'
        '<div class="NotificationItem_item__tOGk7" data-id="137096141"></div>'
    )
    assert extract_max_notification_id(html) == 137096245


def test_extract_max_notification_id_ignores_object_ids():
    """`data-object` carries task/user ids (15273447, 4645800) and must never
    be mistaken for a notification id."""
    html = (
        '<div data-object="15278961" data-type="235"></div>'
        '<div data-id="131617211"></div>'
    )
    assert extract_max_notification_id(html) == 131617211


def test_extract_max_notification_id_handles_empty_panel():
    assert extract_max_notification_id("") is None
    assert extract_max_notification_id("<div>Уведомления</div>") is None


def test_extract_max_notification_id_ignores_malformed():
    """A truncated attr must not raise and must not produce a bogus id."""
    html = '<div data-id="13709" data-id=""></div>'
    assert extract_max_notification_id(html) is None


def test_open_dropdown_does_not_toggle_a_panel_that_is_already_open(monkeypatch):
    """REGRESSION: the header bell TOGGLES the panel. Escape does not close it,
    so after fetch_notifications() it is still open — a blind click CLOSES it
    and every later read of the panel times out."""
    clicks = []

    class FakePage:
        def click(self, selector, timeout=None):
            clicks.append(selector)
            state["open"] = not state["open"]  # the bell toggles

        def wait_for_function(self, script, timeout=None):
            if not state["open"]:
                raise RuntimeError("Timeout")

        def evaluate(self, script, arg=None):
            if script == YouDoClient.PANEL_PREDICATE:
                return state["open"]
            return {}

    state = {"open": False}
    client = YouDoClient.__new__(YouDoClient)
    client._page = FakePage()

    assert client._open_notification_dropdown() is True  # opens: closed -> open
    assert client._open_notification_dropdown() is True  # already open, no click
    assert clicks == ['[data-test="NotificationIcon"]'], clicks


def test_mark_all_notifications_read_calls_endpoint(monkeypatch):
    """The POST body must carry the maxId scraped from the live panel."""
    calls = {}

    class FakePage:
        def click(self, *a, **k):
            pass

        def wait_for_timeout(self, *a, **k):
            pass

        def evaluate(self, script, arg=None):
            calls["script"] = script
            if "Уведомления" in script and "outerHTML" in script:
                return html_for_maxid
            if "markallnotificationsasvisited" in script:
                calls["posted"] = arg
                return {"ok": True}
            raise AssertionError("unexpected evaluate: %s" % script[:60])

    html_for_maxid = '<div data-id="137096245"></div>'

    client = YouDoClient.__new__(YouDoClient)
    client._page = FakePage()
    monkeypatch.setattr(client, "_open_notification_dropdown", lambda: True)

    assert client.mark_all_notifications_read() is True
    assert calls["posted"] == {"maxId": 137096245}


def test_mark_all_notifications_read_returns_false_when_panel_absent(monkeypatch):
    class FakePage:
        def click(self, *a, **k):
            pass

        def wait_for_timeout(self, *a, **k):
            pass

        def evaluate(self, script, arg=None):
            if "Уведомления" in script and "outerHTML" in script:
                return "<div>Уведомления</div>"
            raise AssertionError("must not POST without a maxId")

    client = YouDoClient.__new__(YouDoClient)
    client._page = FakePage()
    monkeypatch.setattr(client, "_open_notification_dropdown", lambda: False)

    assert client.mark_all_notifications_read() is False


def test_mark_all_notifications_read_reports_failure(monkeypatch):
    """A rejected POST must surface as False, not as a silent success."""
    class FakePage:
        def click(self, *a, **k):
            pass

        def wait_for_timeout(self, *a, **k):
            pass

        def evaluate(self, script, arg=None):
            if "Уведомления" in script and "outerHTML" in script:
                return '<div data-id="137096245"></div>'
            if "markallnotificationsasvisited" in script:
                return {"ok": False, "status": 500}
            raise AssertionError("unexpected evaluate")

    client = YouDoClient.__new__(YouDoClient)
    client._page = FakePage()
    monkeypatch.setattr(client, "_open_notification_dropdown", lambda: True)

    assert client.mark_all_notifications_read() is False


@pytest.mark.parametrize("inner_html,expected", [
    ('<div data-id="137096245"></div>', 137096245),
    ('<div data-id=" 137096245 "></div>', 137096245),
])
def test_extract_max_notification_id_tolerates_whitespace(inner_html, expected):
    assert extract_max_notification_id(inner_html) == expected