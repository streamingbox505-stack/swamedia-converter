"""Google Drive: URL parsing, recursive paginated traversal, resilient download."""
from __future__ import annotations
import hashlib, os, re, time
from dataclasses import dataclass, field
from urllib.parse import urlparse, parse_qs

from .policy import has_adult_marker

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
MEDIA_EXT = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp"}
BAD_EXT = {".zip", ".rar", ".7z", ".apk", ".exe", ".jpg", ".jpeg", ".png", ".webp", ".srt", ".vtt", ".ass", ".txt", ".pdf"}
FIELDS = "nextPageToken,files(id,name,mimeType,size,modifiedTime,md5Checksum,shortcutDetails,resourceKey)"


class DriveError(Exception):
    def __init__(self, kind: str, msg: str = "", status: int | None = None):
        super().__init__(f"{kind}: {msg}")
        self.kind, self.status = kind, status  # kind: permission | quota | not_found | network | html | size | checksum


@dataclass(frozen=True)
class DriveRef:
    kind: str  # file | folder
    id: str
    resource_key: str | None = None


def parse_drive_url(url: str) -> DriveRef | None:
    u = urlparse((url or "").strip())
    if u.hostname not in {"drive.google.com", "docs.google.com", "drive.usercontent.google.com"}:
        return None
    q = parse_qs(u.query)
    rk = (q.get("resourcekey") or [None])[0]
    m = re.search(r"/folders/([\w-]+)", u.path)
    if m:
        return DriveRef("folder", m.group(1), rk)
    m = re.search(r"/file/d/([\w-]+)", u.path)
    if m:
        return DriveRef("file", m.group(1), rk)
    if u.path in {"/open", "/uc", "/download"} and q.get("id"):
        return DriveRef("file", q["id"][0], rk)  # /open?id= can be a folder; get_meta() resolves it
    return None


@dataclass
class DriveFile:
    id: str
    name: str
    mime: str
    size: int | None
    modified: str
    md5: str | None
    resource_key: str | None
    trail: tuple
    status: str = "candidate"  # candidate | excluded | skipped
    reason: str = ""

    @property
    def source_version(self) -> str:
        return self.md5 or f"{self.modified}:{self.size}"


def is_media_candidate(name: str, mime: str) -> bool:
    ext = os.path.splitext(name or "")[1].lower()
    if mime.startswith("application/vnd.google-apps"):
        return False
    if ext in BAD_EXT:
        return False
    return mime.startswith("video/") or ext in MEDIA_EXT


def walk_folder(root: DriveRef, list_page, *, follow_shortcuts=False, max_depth=25):
    """list_page(folder_id, page_token, resource_key) -> {'files': [...], 'nextPageToken': ...}.
    Returns (files, issues). Follows nextPageToken to the end, recurses, guards cycles."""
    files, issues, visited = {}, [], set()
    stack = [(root.id, root.resource_key, (), 0)]
    while stack:
        fid, rk, trail, depth = stack.pop()
        if fid in visited:
            issues.append({"type": "cycle_or_repeat", "folder_id": fid}); continue
        visited.add(fid)
        if depth > max_depth:
            issues.append({"type": "max_depth", "folder_id": fid}); continue
        token = None
        while True:
            try:
                page = list_page(fid, token, rk)
            except DriveError as e:
                issues.append({"type": e.kind if e.kind in {"permission", "quota", "not_found"} else "error",
                               "folder_id": fid, "detail": str(e)}); break
            for f in page.get("files", []):
                name, mime = f.get("name", ""), f.get("mimeType", "")
                if mime == SHORTCUT_MIME:
                    sd = f.get("shortcutDetails") or {}
                    if not follow_shortcuts or not sd.get("targetId"):
                        issues.append({"type": "shortcut_skipped", "id": f.get("id"), "name": name}); continue
                    f = {**f, "id": sd["targetId"], "mimeType": sd.get("targetMimeType", ""), "resourceKey": sd.get("targetResourceKey")}
                    mime = f["mimeType"]
                if mime == FOLDER_MIME:
                    stack.append((f["id"], f.get("resourceKey"), trail + (name,), depth + 1)); continue
                if not is_media_candidate(name, mime):
                    issues.append({"type": "not_media", "id": f.get("id"), "name": name}); continue
                df = DriveFile(f["id"], name, mime, int(f["size"]) if f.get("size") else None, f.get("modifiedTime", ""),
                               f.get("md5Checksum"), f.get("resourceKey"), trail)
                if has_adult_marker(name) or any(has_adult_marker(t) for t in trail):
                    df.status, df.reason = "excluded", "adult_marker_in_name_or_path"
                files.setdefault(df.id, df)  # same file id twice = one task
            token = page.get("nextPageToken")
            if not token:
                break
    return list(files.values()), issues


class DriveClient:
    """Thin REST client (Drive API v3). `token_fn()` returns a fresh OAuth access token; never log it."""
    API = "https://www.googleapis.com/drive/v3"

    def __init__(self, token_fn, session=None, sleep=time.sleep):
        import requests
        self.token_fn, self.s, self.sleep = token_fn, session or requests.Session(), sleep

    def _headers(self, fid=None, rk=None):
        h = {"Authorization": f"Bearer {self.token_fn()}"}
        if fid and rk:
            h["X-Goog-Drive-Resource-Keys"] = f"{fid}/{rk}"
        return h

    @staticmethod
    def _raise(r):
        if r.status_code in (401, 403):
            body = (r.text or "")[:400].lower()
            kind = "quota" if any(x in body for x in ("quota", "ratelimit", "userratelimit")) else "permission"
            raise DriveError(kind, f"HTTP {r.status_code}", r.status_code)
        if r.status_code == 404:
            raise DriveError("not_found", "HTTP 404", 404)
        if r.status_code == 429 or r.status_code >= 500:
            e = DriveError("network", f"HTTP {r.status_code}", r.status_code)
            e.retry_after = r.headers.get("Retry-After")
            raise e
        r.raise_for_status()

    def _get(self, url, params, fid=None, rk=None, retries=5, **kw):
        for attempt in range(retries):
            try:
                r = self.s.get(url, params=params, headers=self._headers(fid, rk), timeout=60, **kw)
                self._raise(r)
                return r
            except DriveError as e:
                if e.kind not in {"network", "quota"} or attempt == retries - 1:
                    raise
                ra = getattr(e, "retry_after", None)
                self.sleep(float(ra) if ra and str(ra).isdigit() else min(2 ** attempt * 2, 60))
            except Exception as e:  # connection errors
                if attempt == retries - 1:
                    raise DriveError("network", type(e).__name__)
                self.sleep(min(2 ** attempt * 2, 60))

    def list_page(self, folder_id, page_token=None, resource_key=None):
        p = {"q": f"'{folder_id}' in parents and trashed=false", "fields": FIELDS, "pageSize": 1000,
             "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if page_token:
            p["pageToken"] = page_token
        return self._get(f"{self.API}/files", p, folder_id, resource_key).json()

    def get_meta(self, file_id, resource_key=None):
        p = {"fields": "id,name,mimeType,size,modifiedTime,md5Checksum,resourceKey", "supportsAllDrives": "true"}
        return self._get(f"{self.API}/files/{file_id}", p, file_id, resource_key).json()

    def download(self, f: DriveFile, dest: str, min_free_bytes=2 * 1024**3, retries=5):
        """Stream to disk; verifies HTML-vs-video, size and md5 when known."""
        import shutil
        need = (f.size or 0) + min_free_bytes
        if shutil.disk_usage(os.path.dirname(dest) or ".").free < need:
            raise DriveError("size", "disk guard: not enough free space")
        for attempt in range(retries):
            try:
                r = self._get(f"{self.API}/files/{f.id}", {"alt": "media", "supportsAllDrives": "true"}, f.id,
                              f.resource_key, stream=True, retries=1)
                if "text/html" in r.headers.get("Content-Type", ""):
                    raise DriveError("html", "got HTML instead of video")
                h, n = hashlib.md5(), 0
                with open(dest, "wb") as out:
                    for chunk in r.iter_content(1 << 20):
                        out.write(chunk); h.update(chunk); n += len(chunk)
                if f.size is not None and n != f.size:
                    raise DriveError("size", f"{n} != {f.size}")
                if f.md5 and h.hexdigest() != f.md5:
                    raise DriveError("checksum", "md5 mismatch")
                return dest
            except DriveError as e:
                if e.kind in {"permission", "not_found", "html"} or attempt == retries - 1:
                    raise
                self.sleep(min(2 ** attempt * 5, 120))
