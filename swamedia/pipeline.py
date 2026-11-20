"""Orchestration: discover -> dry-run -> convert (private HF, resume, fail-closed)."""
from __future__ import annotations
import json, os, shutil, time
from collections import Counter

from . import policy, state as st
from .collector import crawl_kind, extract_sources, folder_conflicts
from .converter import convert, ConvertError
from .drive import DriveError, DriveFile, DriveRef, walk_folder
from .naming import normalize_dj, parse_episode, sort_key, find_conflicts, EpisodeInfo

STATE_PATH, SERVING_PATH, PURGE_PATH = "state/tasks.json", "manifest/serving.json", "reports/purge_report.json"


# ---------- discovery ----------
def discover(fetch_page, cache=None, kinds=("movie", "series")):
    records, stats, warnings = {}, [], []
    for k in kinds:
        r, s, w = crawl_kind(k, fetch_page, cache)
        for rid, rec in r.items():
            if rid in records:
                warnings.append(f"id {rid} appears under both kinds; kept first")
                continue
            records[rid] = rec
        stats.append(s); warnings += w
    decisions = {rid: policy.classify(rec) for rid, rec in records.items()}
    conflicts = folder_conflicts({i: records[i] for i, d in decisions.items() if d.status != "excluded"})
    return records, decisions, stats, warnings, conflicts


def review_manifest(records, decisions, conflicts):
    conflicted = {i for c in conflicts for i in c["item_ids"]}
    items = []
    for rid, d in sorted(decisions.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
        if d.status != "source_regular":
            continue
        rec = records[rid]
        refs, unsupported = extract_sources(rec)
        items.append({"id": rid, "title": rec.get("title"), "dj": normalize_dj(rec.get("dj_dubber", "")),
                      "dj_raw": rec.get("dj_dubber"), "type": rec.get("type"), "age_rating": rec.get("age_rating"),
                      "status": "source_regular", "content_verified": False,
                      "source_version": policy.source_version(rec),
                      "drive_folders": sum(r.kind == "folder" for r in refs), "drive_files": sum(r.kind == "file" for r in refs),
                      "unsupported_sources": len(unsupported), "folder_conflict": rid in conflicted,
                      "approve": False})  # owner flips to true, then exports approved_items.json
    return {"note": "source_regular != content_verified. Owner must approve item IDs/folder mappings.", "items": items}


def discovery_report(records, decisions, stats, warnings, conflicts):
    c = Counter(d.status for d in decisions.values())
    return {"api": stats, "distinct_records": len(records), "status_counts": dict(c),
            "exclusion_reasons": dict(Counter(r for d in decisions.values() if d.status == "excluded" for r in d.reasons)),
            "review_reasons": dict(Counter(r for d in decisions.values() if d.status == "needs_review" for r in d.reasons)),
            "folder_conflicts": len(conflicts), "warnings": warnings,
            "claim": "collected list records only; nothing downloaded, converted or verified"}


# ---------- planning ----------
def resolve_files(rec, drive):
    """Return (files, issues) for an item: folders recursed, single files via metadata."""
    refs, unsupported = extract_sources(rec)
    files, issues = {}, [{"type": "unsupported_source", **u} for u in unsupported]
    for ref in refs:
        try:
            if ref.kind == "file":
                m = drive.get_meta(ref.id, ref.resource_key)
                if m.get("mimeType") == "application/vnd.google-apps.folder":
                    ref = DriveRef("folder", ref.id, ref.resource_key)
                else:
                    from .drive import is_media_candidate
                    if is_media_candidate(m.get("name", ""), m.get("mimeType", "")):
                        f = DriveFile(m["id"], m["name"], m["mimeType"], int(m["size"]) if m.get("size") else None,
                                      m.get("modifiedTime", ""), m.get("md5Checksum"), m.get("resourceKey"), ())
                        if policy.has_adult_marker(f.name):
                            f.status, f.reason = "excluded", "adult_marker_in_name_or_path"
                        files.setdefault(f.id, f)
                    continue
            fs, iss = walk_folder(ref, drive.list_page)
            for f in fs:
                files.setdefault(f.id, f)
            issues += iss
        except DriveError as e:
            issues.append({"type": e.kind, "ref": ref.id, "detail": str(e)})
    return list(files.values()), issues


def plan_item(item_id, rec, drive):
    files, issues = resolve_files(rec, drive)
    eps = [{"file_id": f.id, "episode": parse_episode(f.name)} for f in files]
    conflicts = find_conflicts(eps)
    conflicted = {fid for c in conflicts for fid in c["file_ids"]}
    single_movie = rec.get("type") == "movie" and len([f for f in files if f.status == "candidate"]) == 1
    tasks = []
    for f, e in sorted(zip(files, [x["episode"] for x in eps]), key=lambda p: sort_key(p[1], p[0].name)):
        if f.status == "excluded":
            status = "excluded"
        elif e.status != "ok" and not (single_movie and e.reason == "no_explicit_number"):
            status = "needs_review"
        elif f.id in conflicted:
            status, e.reason = "needs_review", "duplicate_episode_number"
        else:
            status = "approved"
        tasks.append(st.new_task(item_id, f, e, status, st.resume_key(item_id, f.id, f.source_version)))
        if f.status == "excluded":
            tasks[-1]["review_reason"] = f.reason
    return {"item_id": item_id, "tasks": tasks, "conflicts": conflicts, "issues": issues}


# ---------- serving manifest (opaque; no HF/Drive URLs) ----------
def build_serving(records, approvals_ok, tasks):
    items = {}
    for t in tasks.values():
        rid = t["item_id"]
        if not t.get("ready") or rid not in approvals_ok or rid not in records:
            continue
        if policy.classify(records[rid]).status == "excluded":
            continue  # exclusion beats everything
        rec = records[rid]
        it = items.setdefault(rid, {"title": rec.get("title"), "dj": normalize_dj(rec.get("dj_dubber", "")),
                                    "type": rec.get("type"), "episodes": []})
        it["episodes"].append({"media_id": st.media_id(t["key"]), "season": t["season"], "episode": t["episode"],
                               "part": t["part"], "path": t["path"]})  # path is SERVER-SIDE only
    for it in items.values():
        it["episodes"].sort(key=lambda e: (e["season"] or 0, e["episode"] if e["episode"] is not None else 10**9, e["part"] or 0))
    return {"schema": 1, "items": items}


def purge_report(records, tasks):
    """Excluded items whose MP4s may already exist; we never delete silently."""
    rows = [t["path"] for t in tasks.values() if t["status"] == "uploaded" and t["item_id"] in records
            and policy.classify(records[t["item_id"]]).status == "excluded"]
    return {"needs_separate_deletion_approval": rows}


# ---------- convert ----------
def run_convert(store, drive, records, approvals, workdir, *, kind="all", only_item_id=None, max_files=5,
                batch_size=1, budget_seconds=340 * 60 - 1800, manual_retry=False, enabled=False, now_fn=st.now_utc):
    """Returns a sanitized report dict. Never downloads/uploads unless enabled AND approved."""
    rep = {"processed": 0, "uploaded": 0, "ready": 0, "failed": 0, "blocked": 0, "pending": 0, "problems": []}
    if not enabled:
        rep["problems"].append("ENABLE_CONVERSION is not true; nothing was downloaded/uploaded")
        return rep
    store.ensure_private()
    approved, problems = policy.approved_ids(records, store.load_json("approved_items.json", {"items": []}) if approvals is None else approvals)
    rep["problems"] += [f"approval {i}: {why}" for i, why in problems]
    state = store.load_json(STATE_PATH, {"tasks": {}})
    tasks = state["tasks"]
    started = time.time()
    for rid in sorted(approved):
        rec = records[rid]
        if (kind != "all" and rec.get("type") != kind) or (only_item_id and rid != str(only_item_id)):
            continue
        plan = plan_item(rid, rec, drive)
        rep["problems"] += [f"item {rid}: {i['type']}" for i in plan["issues"] if i["type"] in {"permission", "quota", "unsupported_source"}]
        for t in plan["tasks"]:
            tasks[t["key"]] = st.merge_task(tasks.get(t["key"]), t)
    if manual_retry:
        for t in tasks.values():
            if t["status"] == "failed":
                st.manual_retry(t)
    todo = [t for t in tasks.values() if t["item_id"] in approved and st.eligible(t, now_fn())
            and (kind == "all" or records[t["item_id"]].get("type") == kind)
            and (not only_item_id or t["item_id"] == str(only_item_id)) and not (t["status"] == "uploaded" and t["ready"])]
    os.makedirs(workdir, exist_ok=True)
    batch = []  # (task, local_mp4)

    def flush():
        if not batch:
            return
        try:
            for t, _ in batch:
                t["status"] = "uploaded"  # provisional; rolled back below if commit fails
            serving = build_serving(records, approved, {k: v for k, v in tasks.items()})
            store.commit([(p, t["path"]) for t, p in batch], {STATE_PATH: state, SERVING_PATH: serving,
                         PURGE_PATH: purge_report(records, tasks)}, f"swamedia: {len(batch)} file(s)")
            for t, p in batch:
                t["ready"] = store.verify_remote(t["path"], os.path.getsize(p))
                rep["uploaded"] += 1; rep["ready"] += int(t["ready"])
                t["attempts"], t["reason"] = 0, None
                os.remove(p)  # clean output only AFTER upload verified
            store.commit([], {STATE_PATH: state, SERVING_PATH: build_serving(records, approved, tasks)}, "swamedia: mark ready")
        except Exception as e:  # upload failure != Drive failure
            for t, _ in batch:
                st.mark_failed(t, f"upload: {type(e).__name__}", "network", now_fn(), stage="upload")
                rep["failed"] += 1
        batch.clear()

    for t in todo[:max_files]:
        if time.time() - started > budget_seconds:
            break
        src, dst = os.path.join(workdir, f"{t['file_id']}.src"), os.path.join(workdir, f"{t['file_id']}.mp4")
        try:
            t["status"] = "downloading"
            f = DriveFile(t["file_id"], t["original_name"], "", None, t["modified"], t["md5"], None, tuple(t["folder_trail"]))
            meta = drive.get_meta(t["file_id"]) if hasattr(drive, "get_meta") else {}
            f.size = int(meta["size"]) if meta.get("size") else None
            f.resource_key = meta.get("resourceKey")
            drive.download(f, src)
            info = convert(src, dst)
            t.update(status="converted", info=info)
            rep["processed"] += 1
            batch.append((t, dst))
            if len(batch) >= batch_size:
                flush()
        except DriveError as e:
            st.mark_failed(t, e.kind, "permission" if e.kind == "permission" else "other", now_fn())
            rep["failed"] += 1
        except ConvertError as e:
            st.mark_failed(t, "convert: " + str(e)[:120], "other", now_fn(), stage="convert"); rep["failed"] += 1
        finally:
            if os.path.exists(src):
                os.remove(src)
    flush()
    try:
        store.commit([], {STATE_PATH: state, SERVING_PATH: build_serving(records, approved, tasks),
                          PURGE_PATH: purge_report(records, tasks)}, "swamedia: checkpoint")
    except Exception as e:
        rep["problems"].append(f"checkpoint commit failed: {type(e).__name__}; pending tasks stay pending")
    c = Counter(t["status"] for t in tasks.values())
    rep["blocked"] = sum(1 for t in tasks.values() if t.get("blocked"))
    rep["pending"] = c["approved"] + c["failed"] + c["downloading"] + c["converted"]
    rep["task_counts"] = dict(c)
    return rep
