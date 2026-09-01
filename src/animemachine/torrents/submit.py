#!/usr/bin/env python3
"""Validate and explicitly submit stopped, hash-scoped qBittorrent plans."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


HASH_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
from ..config.loader import load_config
from ..config.runtime import apply_runtime_overrides
from ..network import transport
from .metainfo import read_torrent_file

PROJECT_ROOT=Path(__file__).resolve().parents[3]
PRODUCT_CONFIG = os.environ.get("ANM_CONFIG_PATH")
if PRODUCT_CONFIG:
    CONFIG=apply_runtime_overrides(json.loads(Path(PRODUCT_CONFIG).read_text(encoding="utf-8-sig")))
    QBT_ROOT=CONFIG["deployment"]["qbtLibraryRoot"]
    CATEGORY=CONFIG["components"]["downloadClient"]["category"]
    BASE_TAGS=set(CONFIG["components"]["downloadClient"]["tags"])
else:
    configured_state = os.getenv("ANM_STATE_DIR", "").strip()
    state_dir = Path(configured_state) if configured_state else None
    config_path = PROJECT_ROOT / "config.json"
    if not config_path.is_file():
        config_path = Path(__file__).resolve().parents[1] / "resources" / "config.example.json"
    cache_path = state_dir / "config-cache.json" if state_dir is not None else None
    CONFIG,_=load_config(config_path=config_path, cache_path=cache_path)
    client = CONFIG["components"]["downloadClient"]
    QBT_ROOT=CONFIG["deployment"]["qbtLibraryRoot"]
    CATEGORY=client["category"]
    BASE_TAGS=set(client["tags"])


def safe_relative(value: str) -> bool:
    pure = PurePosixPath(value)
    return bool(value) and not pure.is_absolute() and ".." not in pure.parts


def validate(plan: dict) -> list[str]:
    errors = []
    if plan.get("schemaVersion") not in {"1.0", "1.1", "1.2"}:
        errors.append("schemaVersion must be 1.0, 1.1 or 1.2")
    if plan.get("category") != CATEGORY:
        errors.append(f"category must be {CATEGORY}")
    endpoint = CONFIG["components"]["downloadClient"]["endpoint"]
    if plan.get("qbtEndpoint") != endpoint:
        errors.append("qbtEndpoint must match current config")
    if plan.get("qbtLibraryRoot") != QBT_ROOT:
        errors.append(f"qbtLibraryRoot must be {QBT_ROOT}")
    if not plan.get("jobs"):
        errors.append("plan must contain at least one job")
    seen_hashes = set()
    for number, job in enumerate(plan.get("jobs", []), 1):
        prefix = f"job {number}"
        operation = job.get("operation", "create")
        if operation not in {"create", "extend"}:
            errors.append(f"{prefix}: operation must be create or extend")
        info_hash = str(job.get("infoHash", "")).lower()
        if not HASH_RE.fullmatch(info_hash):
            errors.append(f"{prefix}: invalid infoHash")
        if info_hash in seen_hashes:
            errors.append(f"{prefix}: duplicate infoHash")
        seen_hashes.add(info_hash)
        save_path = str(job.get("savePath", ""))
        if not save_path.startswith(QBT_ROOT + "/") or ".." in PurePosixPath(save_path).parts:
            errors.append(f"{prefix}: savePath must be a safe child of qbtLibraryRoot")
        if job.get("contentLayout") != "NoSubfolder":
            errors.append(f"{prefix}: contentLayout must be NoSubfolder")
        tags = set(job.get("tags", []))
        if not BASE_TAGS.issubset(tags) or not job.get("resourceGroup") or job.get("resourceGroup") not in tags:
            errors.append(f"{prefix}: tags must include resourceGroup and configured base tags")
        if not Path(str(job.get("torrentPath", ""))).is_file():
            errors.append(f"{prefix}: torrentPath is missing")
        indices, old_paths, new_paths = set(), set(), set()
        for item in job.get("files", []):
            index = item.get("index")
            old_path = str(item.get("oldPath", ""))
            new_path = str(item.get("newPath", ""))
            if not isinstance(index, int) or index < 0 or index in indices or old_path in old_paths:
                errors.append(f"{prefix}: invalid or duplicate file index/path")
            indices.add(index); old_paths.add(old_path)
            if not safe_relative(old_path) or not isinstance(item.get("length"), int) or item["length"] < 0:
                errors.append(f"{prefix}: invalid oldPath/length for index {index}")
            if item.get("selected"):
                if not safe_relative(new_path) or new_path in new_paths:
                    errors.append(f"{prefix}: unsafe or duplicate newPath {new_path!r}")
                new_paths.add(new_path)
            if operation == "extend" and "selectedBefore" not in item:
                errors.append(f"{prefix}: extension requires selectedBefore for index {index}")
            if item.get("selectedBefore") and not item.get("selected"):
                errors.append(f"{prefix}: extension cannot deselect index {index}")
        if not any(item.get("selected") for item in job.get("files", [])):
            errors.append(f"{prefix}: no selected files")
        if operation == "extend" and not any(item.get("selected") and not item.get("selectedBefore") for item in job.get("files", [])):
            errors.append(f"{prefix}: extension has no newly selected files")
    return errors


def validate_live_library(plan: dict) -> list[str]:
    """Reject a stale plan before qBittorrent or its configuration is touched."""
    errors: list[str] = []
    for number, job in enumerate(plan.get("jobs", []), 1):
        for item in job.get("files", []):
            if not item.get("selected") or item.get("selectedBefore"):
                continue
            action = str(item.get("action") or "")
            final_raw = str(item.get("finalPath") or "")
            label = f"job {number} index {item.get('index')}"
            if not final_raw:
                errors.append(f"{label}: finalPath is missing")
                continue
            final_path = Path(final_raw)
            try:
                lexists = os.path.lexists(final_path)
                if final_path.is_symlink():
                    errors.append(f"{label}: finalPath is a symbolic link")
                    continue
                if action in {"add_missing", "add_coexisting"}:
                    if lexists:
                        errors.append(f"{label}: destination changed after planning; regenerate the plan")
                    continue
                if action == "stage_replace":
                    if not lexists or not final_path.is_file():
                        errors.append(f"{label}: replacement source changed after planning; regenerate the plan")
                        continue
                    stat = final_path.stat()
                    if int(stat.st_size) != int(item.get("localBytes", -1)) or int(stat.st_mtime_ns) != int(item.get("localMtimeNs", -1)):
                        errors.append(f"{label}: replacement source size/mtime changed after planning; regenerate the plan")
                    continue
                errors.append(f"{label}: unsupported selected action {action!r}")
            except OSError as exc:
                errors.append(f"{label}: cannot inspect finalPath: {exc}")
    return errors


def encode_multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    boundary = f"----anm-{uuid.uuid4().hex}"
    chunks = []
    for key, value in fields.items():
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"torrents\"; filename=\"{file_path.name}\"\r\nContent-Type: application/x-bittorrent\r\n\r\n".encode())
    chunks.append(read_torrent_file(file_path))
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class Client:
    def __init__(self, endpoint: str, api_key: str | None, allow_bypass: bool):
        if not api_key and not allow_bypass:
            raise RuntimeError("API key is absent; --allow-auth-bypass is required for trusted-network access")
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key

    def request(self, route: str, fields: dict[str, str] | None = None, *, method="POST", multipart=None) -> str:
        url = f"{self.endpoint}/api/v2/{route}"
        headers = {"Referer": self.endpoint}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        body = None
        if method == "GET":
            if fields:
                url += "?" + urllib.parse.urlencode(fields)
        elif multipart:
            body, content_type = multipart
            headers["Content-Type"] = content_type
        else:
            body = urllib.parse.urlencode(fields or {}).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            response = transport.request(method, url, headers=headers, content=body, timeout=60,
                                         max_bytes=16 * 1024 * 1024, allow_credentials=True)
            return response.text
        except Exception as exc:
            response = getattr(exc, "response", None)
            detail = (getattr(response, "text", "") or "")[:500]
            status = getattr(response, "status_code", "transport")
            raise RuntimeError(f"qBittorrent {route} returned {status}: {detail}") from exc

    def get_json(self, route: str, fields: dict[str, str] | None = None):
        return json.loads(self.request(route, fields, method="GET"))

    def post(self, route: str, fields: dict[str, str] | None = None) -> str:
        return self.request(route, fields)


def norm_path(value: str) -> str:
    return value.rstrip("/") or "/"


def get_exact_job(client: Client, info_hash: str):
    jobs = client.get_json("torrents/info", {"hashes": info_hash})
    exact = [item for item in jobs if str(item.get("hash", "")).lower() == info_hash]
    if len(exact) > 1:
        raise RuntimeError(f"multiple qBittorrent jobs returned for {info_hash}")
    return exact[0] if exact else None


def is_stopped(state: str) -> bool:
    return state.lower().startswith(("stopped", "paused"))


def wait_for_job(client: Client, info_hash: str, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = get_exact_job(client, info_hash)
        if item:
            return item
        time.sleep(0.5)
    raise RuntimeError(f"qBittorrent job did not appear: {info_hash}")


def verify_files(client: Client, job: dict, *, phase: str) -> list[dict]:
    actual = client.get_json("torrents/files", {"hash": job["infoHash"]})
    expected = {item["index"]: item for item in job["files"]}
    if set(expected) != {item.get("index") for item in actual}:
        raise RuntimeError("qBittorrent file-index set differs from plan")
    for item in actual:
        planned = expected[item["index"]]
        if phase == "before_extend":
            expected_name = planned["newPath"] if planned.get("selectedBefore") else planned["oldPath"]
        elif phase == "after":
            expected_name = planned["newPath"] if planned.get("selected") else planned["oldPath"]
        else:
            expected_name = planned["oldPath"]
        if item.get("name") != expected_name or item.get("size") != planned["length"]:
            raise RuntimeError(f"file manifest mismatch at index {item['index']}")
    return actual


def verify_recovery_files(client: Client, job: dict, *, operation: str) -> list[dict]:
    actual = client.get_json("torrents/files", {"hash": job["infoHash"]})
    expected = {item["index"]: item for item in job["files"]}
    by_index = {item.get("index"): item for item in actual}
    if set(expected) != set(by_index):
        raise RuntimeError("qBittorrent file-index set differs from plan")
    for index, planned in expected.items():
        item = by_index[index]
        if item.get("size") != planned["length"]:
            raise RuntimeError(f"file manifest mismatch at index {index}")
        name = str(item.get("name") or "")
        priority = int(item.get("priority", 0))
        if operation == "extend" and planned.get("selectedBefore"):
            if name != planned["newPath"] or priority <= 0:
                raise RuntimeError(f"prior selection mismatch at index {index}")
        elif planned.get("selected"):
            if name not in {planned["oldPath"], planned["newPath"]}:
                raise RuntimeError(f"file manifest mismatch at index {index}")
        elif name != planned["oldPath"] or (operation == "extend" and priority > 0):
            raise RuntimeError(f"file manifest mismatch at index {index}")
    return actual


def reconcile_created_job(client: Client, job: dict) -> dict:
    info_hash = job["infoHash"]
    current = get_exact_job(client, info_hash)
    if not current:
        raise RuntimeError(f"expected qBittorrent job is missing: {info_hash}")
    actual_tags = {tag.strip() for tag in str(current.get("tags", "")).split(",") if tag.strip()}
    checks = {
        "stopped": is_stopped(str(current.get("state", ""))),
        "savePath": norm_path(str(current.get("save_path", ""))) == norm_path(job["savePath"]),
        "category": current.get("category") == CATEGORY,
        "tags": set(job["tags"]).issubset(actual_tags),
        "autoTMM": current.get("auto_tmm") is False,
        "noRootFolder": no_root_layout(current, job),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError("create recovery preflight failed: " + ", ".join(failed))
    before = verify_recovery_files(client, job, operation="create")
    by_index = {item["index"]: item for item in before}
    for planned in job["files"]:
        actual = by_index[planned["index"]]
        if planned.get("selected") and actual.get("name") == planned["oldPath"] and planned["oldPath"] != planned["newPath"]:
            client.post("torrents/renameFile", {"hash": info_hash, "oldPath": planned["oldPath"], "newPath": planned["newPath"]})
    enable = [str(item["index"]) for item in job["files"] if item.get("selected") and int(by_index[item["index"]].get("priority", 0)) <= 0]
    disable = [str(item["index"]) for item in job["files"] if not item.get("selected") and int(by_index[item["index"]].get("priority", 0)) > 0]
    if enable:
        client.post("torrents/filePrio", {"hash": info_hash, "id": "|".join(enable), "priority": "1"})
    if disable:
        client.post("torrents/filePrio", {"hash": info_hash, "id": "|".join(disable), "priority": "0"})
    return verify_existing_job(client, job)


def ensure_category_and_tags(client: Client, plan: dict) -> None:
    categories = client.get_json("torrents/categories")
    if CATEGORY not in categories:
        client.post("torrents/createCategory", {"category": CATEGORY, "savePath": ""})
        if CATEGORY not in client.get_json("torrents/categories"):
            raise RuntimeError(f"failed to create {CATEGORY} category")
    required = sorted({tag for job in plan["jobs"] for tag in job["tags"]})
    existing = set(client.get_json("torrents/tags"))
    missing = [tag for tag in required if tag not in existing]
    if missing:
        client.post("torrents/createTags", {"tags": ",".join(missing)})


def no_root_layout(current: dict, job: dict) -> bool:
    """Verify NoSubfolder across qBittorrent API versions.

    Some versions transiently expose a non-empty root_path after adding a
    multifile torrent even though content_path already equals save_path.  The
    latter is the observable final-layout contract.
    """
    save = norm_path(job["savePath"])
    content = norm_path(str(current.get("content_path", "")))
    if len(job.get("files", [])) > 1:
        return str(current.get("root_path", "")) == "" or content == save
    parent = norm_path(str(Path(str(current.get("content_path", ""))).parent))
    return str(current.get("root_path", "")) == "" or parent == save


def verify_existing_job(client: Client, job: dict) -> dict:
    current = get_exact_job(client, job["infoHash"])
    if not current:
        raise RuntimeError(f"expected qBittorrent job is missing: {job['infoHash']}")
    actual_files = verify_files(client, job, phase="after")
    expected_tags = set(job["tags"])
    actual_tags = {tag.strip() for tag in str(current.get("tags", "")).split(",") if tag.strip()}
    checks = {
        "savePath": norm_path(str(current.get("save_path", ""))) == norm_path(job["savePath"]),
        "noRootFolder": no_root_layout(current, job),
        "category": current.get("category") == CATEGORY,
        "tags": expected_tags.issubset(actual_tags),
        "autoTMM": current.get("auto_tmm") is False,
        "stopped": is_stopped(str(current.get("state", ""))),
        "fileCount": len(actual_files) == len(job["files"]),
        "totalBytes": int(current.get("total_size", -1)) == sum(item["length"] for item in job["files"]),
    }
    if job.get("operation", "create") == "create" and not job.get("_preExisting"):
        checks["zeroDownloaded"] = int(current.get("downloaded", 0)) == 0
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError("final verification failed: " + ", ".join(failed))
    return {
        "infoHash": job["infoHash"], "name": current.get("name"), "state": current.get("state"),
        "savePath": current.get("save_path"), "contentPath": current.get("content_path"),
        "downloadPath": current.get("download_path"), "rootPath": current.get("root_path"),
        "category": current.get("category"), "tags": sorted(actual_tags),
        "fileCount": len(actual_files), "totalBytes": sum(item.get("size", 0) for item in actual_files),
        "checks": checks,
    }


def add_one(client: Client, job: dict) -> dict:
    info_hash = job["infoHash"]
    if get_exact_job(client, info_hash):
        raise RuntimeError(f"infohash already exists; refusing to touch it: {info_hash}")
    fields = {
        "savepath": job["savePath"], "category": CATEGORY,
        "tags": ",".join(job["tags"]), "paused": "true", "stopped": "true",
        "root_folder": "false", "contentLayout": "NoSubfolder", "autoTMM": "false",
    }
    multipart = encode_multipart(fields, Path(job["torrentPath"]))
    result = client.request("torrents/add", multipart=multipart).strip()
    accepted = result in {"Ok.", ""}
    if result.startswith("{"):
        response = json.loads(result)
        added = {str(value).lower() for value in response.get("added_torrent_ids", [])}
        accepted = info_hash in added and int(response.get("failure_count", 0)) == 0
    if not accepted:
        raise RuntimeError(f"add failed for {info_hash}: {result}")
    created = True
    try:
        current = wait_for_job(client, info_hash)
        if not is_stopped(str(current.get("state", ""))):
            client.post("torrents/stop", {"hashes": info_hash})
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                current = get_exact_job(client, info_hash)
                if current and is_stopped(str(current.get("state", ""))):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("new job could not be stopped")
        verify_files(client, job, phase="before_create")
        unselected = [str(item["index"]) for item in job["files"] if not item.get("selected")]
        if unselected:
            client.post("torrents/filePrio", {"hash": info_hash, "id": "|".join(unselected), "priority": "0"})
        for item in job["files"]:
            if item.get("selected") and item["oldPath"] != item["newPath"]:
                client.post("torrents/renameFile", {"hash": info_hash, "oldPath": item["oldPath"], "newPath": item["newPath"]})
        return verify_existing_job(client, job)
    except Exception:
        if created and get_exact_job(client, info_hash):
            client.post("torrents/delete", {"hashes": info_hash, "deleteFiles": "false"})
        raise


def restore_extension(client: Client, info_hash: str, before: list[dict]) -> None:
    current = {item["index"]: item for item in client.get_json("torrents/files", {"hash": info_hash})}
    for original in before:
        item = current.get(original["index"])
        if item and item.get("name") != original.get("name"):
            client.post("torrents/renameFile", {"hash": info_hash, "oldPath": item["name"], "newPath": original["name"]})
    for original in before:
        priority = int(original.get("priority", 0))
        client.post("torrents/filePrio", {"hash": info_hash, "id": str(original["index"]), "priority": str(priority)})


def extend_one(client: Client, job: dict) -> dict:
    info_hash = job["infoHash"]
    current = get_exact_job(client, info_hash)
    if not current:
        raise RuntimeError(f"extension target is missing: {info_hash}")
    actual_tags = {tag.strip() for tag in str(current.get("tags", "")).split(",") if tag.strip()}
    preflight = {
        "stopped": is_stopped(str(current.get("state", ""))),
        "savePath": norm_path(str(current.get("save_path", ""))) == norm_path(job["savePath"]),
        "category": current.get("category") == CATEGORY,
        "tags": BASE_TAGS.issubset(actual_tags),
        "autoTMM": current.get("auto_tmm") is False,
        "noRootFolder": no_root_layout(current, job),
    }
    failed = [name for name, passed in preflight.items() if not passed]
    if failed:
        raise RuntimeError("extension preflight failed: " + ", ".join(failed))
    before = verify_recovery_files(client, job, operation="extend")
    by_index = {item["index"]: item for item in before}
    newly_selected = [item for item in job["files"] if item.get("selected") and not item.get("selectedBefore")]
    try:
        for item in newly_selected:
            actual = by_index[item["index"]]
            if actual.get("name") == item["oldPath"] and item["oldPath"] != item["newPath"]:
                client.post("torrents/renameFile", {"hash": info_hash, "oldPath": item["oldPath"], "newPath": item["newPath"]})
        enable = [str(item["index"]) for item in newly_selected if int(by_index[item["index"]].get("priority", 0)) <= 0]
        if enable:
            client.post("torrents/filePrio", {"hash": info_hash, "id": "|".join(enable), "priority": "1"})
        result = verify_existing_job(client, job)
        result["_extensionBefore"] = before
        return result
    except Exception as exc:
        try:
            restore_extension(client, info_hash, before)
            restored = client.get_json("torrents/files", {"hash": info_hash})
            restored_by_index = {item.get("index"): item for item in restored}
            for original in before:
                current_file = restored_by_index.get(original["index"])
                if not current_file or current_file.get("name") != original.get("name") or int(current_file.get("priority", 0)) != int(original.get("priority", 0)):
                    raise RuntimeError(f"extension restoration mismatch at index {original['index']}")
        except Exception as restore_exc:
            raise RuntimeError(f"extension failed and restoration needs reconciliation: {restore_exc}") from exc
        raise


def apply_plan(plan: dict, client: Client) -> dict:
    if not plan.get("approved"):
        raise RuntimeError("live submission requires approved=true")
    live_errors = validate_live_library(plan)
    if live_errors:
        raise RuntimeError("library preflight failed: " + "; ".join(live_errors))
    version = client.request("app/version", method="GET").strip()
    api_version = client.request("app/webapiVersion", method="GET").strip()
    existing = client.get_json("torrents/info")
    existing_by_hash = {str(item.get("hash", "")).lower(): item for item in existing}
    existing_hashes = set(existing_by_hash)
    path_owners = {}
    for item in existing:
        path_owners.setdefault(norm_path(str(item.get("save_path", ""))), set()).add(str(item.get("hash", "")).lower())
    conflicts = []
    for job in plan["jobs"]:
        operation = job.get("operation", "create")
        hash_exists = job["infoHash"] in existing_hashes
        if operation == "create" and hash_exists:
            current = existing_by_hash[job["infoHash"]]
            if current.get("category") == CATEGORY:
                job["_preExisting"] = True
            else:
                conflicts.append(f"foreign existing infohash {job['infoHash']} (category={current.get('category') or 'none'}); not modified")
        if operation == "extend" and not hash_exists:
            conflicts.append(f"missing extension infohash {job['infoHash']}")
        owners = path_owners.get(norm_path(job["savePath"]), set())
        if owners and ((operation == "create" and not job.get("_preExisting")) or owners != {job["infoHash"]}):
            conflicts.append(f"existing savePath {job['savePath']}")
    if conflicts:
        raise RuntimeError("batch preflight conflict: " + "; ".join(conflicts))
    ensure_category_and_tags(client, plan)
    results = []
    try:
        for job in plan["jobs"]:
            if job.get("_preExisting"):
                results.append(reconcile_created_job(client, job))
            else:
                results.append(extend_one(client, job) if job.get("operation", "create") == "extend" else add_one(client, job))
    except Exception:
        # Preserve batch atomicity without touching pre-existing tasks or data.
        for result in reversed(results):
            info_hash = str(result["infoHash"]).lower()
            if result.get("_extensionBefore") and info_hash in existing_hashes:
                restore_extension(client, info_hash, result["_extensionBefore"])
            elif info_hash not in existing_hashes and get_exact_job(client, info_hash):
                client.post("torrents/delete", {"hashes": info_hash, "deleteFiles": "false"})
        raise
    for result in results:
        result.pop("_extensionBefore", None)
    return {
        "schemaVersion": "1.0", "timestamp": datetime.now(timezone.utc).isoformat(),
        "qBittorrentVersion": version, "webApiVersion": api_version, "results": results,
    }


def verify_existing_plan(plan: dict, client: Client) -> dict:
    version = client.request("app/version", method="GET").strip()
    api_version = client.request("app/webapiVersion", method="GET").strip()
    results = [verify_existing_job(client, job) for job in plan["jobs"]]
    return {
        "schemaVersion": "1.0", "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "verify-existing", "qBittorrentVersion": version, "webApiVersion": api_version,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify-existing", action="store_true")
    parser.add_argument("--allow-auth-bypass", action="store_true")
    parser.add_argument("--audit-output", type=Path)
    args = parser.parse_args()
    if args.apply and args.verify_existing:
        raise RuntimeError("choose only one of --apply or --verify-existing")
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    errors = validate(plan)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 2
    print(json.dumps({"valid": True, "jobs": len(plan.get("jobs", [])), "apply": args.apply, "verifyExisting": args.verify_existing}, ensure_ascii=False))
    if args.apply or args.verify_existing:
        if not args.audit_output:
            raise RuntimeError("--audit-output is required for live verification")
        client = Client(plan["qbtEndpoint"], os.environ.get("ANM_QBT_API_KEY"), args.allow_auth_bypass)
        audit = apply_plan(plan, client) if args.apply else verify_existing_plan(plan, client)
        args.audit_output.parent.mkdir(parents=True, exist_ok=True)
        args.audit_output.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"applied": args.apply, "verifiedExisting": args.verify_existing, "audit": str(args.audit_output), "hashes": [r["infoHash"] for r in audit["results"]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
