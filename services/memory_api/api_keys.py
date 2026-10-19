"""Private, atomic ENV-backed credentials shared by Watson and Cortex.

Only the local admin helper writes this file. There is no public key-management
endpoint, and normal Memory credentials cannot create or administer credentials.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

KEY_PATTERN = re.compile(r"^sk_byb_([A-Za-z0-9][A-Za-z0-9-]{0,63})_[A-Za-z0-9_-]{1,256}$")
OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
META_NAME = "MEMORY_API_KEY_REGISTRY_B64"


class KeyRegistryError(Exception):
    """Safe error codes only: never include ENV contents, keys or raw exceptions."""


def key_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def owner_for_key(key: str) -> str:
    match = KEY_PATTERN.fullmatch(key)
    if not match:
        raise KeyRegistryError("INVALID_KEY_FORMAT")
    return match[1]


def read_private_env(path: Path) -> tuple[str, dict]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 2_000_000:
                raise KeyRegistryError("REGISTRY_UNAVAILABLE")
            raw = stream.read()
        for name in ("MEMORY_API_KEYS", META_NAME):
            if len(re.findall(rf"^\s*(?:export\s+)?{name}\s*=", raw, re.M)) > 1:
                raise KeyRegistryError("REGISTRY_INVALID")
        values = dict(dotenv_values(stream=io.StringIO(raw), interpolate=False))
        if "MEMORY_API_KEYS" not in values or values["MEMORY_API_KEYS"] is None:
            raise KeyRegistryError("REGISTRY_INVALID")
        return raw, values
    except (OSError, UnicodeError) as exc:
        raise KeyRegistryError("REGISTRY_UNAVAILABLE") from exc


def allowed_keys(path: Path) -> list[str]:
    _, values = read_private_env(path)
    keys = [key.strip() for key in values["MEMORY_API_KEYS"].split(",") if key.strip()]
    for key in keys:
        owner_for_key(key)
    return list(dict.fromkeys(keys))


def authenticate_key(key: str | None) -> str:
    if not key:
        raise KeyRegistryError("UNAUTHORIZED")
    try:
        owner = owner_for_key(key)
    except KeyRegistryError:
        raise KeyRegistryError("UNAUTHORIZED") from None
    path = os.getenv("MEMORY_API_KEYS_FILE")
    if path:
        allowed = allowed_keys(Path(path))
    else:
        allowed = [value.strip() for value in os.getenv("MEMORY_API_KEYS", "").split(",") if value.strip()]
    # Empty and unavailable allowlists never grant access. Do not fall back to a
    # stale process ENV when the explicitly configured file cannot be read.
    if not any(secrets.compare_digest(key, candidate) for candidate in allowed):
        raise KeyRegistryError("UNAUTHORIZED")
    return owner


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public(entry: dict) -> dict:
    return {name: entry[name] for name in (
        "id", "memoryOwner", "deviceName", "status", "imported", "managed",
        "createdAt", "importedAt", "revokedAt",
    )}


def _load(path: Path) -> tuple[str, list[str], dict, list[dict]]:
    raw, values = read_private_env(path)
    keys = list(dict.fromkeys(key.strip() for key in values["MEMORY_API_KEYS"].split(",") if key.strip()))
    try:
        registry = json.loads(base64.b64decode(values[META_NAME], validate=True)) if values.get(META_NAME) else {"version": 1, "entries": [], "events": [], "updatedAt": None}
        if registry["version"] != 1 or not isinstance(registry["entries"], list) or not isinstance(registry["events"], list):
            raise ValueError()
        entries = registry["entries"]
        if len({entry["id"] for entry in entries}) != len(entries):
            raise ValueError()
        by_id = {entry["id"]: entry for entry in entries}
        active_ids = {key_id(key) for key in keys}
        for entry in entries:
            _public(entry)
            if entry["status"] not in ("active", "revoked") or not OWNER_PATTERN.fullmatch(entry["memoryOwner"]):
                raise ValueError()
            if (entry["status"] == "active") != (entry["id"] in active_ids):
                raise ValueError()
        pending = []
        for i, key in enumerate(keys):
            owner = owner_for_key(key)
            identity = key_id(key)
            if identity in by_id:
                if by_id[identity]["memoryOwner"] != owner:
                    raise ValueError()
                continue
            entry = dict(id=identity, memoryOwner=owner, deviceName=f"Imported connection {i + 1}",
                         status="active", imported=True, managed=False, createdAt=None, importedAt=None, revokedAt=None)
            entries.append(entry)
            pending.append(entry)
        return raw, keys, registry, pending
    except (ValueError, TypeError, KeyError) as exc:
        raise KeyRegistryError("REGISTRY_INVALID") from exc


def _snapshot(registry: dict, pending: list[dict]) -> dict:
    return dict(keys=[_public(entry) for entry in registry["entries"]],
                owners=sorted({entry["memoryOwner"] for entry in registry["entries"]}),
                pendingImport=len(pending), updatedAt=registry["updatedAt"])


def list_keys(path: Path) -> dict:
    _, _, registry, pending = _load(path)
    return _snapshot(registry, pending)


def _atomic_write(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _locked(path: Path):
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _save(path: Path, original: str, keys: list[str], registry: dict) -> None:
    encoded = base64.b64encode(json.dumps(registry, separators=(",", ":")).encode()).decode()
    replacements = {"MEMORY_API_KEYS": ",".join(keys), META_NAME: encoded}
    lines = []
    for line in original.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Z0-9_]+)\s*=", line)
        if match and match[1] in replacements:
            continue
        lines.append(line)
    lines.extend(f"{name}={value}" for name, value in replacements.items())
    if read_private_env(path)[0] != original:
        raise KeyRegistryError("REGISTRY_CHANGED")
    _atomic_write(Path(str(path) + ".bak"), original)
    _atomic_write(path, "\n".join(lines) + "\n")


def mutate_keys(path: Path, request: dict) -> dict:
    action = request.get("action")
    actor = request.get("actor", "")
    nonce = request.get("idempotencyKey", "")
    if action not in ("create", "import", "rename", "revoke") or not isinstance(actor, str) or not actor or len(actor) > 300 or not isinstance(nonce, str) or not 12 <= len(nonce) <= 128:
        raise KeyRegistryError("INVALID_REQUEST")
    if action in ("create", "rename"):
        label = request.get("deviceName", "")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80 or any(ord(c) < 32 for c in label):
            raise KeyRegistryError("INVALID_REQUEST")
        request = {**request, "deviceName": label.strip()}
    if action == "create" and (not isinstance(request.get("memoryOwner"), str) or not OWNER_PATTERN.fullmatch(request["memoryOwner"])):
        raise KeyRegistryError("INVALID_OWNER")
    if action == "revoke" and request.get("confirmationText") != "REVOKE":
        raise KeyRegistryError("CONFIRMATION_REQUIRED")
    request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    nonce_hash = hashlib.sha256(f"{actor}:{nonce}".encode()).hexdigest()
    with _locked(path):
        raw, keys, registry, pending = _load(path)
        prior = next((event for event in registry["events"] if event["requestId"] == nonce_hash), None)
        if prior:
            if prior["requestHash"] != request_hash:
                raise KeyRegistryError("IDEMPOTENCY_CONFLICT")
            entry = next((entry for entry in registry["entries"] if entry["id"] == prior["keyId"]), None)
            if action == "create":
                key = next((key for key in keys if key_id(key) == prior["keyId"]), None)
                if not key:
                    raise KeyRegistryError("KEY_REVOKED")
                return {"key": _public(entry), "secret": key}
            return {"key": _public(entry)} if entry else _snapshot(registry, pending)
        timestamp = _now()
        if action != "import" and pending:
            raise KeyRegistryError("IMPORT_REQUIRED")
        if action == "create":
            if any(entry["status"] == "active" and entry["memoryOwner"] == request["memoryOwner"] and entry["deviceName"].casefold() == request["deviceName"].casefold() for entry in registry["entries"]):
                raise KeyRegistryError("DUPLICATE_DEVICE")
            key = f"sk_byb_{request['memoryOwner']}_{secrets.token_hex(32)}"
            entry = dict(id=key_id(key), memoryOwner=request["memoryOwner"], deviceName=request["deviceName"], status="active",
                         imported=False, managed=True, createdAt=timestamp, importedAt=None, revokedAt=None)
            registry["entries"].append(entry)
            keys.append(key)
            result = {"key": _public(entry), "secret": key}
        elif action == "import":
            for entry in pending:
                entry.update(managed=True, importedAt=timestamp)
            entry = None
            result = None
        else:
            entry = next((entry for entry in registry["entries"] if entry["id"] == request.get("keyId")), None)
            if not entry:
                raise KeyRegistryError("KEY_NOT_FOUND")
            if action == "rename":
                if entry["status"] != "active":
                    raise KeyRegistryError("KEY_REVOKED")
                if any(other["id"] != entry["id"] and other["status"] == "active" and other["memoryOwner"] == entry["memoryOwner"] and other["deviceName"].casefold() == request["deviceName"].casefold() for other in registry["entries"]):
                    raise KeyRegistryError("DUPLICATE_DEVICE")
                entry["deviceName"] = request["deviceName"]
            else:
                keys = [key for key in keys if key_id(key) != entry["id"]]
                entry.update(status="revoked", revokedAt=entry["revokedAt"] or timestamp)
            result = {"key": _public(entry)}
        registry["updatedAt"] = timestamp
        registry["events"].append(dict(action=action, actor=actor, timestamp=timestamp, keyId=entry["id"] if entry else None,
                                       requestId=nonce_hash, requestHash=request_hash))
        _save(path, raw, keys, registry)
        # A read-back verifies persisted state before returning success.
        persisted = list_keys(path)
        if action == "import":
            return persisted
        saved = next(item for item in persisted["keys"] if item["id"] == entry["id"])
        if saved != result["key"]:
            raise KeyRegistryError("REGISTRY_UNAVAILABLE")
        return result


def verify_key(path: Path, identity: str) -> dict:
    import httpx
    _, keys, _, _ = _load(path)
    key = next((key for key in keys if key_id(key) == identity), None)
    if not key:
        raise KeyRegistryError("KEY_REVOKED")
    try:
        response = httpx.get(os.getenv("MEMORY_API_URL", "http://127.0.0.1:8002").rstrip("/") + "/v1/key-info",
                             headers={"X-API-Key": key, "User-Agent": "Cortex-Watson-Key-Check/1.0"}, timeout=5, follow_redirects=False)
        payload = response.json() if response.status_code == 200 else {}
        verified = payload.get("keyId") == identity and payload.get("memoryOwner") == owner_for_key(key) and payload.get("managedKeys") is True
        return dict(verified=verified, checkedAt=_now(), message="Watson accepted this key for its memory owner." if verified else "Watson did not confirm this key. The saved key has not been changed.")
    except (httpx.HTTPError, ValueError):
        return dict(verified=False, checkedAt=_now(), message="Watson could not be reached. The saved key has not been changed.")
