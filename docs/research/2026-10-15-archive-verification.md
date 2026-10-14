# Archive Baseline Verification & Google Access Gate Report
Date: 2026-10-15
Stage: Task 0 Baseline Protection & Gate Proof

## 1. Environment & Runtime Versions
- Python interpreter: `/root/.hermes/hermes-agent/venv/bin/python`
- Python version: `3.11.15 (main, Jun 11 2026, 04:03:40) [Clang 22.1.3 ]`
- Pytest version: `9.1.1`
- Requests version: `2.33.0`

## 2. Baseline Test Suite Execution
- Command: `/root/.hermes/hermes-agent/venv/bin/python -m pytest tests -q`
- Total tests: 85
- Passed: 79
- Failed: 6
- Execution time: 0.33s

### Known Failures Analysis
All 6 failures occur within `tests/test_chat_relay.py`:
1. `test_unread_chat_counter_triggers_a_fetch`: `assert 0 == 1` (`StrictClient.chat_fetch_calls` was 0; runner does not yet wire unread chat fetch).
2. `test_chat_message_is_delivered_to_the_notifier`: `assert [] == [{'id': 161599347, ...}]` (chat message forwarding not yet wired in `run_once`).
3. `test_delivered_chat_message_is_marked_seen`: `assert False is True` (chat messages not yet marked seen in `run_once`).
4. `test_failed_delivery_does_not_mark_seen`: `KeyError: 'chat_messages_forwarded'` (metric key absent in return dictionary).
5. `test_already_seen_chat_message_is_not_refetched_as_new`: `KeyError: 'chat_messages_forwarded'` (metric key absent in return dictionary).
6. `test_chat_failure_is_surfaced_in_stats_not_swallowed`: `assert None == 'chat down'` (exception in chat fetch not recorded as `chat_error` in return dict).

These failures represent un-implemented chat-relay runner hooks and are existing baseline behavior. All core monitoring, filtering, pricing, and notification deduplication tests pass 100%.

## 3. Fixture & PII / Secrets Audit
- `tests/fixtures_dataset.json`: 105 webdevelopment tasks captured from public feed. Verified zero emails, zero phone numbers, zero credentials or tokens. Safe for git tracking.
- Test suite files: All network interactions are mocked via fakes (`FakeClient`, `StrictClient`, `DummyClient`). No external network requests made during test execution.
- Sensitive runtime artifacts: `data/` (`cookies*.json`, `state*.json`, logs) and `logs/` are strictly excluded via `.gitignore`.
- Research files in `docs/research/`: Private research artifacts (screenshots, raw html, task payloads) remain untracked; only this verification document is committed under `docs/research/`.

## 4. Google Workspace Access Gate
- Sentinel tool: `/root/.hermes/scripts/google_auth_sentinel.py`
- Procedure:
  1. `GoogleAuthSentinel().refresh_workspace()`: Refreshed successfully (`expires in 3599s`).
  2. Reloaded refreshed token from disk (`~/.hermes/google_token.json`).
  3. Scopes verified (8 total):
     - `https://www.googleapis.com/auth/drive`
     - `https://www.googleapis.com/auth/spreadsheets`
     - `https://www.googleapis.com/auth/documents`
     - `https://www.googleapis.com/auth/calendar`
     - `https://www.googleapis.com/auth/gmail.modify`
     - `https://www.googleapis.com/auth/gmail.readonly`
     - `https://www.googleapis.com/auth/gmail.send`
     - `https://www.googleapis.com/auth/contacts.readonly`
  4. Drive API probe: `GET https://www.googleapis.com/drive/v3/files?pageSize=5&fields=files(id,name,mimeType)` returned **HTTP 200** (5 files found).
  5. Sheets API probe: `GET https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}?fields=properties.title` on existing accessible spreadsheet returned **HTTP 200**.
- Access Gate Verdict: **HEALTHY / PROVEN_READ_OK**. Google Drive and Google Sheets access is fully operational and authenticated.
