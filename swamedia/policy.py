"""Adult-content policy: FAIL CLOSED. Exclusion always beats approval."""
from __future__ import annotations
import hashlib, json, re
from dataclasses import dataclass, field
from urllib.parse import urlparse

ALLOWED_TYPES = {"movie", "series"}
SAFE_RATINGS = {"all", "g", "7+", "10+", "12+", "13+", "14+", "15+", "16+"}
ADULT_RE = re.compile(
    r"(?<![a-z0-9])(?:adults?|wakubwa|porn\w*|xxx|nsfw|erotic\w*|hentai|1[89]\s*\+|2[01]\s*\+)(?![a-z0-9])",
    re.I,
)
_FLAG_KEYS = ("is_adult", "adult", "adult_flag", "is_18", "nsfw", "mature")
_CAT_KEYS = ("category", "categories", "genre", "classification")
_VERSION_KEYS = ("title", "type", "dj_dubber", "age_rating", "genre", "video_url", "video_url_2",
                 "watch_url", "source_url", "download_url", "download_url_2", "download_url_3", "updated_at")


@dataclass
class Decision:
    status: str  # excluded | needs_review | source_regular
    reasons: list = field(default_factory=list)


def _flat(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [s for x in v for s in _flat(x)]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _flat(x)]
    return [str(v)]


def _url_text(url: str) -> str:
    p = urlparse(url or "")
    return re.sub(r"[/_\-.=&?]", " ", p.path + " " + p.query)


def has_adult_marker(text: str) -> bool:
    return bool(ADULT_RE.search(text or ""))


def _rating_num(r: str):
    m = re.fullmatch(r"\s*(\d{1,2})\s*\+\s*", r or "")
    return int(m.group(1)) if m else None


def classify(rec: dict) -> Decision:
    """Inspect ONLY the record's own metadata (never site navigation)."""
    reasons, excluded = [], False
    rtype = str(rec.get("type", "")).strip().lower()
    if rtype not in ALLOWED_TYPES:
        return Decision("excluded", [f"type_not_allowed:{rtype or 'empty'}"])
    for k in _FLAG_KEYS:
        if str(rec.get(k, "")).strip().lower() in {"1", "true", "yes", "y"}:
            excluded = True; reasons.append(f"adult_flag:{k}")
    for k in _CAT_KEYS + ("age_rating", "age_ratings", "title"):
        for s in _flat(rec.get(k)):
            if has_adult_marker(s):
                excluded = True; reasons.append(f"adult_marker:{k}")
    for k in ("watch_url", "source_url", "video_url", "video_url_2"):
        if has_adult_marker(_url_text(str(rec.get(k, "")))):
            excluded = True; reasons.append(f"adult_marker_url:{k}")
    ratings = [r.strip().lower() for k in ("age_rating", "age_ratings") for r in _flat(rec.get(k)) if r.strip()]
    if any((_rating_num(r) or 0) >= 18 for r in ratings):
        excluded = True; reasons.append("age_rating_18_plus")
    if excluded:
        return Decision("excluded", sorted(set(reasons)))
    if not ratings:
        return Decision("needs_review", ["age_rating_missing"])
    if len(set(ratings)) > 1:
        return Decision("needs_review", ["age_rating_conflict"])
    if ratings[0] not in SAFE_RATINGS:
        return Decision("needs_review", [f"age_rating_unrecognized:{ratings[0]}"])
    return Decision("source_regular", [])


def source_version(rec: dict) -> str:
    snap = {k: rec.get(k) for k in _VERSION_KEYS}
    return hashlib.sha256(json.dumps(snap, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def approved_ids(records: dict, approvals: dict) -> tuple[set, list]:
    """records: id->record. Returns (approved ids, problems). No force-include for excluded."""
    ok, problems = set(), []
    for a in approvals.get("items", []):
        rid = str(a.get("id"))
        rec = records.get(rid)
        if rec is None:
            problems.append((rid, "unknown_item")); continue
        if classify(rec).status != "source_regular":
            problems.append((rid, "not_source_regular")); continue
        if a.get("source_version") != source_version(rec):
            problems.append((rid, "source_changed_back_to_review")); continue
        if not (a.get("approved_by") and a.get("approved_at")):
            problems.append((rid, "missing_approver")); continue
        ok.add(rid)
    return ok, problems
