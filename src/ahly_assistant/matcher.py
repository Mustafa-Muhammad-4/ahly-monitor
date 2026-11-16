from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass


_ARABIC_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_BLOCK_SPLIT = re.compile(r"\n\s*\n+")


def normalize_text(value: str) -> str:
    """Normalize Arabic/Latin text enough for robust keyword matching."""
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = _ARABIC_DIACRITICS.sub("", value)
    value = value.replace("ـ", "")
    value = value.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه"}))
    return re.sub(r"\s+", " ", value).strip()


@dataclass(frozen=True)
class MatchResult:
    is_open: bool
    matched_team: str | None = None
    matched_open: str | None = None
    reason: str = ""
    signature: str | None = None


def _compute_signature(matched_team: str, matched_open: str | None, context_text: str) -> str:
    normalized_context = normalize_text(context_text)
    payload = f"{normalize_text(matched_team)}|{normalize_text(matched_open or '')}|{normalized_context}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def detect_availability(
    page_text: str,
    team_keywords: tuple[str, ...],
    open_keywords: tuple[str, ...],
    require_open_keyword: bool,
) -> MatchResult:
    raw_blocks = [blk.strip() for blk in _BLOCK_SPLIT.split(page_text or "") if blk.strip()]
    normalized_page = normalize_text(page_text)

    team_hit = next((kw for kw in team_keywords if normalize_text(kw) in normalized_page), None)
    if not team_hit:
        return MatchResult(False, reason="No team keyword found")

    if require_open_keyword:
        # When multiple match blocks exist, require the team and availability phrase in the same block.
        blocks_to_check = raw_blocks if len(raw_blocks) > 1 else [page_text or ""]
        for block in blocks_to_check:
            norm_block = normalize_text(block)
            blk_team = next((kw for kw in team_keywords if normalize_text(kw) in norm_block), None)
            if not blk_team:
                continue
            blk_open = next((kw for kw in open_keywords if normalize_text(kw) in norm_block), None)
            if blk_open:
                return MatchResult(
                    True,
                    matched_team=blk_team,
                    matched_open=blk_open,
                    reason=f"Team keyword found + availability phrase: {blk_open}",
                    signature=_compute_signature(blk_team, blk_open, block),
                )
        return MatchResult(False, matched_team=team_hit, reason="Team found, but no availability phrase")

    open_hit = next((kw for kw in open_keywords if normalize_text(kw) in normalized_page), None)
    matched_block = next(
        (blk for blk in raw_blocks if normalize_text(team_hit) in normalize_text(blk)),
        page_text or "",
    )
    return MatchResult(
        True,
        matched_team=team_hit,
        matched_open=open_hit,
        reason="Team keyword found" + (f" + availability phrase: {open_hit}" if open_hit else ""),
        signature=_compute_signature(team_hit, open_hit, matched_block),
    )
