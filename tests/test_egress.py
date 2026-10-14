"""Tests for browser egress handling.

The host exports HTTP_PROXY=http://127.0.0.1:8119 (an RKN-bypass relay).
Chromium inherits it and egresses from a different IP than the VPS address,
which makes YouDo serve the "Доступ ограничен" block page instead of the feed.
"""
import os

from youdo_monitor.client import PROXY_ENV_VARS, _scrub_proxy_env


def test_scrub_removes_uppercase_proxy_vars(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8119")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8119")
    removed = _scrub_proxy_env()
    assert "HTTP_PROXY" in removed and "HTTPS_PROXY" in removed
    assert "HTTP_PROXY" not in os.environ
    assert "HTTPS_PROXY" not in os.environ


def test_scrub_removes_lowercase_proxy_vars(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:8119")
    removed = _scrub_proxy_env()
    assert "http_proxy" in removed
    assert "http_proxy" not in os.environ


def test_scrub_removes_no_proxy_vars(monkeypatch):
    """NO_PROXY also steers Chromium's egress decision and must go."""
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    removed = _scrub_proxy_env()
    assert "no_proxy" in removed and "NO_PROXY" in removed


def test_scrub_is_idempotent(monkeypatch):
    for var in PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    assert _scrub_proxy_env() == []
    assert _scrub_proxy_env() == []


def test_all_proxy_env_vars_covered():
    """Guard against a new proxy variable being added to the environment but
    not to the scrub list."""
    assert "HTTP_PROXY" in PROXY_ENV_VARS
    assert "HTTPS_PROXY" in PROXY_ENV_VARS
    assert "ALL_PROXY" in PROXY_ENV_VARS
