"""Shared isolation for runner tests.

`run_once` resolves its paths from module globals at call time, so a test that
patches only STATE_FILE still writes the real funnel ledger. That happened: a
full-suite run injected 118 synthetic events (including 12 fake
`confirmed_success`) into data/funnel.jsonl. No test may touch production data.
"""
import pytest

from youdo_monitor import runner


@pytest.fixture(autouse=True)
def isolate_runner_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(runner, "LOG_FILE", tmp_path / "run.log")
    monkeypatch.setattr(runner, "FUNNEL_FILE", tmp_path / "funnel.jsonl")
