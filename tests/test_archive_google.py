import hashlib
import json
import time
from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest

from youdo_monitor.archive_google import (
    GoogleArchive,
    GoogleAuthBlocked,
    GooglePermissionBlocked,
    GoogleTransportError,
    HttpTransport,
)


def test_download_uses_exact_id(tmp_path):
    transport = Mock()
    transport.get_bytes.return_value = b"archive"
    g = GoogleArchive(transport, tmp_path / "token.json")
    assert g.download("file-123") == b"archive"
    transport.get_bytes.assert_called_once_with(
        "https://www.googleapis.com/drive/v3/files/file-123",
        params={"alt": "media"},
    )


def test_reserve_file_id():
    transport = Mock()
    transport.get_json.return_value = {"ids": ["res-999"]}
    g = GoogleArchive(transport)
    assert g.reserve_file_id() == "res-999"
    transport.get_json.assert_called_once_with(
        "https://www.googleapis.com/drive/v3/files/generateIds",
        params={"count": 1, "space": "drive"},
    )


def test_preflight_success():
    transport = Mock()
    transport.get_json.return_value = {
        "user": {"emailAddress": "user@example.com"},
        "storageQuota": {"limit": "1000000", "usage": "1000"},
    }
    g = GoogleArchive(transport)
    res = g.preflight()
    assert res["status"] == "ok"
    assert res["user"]["emailAddress"] == "user@example.com"


def test_preflight_auth_blocked_on_unauthorized():
    transport = Mock()
    transport.get_json.side_effect = GoogleAuthBlocked("401 Unauthorized")
    g = GoogleArchive(transport)
    with pytest.raises(GoogleAuthBlocked, match="401 Unauthorized"):
        g.preflight()


def test_permissions_returns_list():
    transport = Mock()
    transport.get_json.return_value = {
        "permissions": [
            {"id": "p1", "type": "user", "role": "owner", "emailAddress": "owner@example.com"}
        ]
    }
    g = GoogleArchive(transport)
    perms = g.permissions("file-123")
    assert len(perms) == 1
    assert perms[0]["role"] == "owner"
    transport.get_json.assert_called_once_with(
        "https://www.googleapis.com/drive/v3/files/file-123/permissions",
        params={"fields": "permissions(id,type,role,emailAddress,domain)"},
    )


def test_ensure_private_root_when_exists():
    transport = Mock()
    transport.get_json.side_effect = [
        # Search returns 1 folder
        {"files": [{"id": "folder-1", "name": "YouDo Archive"}]},
        # Permissions check
        {"permissions": [{"id": "p1", "type": "user", "role": "owner"}]},
    ]
    g = GoogleArchive(transport)
    folder = g.ensure_private_root(archive_key="test-key")
    assert folder["id"] == "folder-1"


def test_ensure_private_root_when_not_exists():
    transport = Mock()
    transport.get_json.side_effect = [
        # Search returns 0
        {"files": []},
        # reserve_file_id
        {"ids": ["folder-new"]},
        # permissions check
        {"permissions": [{"id": "p1", "type": "user", "role": "owner"}]},
    ]
    transport.post.return_value = {"id": "folder-new", "name": "YouDo Archive"}

    g = GoogleArchive(transport)
    folder = g.ensure_private_root(archive_key="test-key")
    assert folder["id"] == "folder-new"
    transport.post.assert_called_once()
    post_args, post_kwargs = transport.post.call_args
    assert post_args[0] == "https://www.googleapis.com/drive/v3/files"
    assert post_kwargs["json"]["id"] == "folder-new"
    assert post_kwargs["json"]["mimeType"] == "application/vnd.google-apps.folder"
    assert post_kwargs["json"]["appProperties"]["archive_key"] == "test-key"


def test_ensure_private_root_multiple_folders_raises():
    transport = Mock()
    transport.get_json.return_value = {
        "files": [{"id": "f1"}, {"id": "f2"}]
    }
    g = GoogleArchive(transport)
    with pytest.raises(GoogleTransportError, match="Multiple root folders"):
        g.ensure_private_root(archive_key="test-key")


def test_ensure_private_root_blocks_on_anyone_permission():
    transport = Mock()
    transport.get_json.side_effect = [
        {"files": [{"id": "folder-1"}]},
        {"permissions": [
            {"id": "p1", "type": "user", "role": "owner"},
            {"id": "p2", "type": "anyone", "role": "reader"},
        ]},
    ]
    g = GoogleArchive(transport)
    with pytest.raises(GooglePermissionBlocked, match="Public/domain permission detected"):
        g.ensure_private_root(archive_key="test-key")


def test_ensure_private_root_blocks_on_domain_permission():
    transport = Mock()
    transport.get_json.side_effect = [
        {"files": [{"id": "folder-1"}]},
        {"permissions": [
            {"id": "p1", "type": "domain", "role": "reader", "domain": "example.com"},
        ]},
    ]
    g = GoogleArchive(transport)
    with pytest.raises(GooglePermissionBlocked, match="Public/domain permission"):
        g.ensure_private_root(archive_key="test-key")


def test_ensure_private_root_blocks_on_unexpected_role():
    transport = Mock()
    transport.get_json.side_effect = [
        {"files": [{"id": "folder-1"}]},
        {"permissions": [
            {"id": "p1", "type": "user", "role": "writer"},
        ]},
    ]
    g = GoogleArchive(transport)
    with pytest.raises(GooglePermissionBlocked, match="Unexpected non-owner role"):
        g.ensure_private_root(archive_key="test-key")


def test_put_verified_new_file():
    transport = Mock()
    # Metadata lookup returns 404 (None or raises 404)
    transport.get_json.side_effect = GoogleTransportError("HTTP 404 Not Found", status_code=404)
    blob = b"gzipped-payload-content"
    transport.post.return_value = {"id": "file-1", "name": "batch-1.jsonl.gz"}
    transport.get_bytes.return_value = blob

    g = GoogleArchive(transport)
    res = g.put_verified("file-1", "parent-1", "batch-1.jsonl.gz", blob)

    assert res["id"] == "file-1"
    transport.post.assert_called_once()
    transport.get_bytes.assert_called_once_with(
        "https://www.googleapis.com/drive/v3/files/file-1",
        params={"alt": "media"},
    )


def test_put_verified_existing_file_hash_match():
    transport = Mock()
    blob = b"exact-same-content"
    # File exists in metadata
    transport.get_json.return_value = {"id": "file-1", "name": "batch-1.jsonl.gz"}
    # Download matches
    transport.get_bytes.return_value = blob

    g = GoogleArchive(transport)
    res = g.put_verified("file-1", "parent-1", "batch-1.jsonl.gz", blob)

    assert res["id"] == "file-1"
    transport.post.assert_not_called()  # Did not post a duplicate!


def test_put_verified_existing_file_hash_mismatch():
    transport = Mock()
    # File exists in metadata
    transport.get_json.return_value = {"id": "file-1", "name": "batch-1.jsonl.gz"}
    # Download returns different content
    transport.get_bytes.return_value = b"different-corrupted-bytes"

    g = GoogleArchive(transport)
    with pytest.raises(GoogleTransportError, match="exists with mismatched hash"):
        g.put_verified("file-1", "parent-1", "batch-1.jsonl.gz", b"expected-bytes")


def test_put_verified_upload_timeout_readback_commits_without_duplicate():
    transport = Mock()
    blob = b"payload-that-succeeded-on-server"

    # Step 1: Initial metadata check -> not found
    # Step 2: POST throws timeout error
    # Step 3: Ambiguity readback checks metadata -> found
    # Step 4: Ambiguity readback downloads bytes -> matches!
    transport.get_json.side_effect = [
        GoogleTransportError("HTTP 404 Not Found", status_code=404),
        {"id": "file-1", "name": "batch-1.jsonl.gz"},
    ]
    transport.post.side_effect = GoogleTransportError("Upload timeout", status_code=504)
    transport.get_bytes.return_value = blob

    g = GoogleArchive(transport)
    res = g.put_verified("file-1", "parent-1", "batch-1.jsonl.gz", blob)

    assert res["id"] == "file-1"
    assert transport.post.call_count == 1


def test_put_verified_readback_hash_mismatch_raises():
    transport = Mock()
    transport.get_json.side_effect = GoogleTransportError("HTTP 404 Not Found", status_code=404)
    transport.post.return_value = {"id": "file-1"}
    transport.get_bytes.return_value = b"corrupted-during-upload"

    g = GoogleArchive(transport)
    with pytest.raises(GoogleTransportError, match="verification failed"):
        g.put_verified("file-1", "parent-1", "batch-1.jsonl.gz", b"original-bytes")


def test_get_json():
    transport = Mock()
    transport.get_bytes.return_value = json.dumps({"key": "val"}).encode("utf-8")
    g = GoogleArchive(transport)
    data = g.get_json("file-json")
    assert data == {"key": "val"}
    transport.get_bytes.assert_called_once_with(
        "https://www.googleapis.com/drive/v3/files/file-json",
        params={"alt": "media"},
    )


def test_put_json_verified_creates_new():
    transport = Mock()
    transport.get_json.side_effect = GoogleTransportError("HTTP 404 Not Found", status_code=404)
    data = {"catalog": [1, 2, 3]}
    canonical_bytes = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")

    transport.post.return_value = {"id": "cat-1", "name": "catalog.json"}
    transport.get_bytes.return_value = canonical_bytes

    g = GoogleArchive(transport)
    res = g.put_json_verified("cat-1", data, parent_id="root-1", name="catalog.json")

    assert res["id"] == "cat-1"
    transport.post.assert_called_once()
    transport.patch.assert_not_called()


def test_put_json_verified_patches_existing():
    transport = Mock()
    # File exists
    transport.get_json.return_value = {"id": "cat-1", "name": "catalog.json"}
    data = {"catalog": [1, 2, 3, 4]}
    canonical_bytes = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")

    transport.patch.return_value = {"id": "cat-1", "name": "catalog.json"}
    transport.get_bytes.return_value = canonical_bytes

    g = GoogleArchive(transport)
    res = g.put_json_verified("cat-1", data)

    assert res["id"] == "cat-1"
    transport.post.assert_not_called()
    transport.patch.assert_called_once()


def test_put_json_verified_readback_mismatch_raises():
    transport = Mock()
    transport.get_json.return_value = {"id": "cat-1"}
    transport.patch.return_value = {"id": "cat-1"}
    transport.get_bytes.return_value = json.dumps({"wrong": "data"}).encode("utf-8")

    g = GoogleArchive(transport)
    with pytest.raises(GoogleTransportError, match="Readback verification failed"):
        g.put_json_verified("cat-1", {"correct": "data"})


def test_ensure_private_spreadsheet_creates_without_pregenerated_id():
    transport = Mock()
    transport.get_json.side_effect = [
        # Search returns 0
        {"files": []},
        # Permissions check on created sheet
        {"permissions": [{"id": "p1", "type": "user", "role": "owner"}]},
    ]
    transport.post.return_value = {"id": "sheet-generated-by-server", "name": "Index"}

    g = GoogleArchive(transport)
    sheet = g.ensure_private_spreadsheet("archive-key-1", "root-1", title="Index")
    assert sheet["id"] == "sheet-generated-by-server"

    transport.post.assert_called_once()
    post_args, post_kwargs = transport.post.call_args
    # Crucial: ID must NOT be passed in POST body for Google Workspace spreadsheet
    assert "id" not in post_kwargs["json"]
    assert post_kwargs["json"]["mimeType"] == "application/vnd.google-apps.spreadsheet"
    assert post_kwargs["json"]["parents"] == ["root-1"]
    assert post_kwargs["json"]["appProperties"]["archive_key"] == "archive-key-1"


def test_http_transport_redacts_token_in_errors(tmp_path):
    token_file = tmp_path / "google_token.json"
    token_file.write_text(json.dumps({"token": "secret_access_token_12345"}), encoding="utf-8")

    transport = HttpTransport(token_path=token_file)

    with patch("requests.Session.request") as mock_req:
        mock_resp = Mock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request with Bearer secret_access_token_12345 and error info"
        mock_resp.headers = {}
        mock_req.return_value = mock_resp

        with pytest.raises(GoogleTransportError) as exc_info:
            transport.get_json("https://www.googleapis.com/drive/v3/files")

        error_message = str(exc_info.value)
        assert "secret_access_token_12345" not in error_message
        assert "[REDACTED]" in error_message


def test_http_transport_retries_429_5xx_and_respects_retry_after(tmp_path):
    token_file = tmp_path / "google_token.json"
    token_file.write_text(json.dumps({"token": "token123"}), encoding="utf-8")

    transport = HttpTransport(token_path=token_file, max_retries=3)

    with patch("requests.Session.request") as mock_req, patch("time.sleep") as mock_sleep:
        resp_429 = Mock(status_code=429, headers={"Retry-After": "2"}, text="Too Many Requests")
        resp_503 = Mock(status_code=503, headers={}, text="Service Unavailable")
        resp_200 = Mock(status_code=200, headers={}, json=lambda: {"ok": True}, content=b'{"ok": true}')

        mock_req.side_effect = [resp_429, resp_503, resp_200]

        res = transport.get_json("https://www.googleapis.com/test")
        assert res == {"ok": True}
        assert mock_req.call_count == 3
        assert mock_sleep.call_count == 2
        # First sleep should respect Retry-After 2 (bounded)
        mock_sleep.assert_any_call(2.0)


def test_http_transport_does_not_retry_400_or_403(tmp_path):
    token_file = tmp_path / "google_token.json"
    token_file.write_text(json.dumps({"token": "token123"}), encoding="utf-8")

    transport = HttpTransport(token_path=token_file, max_retries=3)

    with patch("requests.Session.request") as mock_req:
        mock_resp = Mock(status_code=403, headers={}, text="Permission Denied")
        mock_req.return_value = mock_resp

        with pytest.raises(GoogleTransportError) as exc:
            transport.get_json("https://www.googleapis.com/test")

        assert exc.value.status_code == 403
        assert mock_req.call_count == 1  # No retries on 403


def test_http_transport_401_triggers_sentinel_refresh_once(tmp_path):
    token_file = tmp_path / "google_token.json"
    token_file.write_text(json.dumps({"token": "stale_token"}), encoding="utf-8")

    refresh_called = 0

    def fake_refresh():
        nonlocal refresh_called
        refresh_called += 1
        token_file.write_text(json.dumps({"token": "fresh_token"}), encoding="utf-8")
        return True

    transport = HttpTransport(token_path=token_file, refresh_fn=fake_refresh)

    with patch("requests.Session.request") as mock_req:
        resp_401 = Mock(status_code=401, headers={}, text="Unauthorized")
        resp_200 = Mock(status_code=200, headers={}, json=lambda: {"fresh": True}, content=b'{"fresh": true}')

        # First request with stale_token -> 401; second request with fresh_token -> 200
        mock_req.side_effect = [resp_401, resp_200]

        res = transport.get_json("https://www.googleapis.com/test")
        assert res == {"fresh": True}
        assert refresh_called == 1
        assert mock_req.call_count == 2


def test_http_transport_401_raises_google_auth_blocked_if_refresh_fails(tmp_path):
    token_file = tmp_path / "google_token.json"
    token_file.write_text(json.dumps({"token": "revoked_token"}), encoding="utf-8")

    def failing_refresh():
        return False

    transport = HttpTransport(token_path=token_file, refresh_fn=failing_refresh)

    with patch("requests.Session.request") as mock_req:
        resp_401 = Mock(status_code=401, headers={}, text="Unauthorized")
        mock_req.return_value = resp_401

        with pytest.raises(GoogleAuthBlocked, match="Unauthorized"):
            transport.get_json("https://www.googleapis.com/test")

        assert mock_req.call_count == 1


def test_put_verified_includes_app_properties_in_metadata():
    transport = Mock()
    transport.get_json.side_effect = GoogleTransportError("HTTP 404 Not Found", status_code=404)
    blob = b"test-blob-bytes"
    transport.post.return_value = {"id": "file-p"}
    transport.get_bytes.return_value = blob

    g = GoogleArchive(transport)
    props = {"archive_key": "k1", "batch_id": "b1", "schema_version": "1"}
    g.put_verified("file-p", "parent-1", "b1.jsonl.gz", blob, properties=props)

    transport.post.assert_called_once()
    _, post_kwargs = transport.post.call_args
    # Verify body contains multipart metadata with appProperties
    body = post_kwargs["data"]
    assert b'"archive_key": "k1"' in body
    assert b'"batch_id": "b1"' in body
    assert b'"schema_version": "1"' in body


def test_ensure_private_spreadsheet_existing_and_multiple():
    transport = Mock()
    # Case 1: Exactly 1 existing sheet
    transport.get_json.side_effect = [
        {"files": [{"id": "sheet-existing"}]},
        {"permissions": [{"id": "p1", "type": "user", "role": "owner"}]},
    ]
    g = GoogleArchive(transport)
    res = g.ensure_private_spreadsheet("key-1", "root-1")
    assert res["id"] == "sheet-existing"
    transport.post.assert_not_called()

    # Case 2: Multiple existing sheets -> blocker
    transport.get_json.side_effect = [
        {"files": [{"id": "s1"}, {"id": "s2"}]},
    ]
    with pytest.raises(GoogleTransportError, match="Multiple spreadsheets found"):
        g.ensure_private_spreadsheet("key-1", "root-1")


def test_ensure_private_spreadsheet_blocks_on_non_owner():
    transport = Mock()
    transport.get_json.side_effect = [
        {"files": [{"id": "sheet-shared"}]},
        {"permissions": [
            {"id": "p1", "type": "user", "role": "owner"},
            {"id": "p2", "type": "user", "role": "writer"},
        ]},
    ]
    g = GoogleArchive(transport)
    with pytest.raises(GooglePermissionBlocked, match="Unexpected non-owner role"):
        g.ensure_private_spreadsheet("key-1", "root-1")


def test_put_verified_upload_failure_without_ambiguity_re_raises():
    transport = Mock()
    # Step 1: Initial check 404
    # Step 2: POST fails
    # Step 3: Ambiguity readback check 404
    transport.get_json.side_effect = [
        GoogleTransportError("HTTP 404 Not Found", status_code=404),
        GoogleTransportError("HTTP 404 Not Found", status_code=404),
    ]
    transport.post.side_effect = GoogleTransportError("Network fatal error", status_code=500)

    g = GoogleArchive(transport)
    with pytest.raises(GoogleTransportError, match="Network fatal error"):
        g.put_verified("file-fail", "parent-1", "fail.bin", b"data")


def test_http_transport_missing_or_corrupt_token_raises_google_auth_blocked(tmp_path):
    missing_file = tmp_path / "non_existent_token.json"
    t1 = HttpTransport(token_path=missing_file)
    with pytest.raises(GoogleAuthBlocked, match="Token file not found"):
        t1.get_json("https://www.googleapis.com/test")

    empty_file = tmp_path / "empty_token.json"
    empty_file.write_text("{}", encoding="utf-8")
    t2 = HttpTransport(token_path=empty_file)
    with pytest.raises(GoogleAuthBlocked, match="No access token present"):
        t2.get_json("https://www.googleapis.com/test")


def test_google_archive_default_instantiation(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({"token": "t"}), encoding="utf-8")
    g = GoogleArchive(token_path=token_file)
    assert isinstance(g.transport, HttpTransport)
    assert g.transport.token_path == token_file

