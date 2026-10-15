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

## 5. Task 10 End-to-End Live Verification & Operational Benchmarks

Verification Date: 2026-10-15
Stage: Task 10 CLI and Safe Real End-to-End Verification

### 5.1 Real Production & Test Handles
- **Production Google Drive Root Folder**:
  - Name: `YouDo Experience Archive`
  - ID: `1Ar12yCOGEmjgE-8U_a-N_LA77aqzjVYz`
  - Archive Key: `youdo_experience_archive`
  - Permissions: Strictly owner-only (`snowfedya@gmail.com`), verified private.
- **Production Google Sheets Index**:
  - Name: `YouDo Experience Index`
  - ID: `1qqEQP0Od73DxxTl-KCOvPUAHR26vMNynm1T8Xcfos24`
  - Sheet Tab IDs:
    - `Лист1`: `0`
    - `Заказы`: `405706904`
    - `Отклики`: `236479571`
    - `Результаты`: `803780645`
    - `Синхронизация`: `1665998883`
- **Cloud Catalog**:
  - File: `catalog.json`
  - ID: `1cPb87g0ZItCRzFgS-IT64R0Oqw5MhMbU`
  - Parent: `1Ar12yCOGEmjgE-8U_a-N_LA77aqzjVYz`
- **Local Catalog**:
  - Path: `/root/youdo-monitor/data/archive_catalog.json`
  - Permissions: `0600` (`-rw-------`)
- **HMAC Export Key**:
  - Path: `/root/youdo-monitor/data/archive_export.key`
  - Permissions: `0600` (`-rw-------`, 32 bytes)
- **Isolated Test Destination Spreadsheet** (rebuild & outcome lifecycle testing):
  - Name: `Test Rebuild Destination Index`
  - ID: `1vDxWFH2i17-FArzVrtBCfg3-eZIud7SE6IC5Mbm9Jak`
  - Status: Preserved for inspection per verification brief.

### 5.2 Measured Real Performance Metrics (`/usr/bin/time -v`)

All measurements executed with real Google Workspace API calls on host `5.4.0-216-generic` / Python 3.11.15.

| Operation / Command | Elapsed (s) | Max RSS (KB) | Key Outputs & State Metrics |
|---|---|---|---|
| `preflight` | 1.12 | 33,200 | HTTP 200; user `snowfedya@gmail.com`; Drive quota checked |
| `bootstrap --name "YouDo Experience Archive"` | 11.53 | 33,404 | Root folder, spreadsheet, 4 tabs, cloud catalog & local 0600 catalog initialized |
| `import-state --path data/state.json` (Run 1) | 1.85 | 33,492 | 333 tasks scanned, 696 events generated, 696 inserted, 0 duplicates |
| `import-state --path data/state.json` (Run 2) | 1.82 | 33,480 | 333 tasks scanned, 696 events generated, 0 inserted, 696 duplicates skipped (100% idempotent) |
| `sync` (Run 1) | 26.52 | 39,120 | 2 batches processed (1 recovered + 1 new); 39 events synced to Drive & Sheets |
| `sync` (Run 2) | 3.45 | 37,800 | 0 batches, `no_pending_events` (100% idempotent) |
| `lookup --task-id 15320824` | 3.20 | 38,288 | 3 canonical events retrieved (`task_observed`, `offer_attempt`, `offer_result`); package SHA-256 verified |
| `export --output /tmp/youdo_export_sample.jsonl.gz` | 2.70 | 37,660 | 735 events exported; compressed size: 19,887 bytes; SHA-256: `665fbd19...` |
| `verify --live-feed --no-offers --no-notifications` | 29.79 | 46,580 | 39 tasks scanned from live feed; 5 task details fetched; 1 batch (`batch_20261015_023444_7baf3f`) with 10 events synced; 0 offers sent, 0 notifications sent |
| `rebuild-index --destination-spreadsheet-id <test_id>` | 16.29 | 37,732 | 3 batches replayed; 745 events replayed; reconstructed 333 Orders, 30 Offers (exact parity with cloud packages) |

### 5.3 Safety & Invariant Verification Checks
1. **Side-Effect Free Verification**:
   - `python -m youdo_monitor.archive_cli verify` strictly enforces `--live-feed --no-offers --no-notifications`.
   - Never instantiates `OfferManager.submit` or `Notifier` Telegram send.
   - Does NOT write to monitoring `seen_tasks` in `data/state.json`.
2. **Rebuild Protection**:
   - `rebuild-index` strictly refuses invocation without `--destination-spreadsheet-id`.
   - Rejects target spreadsheet matching production catalog (`ValueError: Destination spreadsheet matches production spreadsheet!`).
3. **Manual Outcome Revisioning on Live Sheets**:
   - Verified on `1vDxWFH2i17-FArzVrtBCfg3-eZIud7SE6IC5Mbm9Jak`:
   - Revision 1: Outcome `out_24d88e76f875` created with note `"Initial note"` and revision hash `3821d4038c4fe4cfee57db7155be54a41e5fc633d03a3cbe8fa4765dd1a76a5c`.
   - Revision 2: Note updated to `"Updated second note"` -> `outcome_id` preserved, revision hash bumped to `28daae6ce32dd8b886586b8af3be33682d22d7cbba914d5e8d5981cf10618afe`. User fields (`amount: 5000`, `currency: RUB`, `task_id: 12655116`) preserved.
4. **Integration Test Event Exclusion**:
   - Events marked with `source="integration_test"` are excluded from training export by default.
   - Verified in `tests/test_archive_cli.py`: exported with `--include-tests` only when explicitly requested.

