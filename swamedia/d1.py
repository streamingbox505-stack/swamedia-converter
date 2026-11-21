"""Sync SwaMedia metadata to Cloudflare D1 (REST). Excluded items are stored WITHOUT links."""
from __future__ import annotations
import os, re
from . import policy
from .collector import extract_sources
from .naming import normalize_dj
from .state import now_utc, iso

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "d1", "schema.sql")


def lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\x00", "").replace("'", "''") + "'"


def _ins(table, row, keys):
    return f"INSERT OR REPLACE INTO {table} ({','.join(keys)}) VALUES ({','.join(lit(row.get(k)) for k in keys)});"


def build_statements(records, decisions, approved, tasks, hf_repo):
    ts, out = iso(now_utc()), []
    ik = ["id", "title", "type", "dj", "dj_raw", "age_rating", "poster_url", "image_url", "watch_url", "source_url",
          "source_provider", "status", "reasons", "approved", "source_version", "updated_at"]
    for rid, rec in records.items():
        d = decisions[rid]
        if d.status == "excluded":  # no links, no posters: only id + status + reasons
            out.append(f"DELETE FROM source_folders WHERE item_id={lit(rid)};")
            out.append(_ins("items", {"id": rid, "status": "excluded", "reasons": ",".join(d.reasons), "approved": 0,
                                      "updated_at": ts}, ik))
            continue
        out.append(_ins("items", {"id": rid, "title": rec.get("title"), "type": rec.get("type"),
                                  "dj": normalize_dj(rec.get("dj_dubber", "")), "dj_raw": rec.get("dj_dubber"),
                                  "age_rating": rec.get("age_rating"), "poster_url": rec.get("poster_url"),
                                  "image_url": rec.get("image_url"), "watch_url": rec.get("watch_url"),
                                  "source_url": rec.get("source_url"), "source_provider": rec.get("source_provider"),
                                  "status": d.status, "reasons": ",".join(d.reasons),
                                  "approved": int(rid in approved and d.status == "source_regular"),
                                  "source_version": policy.source_version(rec), "updated_at": ts}, ik))
        for ref in extract_sources(rec)[0]:
            url = f"https://drive.google.com/drive/folders/{ref.id}" if ref.kind == "folder" else f"https://drive.google.com/file/d/{ref.id}/view"
            out.append(_ins("source_folders", {"item_id": rid, "drive_kind": ref.kind, "drive_id": ref.id, "drive_url": url},
                            ["item_id", "drive_kind", "drive_id", "drive_url"]))
    from .state import media_id
    mk = ["media_id", "item_id", "drive_file_id", "original_name", "season", "episode", "part", "hf_repo", "hf_folder",
          "hf_path", "status", "ready", "duration", "size", "updated_at"]
    for t in tasks.values():
        rid = t["item_id"]
        if rid not in records or decisions[rid].status == "excluded":
            continue
        info = t.get("info") or {}
        out.append(_ins("media", {"media_id": media_id(t["key"]), "item_id": rid, "drive_file_id": t["file_id"],
                                  "original_name": t["original_name"], "season": t["season"], "episode": t["episode"],
                                  "part": t["part"], "hf_repo": hf_repo, "hf_folder": f"videos/swamedia/{rid}/",
                                  "hf_path": t["path"], "status": t["status"], "ready": int(bool(t.get("ready"))),
                                  "duration": info.get("duration"), "size": info.get("size"), "updated_at": ts}, mk))
    # excluded items: also remove any media rows from serving
    for rid, d in decisions.items():
        if d.status == "excluded":
            out.append(f"UPDATE media SET ready=0, status='excluded' WHERE item_id={lit(rid)};")
    return out


class D1Error(RuntimeError):
    def __init__(self, status, detail=""):
        super().__init__(f"D1 error HTTP {status}" + (f": {detail}" if detail else ""))
        self.status, self.detail = status, detail


class D1Client:
    def __init__(self, account_id, database_id, api_token, session=None):
        import requests
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
        self.h, self.s = {"Authorization": f"Bearer {api_token}"}, session or requests.Session()

    def run(self, sql: str):
        r = self.s.post(self.url, headers=self.h, json={"sql": sql}, timeout=120)
        try:
            body = r.json()
        except Exception:
            body = {}
        if r.status_code >= 400 or not body.get("success", False):
            errs = body.get("errors") or []
            detail = "; ".join(f"{e.get('code')}: {str(e.get('message'))[:160]}" for e in errs[:2])
            raise D1Error(r.status_code, detail)  # code + short message only
        return body


_TARGET = re.compile(r"^(?:INSERT OR REPLACE INTO (\w+) \([^)]*\) VALUES \('?([^',)]*)|(UPDATE|DELETE)\b.*?item_id='?([^';]*))")


def _label(stmt):
    m = _TARGET.match(stmt)
    if not m:
        return "?"
    return f"{m.group(1)}:{m.group(2)}" if m.group(1) else f"{m.group(3).lower()}:{m.group(4)}"


def sync(client, statements, chunk=100, schema_path=SCHEMA, failures=None):
    """Apply statements in chunks. If a chunk fails, retry its statements one by one so a single bad row
    cannot block the rest; failed rows are appended to `failures` as (label, detail)."""
    client.run(re.sub(r"--.*", "", open(schema_path, encoding="utf-8").read()))
    for i in range(0, len(statements), chunk):
        part = statements[i:i + chunk]
        try:
            client.run("\n".join(part))
        except D1Error:
            for stmt in part:
                try:
                    client.run(stmt)
                except D1Error as e:
                    if failures is None:
                        raise
                    failures.append((_label(stmt), e.detail or f"HTTP {e.status}"))
    return len(statements)


def write_sql_file(statements, path, schema_path=SCHEMA):
    with open(path, "w", encoding="utf-8") as f:
        f.write(open(schema_path, encoding="utf-8").read() + "\n" + "\n".join(statements) + "\n")
