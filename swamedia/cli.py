"""CLI: python -m swamedia.cli --mode discover|dry-run|convert"""
from __future__ import annotations
import argparse, json, os, sys
from collections import Counter

from . import pipeline, policy
from .collector import PageCache, api_url, fetch_json


def _fetch_page(kind, page):
    return fetch_json(api_url(kind, page))


def _drive_client():
    raw = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("BLOCKED: GDRIVE_SERVICE_ACCOUNT_JSON secret missing (Drive API access required).")
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=["https://www.googleapis.com/auth/drive.readonly"])

    def token():
        if not creds.valid:
            creds.refresh(Request())
        return creds.token
    from .drive import DriveClient
    return DriveClient(token)


def _write(out, name, obj):
    with open(os.path.join(out, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["discover", "dry-run", "convert", "sync-d1"], default="discover")
    ap.add_argument("--kind", choices=["all", "movie", "series"], default="all")
    ap.add_argument("--only-item-id")
    ap.add_argument("--max-files", type=int, default=5)
    ap.add_argument("--approvals", help="local approved_items.json (else read from private HF dataset)")
    ap.add_argument("--manual-retry", action="store_true")
    ap.add_argument("--out", default="out")
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--budget-minutes", type=int, default=310)
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    kinds = ("movie", "series") if a.kind == "all" else (a.kind,)
    records, decisions, stats, warnings, conflicts = pipeline.discover(_fetch_page, PageCache(os.path.join(a.workdir, "pages")), kinds)
    report = pipeline.discovery_report(records, decisions, stats, warnings, conflicts)
    _write(a.out, "discovery_report.json", report)
    _write(a.out, "review_manifest.json", pipeline.review_manifest(records, decisions, conflicts))
    summary = [f"mode={a.mode}", f"records={report['distinct_records']}", f"status={report['status_counts']}"] + warnings
    if a.mode == "sync-d1":
        from . import d1
        from .hf_state import HFStore
        store = HFStore(os.environ.get("HF_REPO", ""), os.environ.get("HF_TOKEN", ""))  # enforces PRIVATE
        approvals = json.load(open(a.approvals, encoding="utf-8")) if a.approvals else store.load_json("approved_items.json", {"items": []})
        approved, _ = policy.approved_ids(records, approvals)
        tasks = store.load_json(pipeline.STATE_PATH, {"tasks": {}})["tasks"]
        stmts = d1.build_statements(records, decisions, approved, tasks, store.repo)
        d1.write_sql_file(stmts, os.path.join(a.workdir, "d1_sync.sql"))  # local only, contains links: NOT uploaded as artifact
        n = d1.sync(d1.D1Client(os.environ["CF_ACCOUNT_ID"], os.environ["D1_DATABASE_ID"], os.environ["CF_API_TOKEN"]), stmts)
        summary.append(f"d1 statements={n} approved={len(approved)} media_rows={len(tasks)}")
    elif a.mode != "discover":
        approvals = json.load(open(a.approvals, encoding="utf-8")) if a.approvals else None
        drive = _drive_client()
        if a.mode == "dry-run":
            if approvals is None:
                sys.exit("dry-run needs --approvals approved_items.json")
            ok, problems = policy.approved_ids(records, approvals)
            plans = [pipeline.plan_item(i, records[i], drive) for i in sorted(ok)
                     if not a.only_item_id or i == a.only_item_id][: a.max_files if a.only_item_id is None else None]
            c = Counter(t["status"] for p in plans for t in p["tasks"])
            _write(a.out, "dry_run_report.json", {"items": len(plans), "task_status": dict(c), "approval_problems": problems,
                   "conflicts": [x for p in plans for x in p["conflicts"]],
                   "issues": Counter(i["type"] for p in plans for i in p["issues"])})
            summary.append(f"dry-run items={len(plans)} tasks={dict(c)}")
        else:
            from .hf_state import HFStore
            store = HFStore(os.environ.get("HF_REPO", ""), os.environ.get("HF_TOKEN", ""))
            rep = pipeline.run_convert(store, drive, records, approvals, os.path.join(a.workdir, "tmp"), kind=a.kind,
                                       only_item_id=a.only_item_id, max_files=a.max_files, manual_retry=a.manual_retry,
                                       enabled=os.environ.get("ENABLE_CONVERSION", "").lower() == "true",
                                       budget_seconds=a.budget_minutes * 60)
            _write(a.out, "convert_report.json", rep)
            summary.append(f"convert: {rep}")
    open(os.path.join(a.out, "summary.md"), "w").write("\n".join(f"- {s}" for s in summary) + "\n")
    print("\n".join(summary))


if __name__ == "__main__":
    main()
