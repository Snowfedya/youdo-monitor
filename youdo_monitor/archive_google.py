"""Google Drive & Sheets transport and verified idempotent objects.

Provides authenticated, byte-verified, durable operations on Google Drive:
- Injected HttpTransport with bounded timeouts, 429/5xx backoff retries, and token redaction.
- Automatic single token refresh on 401 via google_auth_sentinel.
- Idempotent uploads with pre-allocated Drive file IDs.
- Byte-level SHA-256 download verification on every write.
- Permission enforcement (owner-only private access guard).
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import requests


class GoogleAuthBlocked(RuntimeError):
    """Raised when Google Workspace authentication fails and cannot be recovered via sentinel."""
    pass


class GooglePermissionBlocked(RuntimeError):
    """Raised when root folder or file has invalid/public permissions."""
    pass


class GoogleTransportError(RuntimeError):
    """Raised on Google Drive/Sheets transport failures, corruption, or unexpected status."""

    def __init__(self, message: str, status_code: int | None = None, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


def redact_secrets(text: str, token: str | None = None) -> str:
    """Strip bearer tokens and secrets from strings and exception messages."""
    if not text:
        return ""
    res = re.sub(r"(Bearer\s+)[A-Za-z0-9._~+/-]+", r"\1[REDACTED]", text)
    res = re.sub(r"(client_secret=)[^&\s]+", r"\1[REDACTED]", res)
    if token and len(token) > 4:
        res = res.replace(token, "[REDACTED]")
    return res


def encode_multipart_related(metadata: dict, media_bytes: bytes, media_mime: str) -> tuple[bytes, str]:
    """Encode multipart/related body for Google Drive API v3 uploads."""
    boundary = f"===============youdo_archive_{uuid.uuid4().hex}=="
    meta_json = json.dumps(metadata, ensure_ascii=False)

    parts = [
        f"--{boundary}\r\n".encode("utf-8"),
        b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
        meta_json.encode("utf-8"),
        b"\r\n",
        f"--{boundary}\r\n".encode("utf-8"),
        f"Content-Type: {media_mime}\r\n\r\n".encode("utf-8"),
        media_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(parts)
    content_type = f"multipart/related; boundary={boundary}"
    return body, content_type


def default_sentinel_refresh(
    token_path: Path | str = "/root/.hermes/google_token.json",
    client_secret_path: Path | str = "/root/.hermes/google_client_secret.json",
    sentinel_path: Path | str = "/root/.hermes/scripts/google_auth_sentinel.py",
) -> bool:
    """Execute standard sentinel refresh procedure without modifying credential storage."""
    try:
        sentinel_file = Path(sentinel_path)
        if sentinel_file.exists():
            sentinel_dir = str(sentinel_file.parent)
            if sentinel_dir not in sys.path:
                sys.path.insert(0, sentinel_dir)
            from google_auth_sentinel import GoogleAuthSentinel

            sentinel = GoogleAuthSentinel(
                token_path=str(token_path),
                client_secret_path=str(client_secret_path),
            )
            ok, _ = sentinel.refresh_workspace()
            return bool(ok)
    except Exception:
        pass

    try:
        if Path(sentinel_path).exists():
            res = subprocess.run(
                [sys.executable, str(sentinel_path), "refresh"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return res.returncode == 0
    except Exception:
        pass

    return False


class HttpTransport:
    """HTTP transport for Google APIs with token redaction, 429/5xx retry, and 401 refresh."""

    def __init__(
        self,
        token_path: Path | str = "/root/.hermes/google_token.json",
        client_secret_path: Path | str = "/root/.hermes/google_client_secret.json",
        timeout: float = 20.0,
        max_retries: int = 3,
        sentinel_path: Path | str = "/root/.hermes/scripts/google_auth_sentinel.py",
        refresh_fn: Optional[Callable[[], bool]] = None,
    ):
        self.token_path = Path(token_path)
        self.client_secret_path = Path(client_secret_path)
        self.timeout = timeout
        self.max_retries = max_retries
        self.sentinel_path = Path(sentinel_path)
        self._refresh_fn = refresh_fn
        self._session = requests.Session()

    def _read_token(self) -> str:
        if not self.token_path.exists():
            raise GoogleAuthBlocked(f"Token file not found: {self.token_path}")
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
            token = data.get("token") or data.get("access_token")
            if not token:
                raise GoogleAuthBlocked(f"No access token present in {self.token_path}")
            return token
        except Exception as e:
            if isinstance(e, GoogleAuthBlocked):
                raise
            raise GoogleAuthBlocked(f"Failed to read token file: {e}")

    def _refresh(self) -> bool:
        if self._refresh_fn is not None:
            return self._refresh_fn()
        return default_sentinel_refresh(
            token_path=self.token_path,
            client_secret_path=self.client_secret_path,
            sentinel_path=self.sentinel_path,
        )

    def request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json_data: dict | None = None,
        data: bytes | None = None,
        headers: dict | None = None,
    ) -> requests.Response:
        attempt = 0
        refreshed_auth = False
        token = self._read_token()

        req_headers = dict(headers or {})

        while True:
            attempt += 1
            req_headers["Authorization"] = f"Bearer {token}"

            try:
                resp = self._session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    data=data,
                    headers=req_headers,
                    timeout=self.timeout,
                )
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if attempt < self.max_retries and method.upper() in ("GET", "HEAD"):
                    delay = (1.0 if attempt == 1 else 2.0) + random.uniform(0.0, 0.5)
                    time.sleep(delay)
                    continue
                safe_err = redact_secrets(str(exc), token)
                raise GoogleTransportError(f"Network error ({method} {url}): {safe_err}") from exc

            # Handle 401 Unauthorized
            if resp.status_code == 401:
                if not refreshed_auth:
                    refreshed_auth = True
                    refresh_ok = self._refresh()
                    if refresh_ok:
                        token = self._read_token()
                        continue
                safe_body = redact_secrets(resp.text, token)
                raise GoogleAuthBlocked(f"401 Unauthorized from {url}: {safe_body}")

            # Handle 429 and 5xx retries
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt < self.max_retries:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = min(float(retry_after), 30.0)
                        except ValueError:
                            delay = (1.0 if attempt == 1 else 2.0) + random.uniform(0.0, 0.5)
                    else:
                        delay = (1.0 if attempt == 1 else 2.0) + random.uniform(0.0, 0.5)
                    time.sleep(delay)
                    continue

            # Non-retryable or exhausted errors
            if resp.status_code >= 400:
                safe_body = redact_secrets(resp.text, token)
                raise GoogleTransportError(
                    f"HTTP {resp.status_code} ({method} {url}): {safe_body}",
                    status_code=resp.status_code,
                    details=safe_body,
                )

            return resp

    def get_json(self, url: str, params: dict | None = None) -> dict:
        resp = self.request("GET", url, params=params)
        return resp.json()

    def get_bytes(self, url: str, params: dict | None = None) -> bytes:
        resp = self.request("GET", url, params=params)
        return resp.content

    def post(
        self,
        url: str,
        json: dict | None = None,
        data: bytes | None = None,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        resp = self.request("POST", url, params=params, json_data=json, data=data, headers=headers)
        if resp.content:
            try:
                return resp.json()
            except Exception:
                return {}
        return {}

    def patch(
        self,
        url: str,
        json: dict | None = None,
        data: bytes | None = None,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        resp = self.request("PATCH", url, params=params, json_data=json, data=data, headers=headers)
        if resp.content:
            try:
                return resp.json()
            except Exception:
                return {}
        return {}

    def delete(self, url: str, params: dict | None = None) -> dict:
        resp = self.request("DELETE", url, params=params)
        if resp.content:
            try:
                return resp.json()
            except Exception:
                return {}
        return {}


class GoogleArchive:
    """Client for idempotent, byte-verified operations against Google Drive."""

    def __init__(
        self,
        transport: Any = None,
        token_path: Path | str = "/root/.hermes/google_token.json",
        client_secret_path: Path | str = "/root/.hermes/google_client_secret.json",
    ):
        self.token_path = Path(token_path)
        self.client_secret_path = Path(client_secret_path)
        if transport is not None:
            self.transport = transport
        else:
            self.transport = HttpTransport(
                token_path=self.token_path,
                client_secret_path=self.client_secret_path,
            )

    def preflight(self) -> dict:
        """Perform preflight probe to verify Google Drive authentication and API access."""
        try:
            data = self.transport.get_json(
                "https://www.googleapis.com/drive/v3/about",
                params={"fields": "user,storageQuota"},
            )
            return {"status": "ok", **data}
        except GoogleAuthBlocked:
            raise
        except Exception as exc:
            # Fallback probe if about endpoint fields are restricted
            if "about" in str(exc):
                files_data = self.transport.get_json(
                    "https://www.googleapis.com/drive/v3/files",
                    params={"pageSize": 1, "fields": "files(id)"},
                )
                return {"status": "ok", "probe": files_data}
            raise

    def reserve_file_id(self) -> str:
        """Reserve a pre-generated file ID from Drive API v3."""
        data = self.transport.get_json(
            "https://www.googleapis.com/drive/v3/files/generateIds",
            params={"count": 1, "space": "drive"},
        )
        ids = data.get("ids", [])
        if not ids:
            raise GoogleTransportError("No ID returned by generateIds")
        return ids[0]

    def permissions(self, file_id: str) -> list[dict]:
        """Fetch access control list permissions for file or folder."""
        data = self.transport.get_json(
            f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
            params={"fields": "permissions(id,type,role,emailAddress,domain)"},
        )
        return data.get("permissions", [])

    def assert_private_permissions(self, file_id: str) -> list[dict]:
        """Verify that object permissions are strictly private (owner-only, no anyone/domain)."""
        perms = self.permissions(file_id)
        if not perms:
            raise GooglePermissionBlocked(f"No permissions found for file/folder {file_id}")
        for p in perms:
            ptype = p.get("type")
            prole = p.get("role")
            if ptype in ("anyone", "domain"):
                raise GooglePermissionBlocked(
                    f"Public/domain permission detected on {file_id}: {p}"
                )
            if prole not in ("owner",):
                raise GooglePermissionBlocked(
                    f"Unexpected non-owner role on {file_id}: {p}"
                )
        return perms

    def ensure_private_root(
        self,
        archive_key: str,
        name: str = "YouDo Experience Archive",
    ) -> dict:
        """Find or create private archive root folder indexed by appProperty archive_key."""
        q = (
            f"mimeType = 'application/vnd.google-apps.folder' and "
            f"appProperties has {{ key='archive_key' and value='{archive_key}' }} and "
            f"trashed = false"
        )
        res = self.transport.get_json(
            "https://www.googleapis.com/drive/v3/files",
            params={"q": q, "fields": "files(id,name,mimeType,appProperties)", "spaces": "drive"},
        )
        files = res.get("files", [])
        if len(files) > 1:
            raise GoogleTransportError(
                f"Multiple root folders found for archive_key {archive_key}: {[f.get('id') for f in files]}"
            )
        if len(files) == 1:
            folder = files[0]
            self.assert_private_permissions(folder["id"])
            return folder

        # Create new folder with pre-generated ID
        folder_id = self.reserve_file_id()
        payload = {
            "id": folder_id,
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "appProperties": {
                "archive_key": archive_key,
            },
        }
        created = self.transport.post(
            "https://www.googleapis.com/drive/v3/files",
            json=payload,
        )
        if not created or "id" not in created:
            created = {"id": folder_id, "name": name, "mimeType": "application/vnd.google-apps.folder"}

        self.assert_private_permissions(created["id"])
        return created

    def ensure_private_spreadsheet(
        self,
        archive_key: str,
        parent_id: str,
        title: str = "YouDo Experience Index",
    ) -> dict:
        """Find or create private Google Spreadsheet in parent root.

        Note: Pre-generated IDs are not supported for Google Workspace files per official Google API specs.
        """
        q = (
            f"'{parent_id}' in parents and "
            f"mimeType = 'application/vnd.google-apps.spreadsheet' and "
            f"appProperties has {{ key='archive_key' and value='{archive_key}' }} and "
            f"trashed = false"
        )
        res = self.transport.get_json(
            "https://www.googleapis.com/drive/v3/files",
            params={"q": q, "fields": "files(id,name,mimeType,appProperties)", "spaces": "drive"},
        )
        files = res.get("files", [])
        if len(files) > 1:
            raise GoogleTransportError(
                f"Multiple spreadsheets found for archive_key {archive_key} in root {parent_id}: {[f.get('id') for f in files]}"
            )
        if len(files) == 1:
            sheet = files[0]
            self.assert_private_permissions(sheet["id"])
            return sheet

        # Create new spreadsheet without pre-generated ID
        payload = {
            "name": title,
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": [parent_id],
            "appProperties": {
                "archive_key": archive_key,
            },
        }
        created = self.transport.post(
            "https://www.googleapis.com/drive/v3/files",
            json=payload,
        )
        sheet_id = created.get("id")
        if not sheet_id:
            raise GoogleTransportError("Failed to create spreadsheet: no ID returned")

        self.assert_private_permissions(sheet_id)
        return created

    def ensure_folder_path(self, parent_id: str, path_parts: list[str]) -> str:
        """Find or create nested folder hierarchy under parent_id, returning deepest folder_id."""
        current_parent = parent_id
        for part in path_parts:
            q = (
                f"mimeType = 'application/vnd.google-apps.folder' and "
                f"'{current_parent}' in parents and "
                f"name = '{part}' and "
                f"trashed = false"
            )
            res = self.transport.get_json(
                "https://www.googleapis.com/drive/v3/files",
                params={"q": q, "spaces": "drive", "fields": "files(id, name)"},
            )
            files = res.get("files", []) if isinstance(res, dict) else []
            if files:
                current_parent = files[0]["id"]
            else:
                new_id = self.reserve_file_id()
                payload = {
                    "id": new_id,
                    "name": part,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [current_parent],
                }
                created = self.transport.post(
                    "https://www.googleapis.com/drive/v3/files",
                    json=payload,
                )
                current_parent = (created.get("id") if isinstance(created, dict) else None) or new_id
        return current_parent

    def download(self, file_id: str) -> bytes:
        """Download exact raw binary bytes from Google Drive file."""
        return self.transport.get_bytes(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={"alt": "media"},
        )

    def get_json(self, file_id: str) -> dict:
        """Download and parse JSON object from Google Drive file."""
        raw = self.download(file_id)
        return json.loads(raw.decode("utf-8"))

    def _get_file_metadata(self, file_id: str) -> dict | None:
        """Look up metadata for a file ID, returning None if 404/not found."""
        try:
            return self.transport.get_json(
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                params={"fields": "id,name,mimeType,size,sha256Checksum,appProperties"},
            )
        except GoogleTransportError as e:
            if e.status_code == 404 or "404" in str(e) or "Not Found" in str(e):
                return None
            raise
        except FileNotFoundError:
            return None

    def put_verified(
        self,
        file_id: str,
        parent_id: str,
        name: str,
        blob: bytes,
        properties: dict | None = None,
        mime_type: str = "application/gzip",
    ) -> dict:
        """Upload blob idempotently with pre-allocated ID, verifying SHA-256 via download readback."""
        expected_hash = hashlib.sha256(blob).hexdigest()

        # Step 1: Check if file already exists
        meta = self._get_file_metadata(file_id)
        if meta is not None:
            existing_bytes = self.download(file_id)
            existing_hash = hashlib.sha256(existing_bytes).hexdigest()
            if existing_hash == expected_hash:
                return meta
            raise GoogleTransportError(
                f"File {file_id} already exists with mismatched hash: expected {expected_hash}, found {existing_hash}"
            )

        # Step 2: Upload new file with pre-allocated file_id via multipart/related
        metadata = {
            "id": file_id,
            "name": name,
            "parents": [parent_id],
            "mimeType": mime_type,
            "appProperties": properties or {},
        }
        body, content_type = encode_multipart_related(metadata, blob, mime_type)

        try:
            created = self.transport.post(
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
                data=body,
                headers={"Content-Type": content_type},
            )
        except Exception as post_err:
            # Network ambiguity readback check: did server commit before timing out?
            ambiguity_meta = self._get_file_metadata(file_id)
            if ambiguity_meta is not None:
                downloaded = self.download(file_id)
                if hashlib.sha256(downloaded).hexdigest() == expected_hash:
                    return ambiguity_meta
                raise GoogleTransportError(
                    f"File {file_id} committed with mismatched hash after upload ambiguity"
                ) from post_err
            raise

        # Step 3: Exact readback verification
        downloaded = self.download(file_id)
        downloaded_hash = hashlib.sha256(downloaded).hexdigest()
        if downloaded_hash != expected_hash:
            raise GoogleTransportError(
                f"Upload verification failed for {file_id}: hash mismatch ({downloaded_hash} != {expected_hash})"
            )

        return created or {"id": file_id, "name": name, "sha256": downloaded_hash}

    def put_json_verified(
        self,
        file_id: str,
        data: dict,
        parent_id: str | None = None,
        name: str | None = None,
        properties: dict | None = None,
    ) -> dict:
        """Create or PATCH JSON document, verifying contents via exact download readback."""
        canonical_bytes = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
        meta = self._get_file_metadata(file_id)

        if meta is not None:
            # File exists -> PATCH content
            updated = self.transport.patch(
                f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media",
                data=canonical_bytes,
                headers={"Content-Type": "application/json; charset=UTF-8"},
            )
        else:
            # File does not exist -> multipart CREATE
            metadata = {
                "id": file_id,
                "mimeType": "application/json",
                "appProperties": properties or {},
            }
            if name:
                metadata["name"] = name
            if parent_id:
                metadata["parents"] = [parent_id]

            body, content_type = encode_multipart_related(metadata, canonical_bytes, "application/json")
            updated = self.transport.post(
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
                data=body,
                headers={"Content-Type": content_type},
            )

        # Readback verification
        readback_bytes = self.download(file_id)
        try:
            readback_data = json.loads(readback_bytes.decode("utf-8"))
        except Exception as exc:
            raise GoogleTransportError(f"Readback of JSON file {file_id} failed to parse: {exc}") from exc

        if readback_data != data:
            raise GoogleTransportError(f"Readback verification failed for JSON file {file_id}: content mismatch")

        return updated or {"id": file_id, "name": name}
