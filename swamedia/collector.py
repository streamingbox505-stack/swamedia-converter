"""Collect movie/series list from get_movies.php JSON (pagination, cache, dedupe, allowlist)."""
from __future__ import annotations
import json, os
from urllib.parse import urlparse, parse_qs, unquote

from .drive import parse_drive_url, DriveRef

API_HOST = "swamedia.co.tz"
DRIVE_HOSTS = {"drive.google.com", "docs.google.com", "drive.usercontent.google.com"}
ALLOWED_HOSTS = DRIVE_HOSTS | {API_HOST}
SOURCE_FIELDS = ("video_url", "video_url_2", "watch_url", "source_url", "download_url", "download_url_2", "download_url_3")
WRAP_PARAMS = ("url", "u", "link", "file", "target", "redirect")


def api_url(kind: str, page: int, limit: int = 48) -> str:
    return f"https://{API_HOST}/api/get_movies.php?page={page}&limit={limit}&type={kind}&sort=newest&dj="


def fetch_json(url: str, session=None) -> dict:
    """Allowlisted GET; only the SwaMedia list API is ever requested."""
    if urlparse(url).hostname != API_HOST or not urlparse(url).path.startswith("/api/get_movies.php"):
        raise ValueError("URL not allowlisted")
    import requests
    r = (session or requests).get(url, timeout=60, headers={"User-Agent": "swamedia-converter/1.0"})
    r.raise_for_status()
    return r.json()


class PageCache:
    def __init__(self, directory: str | None):
        self.dir = directory
        if directory:
            os.makedirs(directory, exist_ok=True)

    def get(self, kind, page):
        p = self.dir and os.path.join(self.dir, f"{kind}-{page}.json")
        return json.load(open(p, encoding="utf-8")) if p and os.path.exists(p) else None

    def put(self, kind, page, data):
        if self.dir:
            json.dump(data, open(os.path.join(self.dir, f"{kind}-{page}.json"), "w", encoding="utf-8"), ensure_ascii=False)


def crawl_kind(kind: str, fetch_page, cache: PageCache | None = None, max_pages: int = 3000):
    """fetch_page(kind, page) -> dict. Stops on empty page / hasMore=false / repeated page; never on `total` alone."""
    cache = cache or PageCache(None)
    recs, warn, sigs = {}, [], set()
    stats = {"kind": kind, "reported_total": None, "pages": 0, "duplicates": 0, "missing_id": 0}
    for page in range(1, max_pages + 1):
        data = cache.get(kind, page)
        if data is None:
            data = fetch_page(kind, page)
            if data.get("success", True):
                cache.put(kind, page, data)
        if not data.get("success", True):
            warn.append(f"{kind}: page {page} success=false"); break
        if data.get("total") is not None:
            stats["reported_total"] = data["total"]
        items = data.get("movies") or []
        if not items:
            break
        stats["pages"] += 1
        sig = tuple(str(m.get("id")) for m in items)
        if sig in sigs:
            warn.append(f"{kind}: page {page} repeats an earlier page; stopping"); break
        sigs.add(sig)
        for m in items:
            rid = m.get("id")
            if rid in (None, ""):
                stats["missing_id"] += 1; continue
            rid = str(rid)
            if rid in recs:
                stats["duplicates"] += 1
                if recs[rid] != m:
                    warn.append(f"{kind}: id {rid} duplicated with different metadata")
                continue
            recs[rid] = m
        if data.get("hasMore") is False:
            break
    else:
        warn.append(f"{kind}: max_pages reached")
    stats["collected"] = len(recs)
    if stats["reported_total"] is not None and stats["collected"] != stats["reported_total"]:
        warn.append(f"{kind}: collected {stats['collected']} but API reports total {stats['reported_total']} "
                    f"(gap {stats['reported_total'] - stats['collected']}); do NOT claim 'all collected'")
    return recs, stats, warn


def unwrap_url(url: str) -> str:
    """Decode wrapped download_url (?url=...) only when the inner target is allowlisted."""
    u = urlparse(url or "")
    q = parse_qs(u.query)
    for k in WRAP_PARAMS:
        if q.get(k):
            inner = unquote(q[k][0])
            if urlparse(inner).hostname in ALLOWED_HOSTS:
                return inner
    return url or ""


def extract_sources(rec: dict):
    """Returns (drive_refs, unsupported). trailer_url is never read."""
    refs, unsupported, seen = [], [], set()
    for k in SOURCE_FIELDS:
        raw = rec.get(k)
        if not raw:
            continue
        url = unwrap_url(str(raw))
        host = urlparse(url).hostname
        ref = parse_drive_url(url)
        if ref:
            if (ref.kind, ref.id) not in seen:
                seen.add((ref.kind, ref.id)); refs.append(ref)
        elif host != API_HOST:  # own site pages (watch_url) are not media sources
            unsupported.append({"field": k, "host": host or "invalid", "provider": rec.get("source_provider")})
    return refs, unsupported


def folder_conflicts(records: dict) -> list[dict]:
    """Same Drive folder referenced by several items with different title/DJ => review conflict."""
    by = {}
    for rid, rec in records.items():
        for ref in extract_sources(rec)[0]:
            by.setdefault((ref.kind, ref.id), []).append(rid)
    out = []
    for (kind, fid), ids in by.items():
        if len(set(ids)) > 1:
            ids = sorted(set(ids))
            sigs = {(records[i].get("title"), records[i].get("dj_dubber")) for i in ids}
            if len(sigs) > 1:
                out.append({"drive_kind": kind, "drive_id": fid, "item_ids": ids, "reason": "shared_source_different_title_or_dj"})
    return out
