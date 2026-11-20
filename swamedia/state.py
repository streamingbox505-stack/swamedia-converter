"""Task state machine + resume key + retry/cooldown (never permanent lock-out)."""
from __future__ import annotations
import hashlib
from datetime import datetime, timedelta, timezone

from . import SETTINGS_VERSION

STATES = {"discovered", "needs_review", "approved", "downloading", "converted", "uploaded", "failed", "excluded"}
BASE_DELAY, MAX_DELAY = 300, 86400  # 5 min .. 24 h


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat()


def resume_key(item_id, file_id, source_version, settings_version=SETTINGS_VERSION) -> str:
    return hashlib.sha256(f"{item_id}|{file_id}|{source_version}|{settings_version}".encode()).hexdigest()[:20]


def task_key(item_id, file_id) -> str:
    return f"{item_id}:{file_id}"


def media_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]  # opaque id exposed to viewers


def repo_path(item_id, file_id) -> str:
    return f"videos/swamedia/{item_id}/{file_id}.mp4"


def new_task(item_id, f, ep, status, rkey) -> dict:
    return {"key": task_key(item_id, f.id), "item_id": str(item_id), "file_id": f.id, "status": status,
            "resume_key": rkey, "path": repo_path(item_id, f.id), "original_name": f.name,
            "folder_trail": list(f.trail), "modified": f.modified, "md5": f.md5,
            "season": ep.season, "episode": ep.episode, "part": ep.part, "review_reason": ep.reason or None,
            "attempts": 0, "next_retry_at": None, "blocked": False, "reason": None, "ready": False}


def retry_delay(attempts: int) -> int:
    return min(BASE_DELAY * 2 ** max(attempts - 1, 0), MAX_DELAY)


def mark_failed(task: dict, reason: str, kind: str, now: datetime | None = None, stage: str = "download") -> dict:
    now = now or now_utc()
    task.update(status="failed", attempts=task["attempts"] + 1, reason=reason, failure_stage=stage, ready=False)
    if kind == "permission":  # 401/403 missing permission: blocked until credentials/access change
        task.update(blocked=True, next_retry_at=None)
    else:  # after 5+ attempts the delay just stays capped; the task is never locked forever
        task.update(blocked=False, next_retry_at=iso(now + timedelta(seconds=retry_delay(task["attempts"]))))
    return task


def manual_retry(task: dict) -> dict:
    task.update(status="approved", blocked=False, next_retry_at=None, reason=None)
    return task


def eligible(task: dict, now: datetime | None = None) -> bool:
    now = now or now_utc()
    if task["status"] not in {"approved", "failed", "converted", "downloading"} or task.get("blocked"):
        return False
    nra = task.get("next_retry_at")
    return not nra or datetime.fromisoformat(nra) <= now


def merge_task(existing: dict | None, fresh: dict) -> dict:
    """Keep progress when source+settings unchanged; replace (with validation) when resume_key changed."""
    if not existing:
        return fresh
    if existing["resume_key"] == fresh["resume_key"]:
        if fresh["status"] == "needs_review":
            return fresh
        keep = ("status", "attempts", "next_retry_at", "blocked", "reason", "ready", "failure_stage")
        return {**fresh, **{k: existing[k] for k in keep if k in existing}}
    fresh["replaces"] = existing["resume_key"]
    return fresh
