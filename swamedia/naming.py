"""DJ normalisation and episode/season/part parsing. Never guesses."""
from __future__ import annotations
import os, re
from dataclasses import dataclass


def normalize_dj(raw: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "").strip())
    low = re.sub(r"^dj\s*", "", s.lower()).strip()
    if re.fullmatch(r"bla(?:ck|k)[iy]?", low):
        return "DJ Black"
    if low.startswith("mack"):
        return "DJ Mack"
    return s


@dataclass
class EpisodeInfo:
    season: int | None = None
    episode: int | None = None
    part: int | None = None
    status: str = "ok"  # ok | needs_review
    reason: str = ""


_NOISE = re.compile(r"\b(?:(?:19|20)\d{2}|\d{3,4}p|x26[45]|h\.?26[45]|hevc|hdrip|webrip|web-dl|bluray|aac|mp4|mkv)\b", re.I)
_SE = re.compile(r"(?<![a-z])s(\d{1,2})\s*[ ._-]?\s*e(\d{1,4})(?!\d)", re.I)
_EP = re.compile(r"(?<![a-z])(?:episode|episodi|kipindi|ep|e)\s*[ ._#-]?\s*(\d{1,4})(?!\d)", re.I)
_PART = re.compile(r"(?<![a-z])(?:part|pt|sehemu)\s*[ ._#-]?\s*(\d{1,2})(?!\d)", re.I)
_RANGE = re.compile(r"(?<!\d)(\d{1,4})\s*[-–]\s*(\d{1,4})(?!\d)")


def parse_episode(filename: str) -> EpisodeInfo:
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    s = _NOISE.sub(" ", stem)
    m = _SE.search(s)
    info = EpisodeInfo()
    if m:
        info.season, info.episode = int(m.group(1)), int(m.group(2))
        s = s[:m.start()] + " " + s[m.end():]
    else:
        eps = {int(x) for x in _EP.findall(s)}
        if len(eps) > 1:
            return EpisodeInfo(status="needs_review", reason="ambiguous_multiple_episode_numbers")
        if eps:
            info.episode = eps.pop()
            s = _EP.sub(" ", s)
    parts = {int(x) for x in _PART.findall(s)}
    if len(parts) > 1:
        return EpisodeInfo(status="needs_review", reason="ambiguous_multiple_parts")
    if parts:
        info.part = parts.pop()
    if info.episode is None and info.part is None:
        if _RANGE.search(s):
            return EpisodeInfo(status="needs_review", reason="ambiguous_range")
        if re.fullmatch(r"\s*\d{1,4}\s*", s):
            info.episode = int(s)
        else:
            return EpisodeInfo(status="needs_review", reason="no_explicit_number")
    return info


def sort_key(e: EpisodeInfo, name: str = ""):
    return (e.season or 0, e.episode if e.episode is not None else 10**9, e.part or 0, name)


def find_conflicts(entries: list[dict]) -> list[dict]:
    """entries: {file_id, episode: EpisodeInfo}. Same file id twice = one task; same number, two ids = conflict."""
    seen, groups = set(), {}
    for e in entries:
        if e["file_id"] in seen:
            continue
        seen.add(e["file_id"])
        ep = e["episode"]
        if ep.status != "ok" or ep.episode is None:
            continue
        groups.setdefault((ep.season or 0, ep.episode, ep.part or 0), []).append(e["file_id"])
    return [{"key": list(k), "file_ids": v, "reason": "duplicate_episode_number"} for k, v in groups.items() if len(v) > 1]
