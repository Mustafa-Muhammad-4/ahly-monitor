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


def detect_sport(tournament: str, context: str = "") -> tuple[str, str]:
    """Detect sport from tournament name or context in Arabic and English."""
    combined = normalize_text(f"{tournament} {context}")
    if any(k in combined for k in ("basket", "سله")):
        return "basketball", "🏀 كرة سلة (Basketball)"
    if any(k in combined for k in ("volley", "طايره", "طائره")):
        return "volleyball", "🏐 كرة طائرة (Volleyball)"
    handball_terms = ("handball", "كره يد", "كرة يد", "كره اليد", "كرة اليد", "دوري اليد", "دورى اليد", "كاس اليد", "كأس اليد")
    if any(k in combined for k in handball_terms) or re.search(r"\bيد\b", combined):
        return "handball", "🤾 كرة يد (Handball)"
    return "football", "⚽ كرة قدم (Football)"


@dataclass(frozen=True)
class MatchInfo:
    team1: str
    team2: str
    title: str
    tournament: str
    sport: str
    sport_label: str
    date: str
    time: str
    stadium: str
    status: str
    is_bookable: bool
    match_key: str


@dataclass(frozen=True)
class MatchResult:
    is_open: bool
    matched_team: str | None = None
    matched_open: str | None = None
    reason: str = ""
    signature: str | None = None
    matches: tuple[MatchInfo, ...] = ()


def _compute_signature(matched_team: str, matched_open: str | None, context_text: str) -> str:
    normalized_context = normalize_text(context_text)
    payload = f"{normalize_text(matched_team)}|{normalize_text(matched_open or '')}|{normalized_context}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def parse_match_card(raw: dict) -> MatchInfo:
    team1 = raw.get("team1", "").strip()
    team2 = raw.get("team2", "").strip()
    title = raw.get("title", f"{team1} vs {team2}").strip()
    tournament = raw.get("tournament", "").strip()
    date = raw.get("date", "").strip()
    time = raw.get("time", "").strip()
    stadium = raw.get("stadium", "").strip()
    status = raw.get("status", raw.get("btnText", "")).strip() or "Available"
    is_bookable = bool(raw.get("is_bookable", True))

    sport, sport_label = detect_sport(tournament, title)
    norm_key = f"{sport}|{normalize_text(team1)}_{normalize_text(team2)}|{normalize_text(date)}|{normalize_text(tournament)}"
    match_key = hashlib.sha256(norm_key.encode("utf-8")).hexdigest()[:16]

    return MatchInfo(
        team1=team1,
        team2=team2,
        title=title,
        tournament=tournament,
        sport=sport,
        sport_label=sport_label,
        date=date,
        time=time,
        stadium=stadium,
        status=status,
        is_bookable=is_bookable,
        match_key=match_key,
    )


def detect_matches_from_cards(
    cards_data: list[dict],
    team_keywords: tuple[str, ...],
    football_only: bool = False,
    require_bookable: bool = True,
) -> list[MatchInfo]:
    """Filter and parse structured match cards for target teams."""
    matched: list[MatchInfo] = []
    norm_keywords = [normalize_text(kw) for kw in team_keywords]

    for raw in cards_data:
        info = parse_match_card(raw)
        norm_teams = normalize_text(f"{info.team1} {info.team2}")
        team_matched = any(kw in norm_teams for kw in norm_keywords)
        if not team_matched:
            continue

        if football_only and info.sport != "football":
            continue

        if require_bookable and not info.is_bookable:
            continue

        matched.append(info)

    return matched


def detect_availability(
    page_text: str,
    team_keywords: tuple[str, ...],
    open_keywords: tuple[str, ...],
    require_open_keyword: bool,
    cards_data: list[dict] | None = None,
    football_only: bool = False,
) -> MatchResult:
    # If structured cards are present, use the high-fidelity parser
    if cards_data:
        matched_cards = detect_matches_from_cards(
            cards_data,
            team_keywords=team_keywords,
            football_only=football_only,
            require_bookable=True,  # always require bookable for card-based detection
        )
        if matched_cards:
            primary = matched_cards[0]
            return MatchResult(
                is_open=True,
                matched_team=primary.title,
                matched_open=primary.status,
                reason=f"Found {len(matched_cards)} match(es) for {primary.title} ({primary.sport_label})",
                signature=primary.match_key,
                matches=tuple(matched_cards),
            )
        # If no target card is available or bookable
        return MatchResult(False, reason="No matching bookable match card found")

    # Fallback for plain text matching
    raw_blocks = [blk.strip() for blk in _BLOCK_SPLIT.split(page_text or "") if blk.strip()]
    normalized_page = normalize_text(page_text)

    team_hit = next((kw for kw in team_keywords if normalize_text(kw) in normalized_page), None)
    if not team_hit:
        return MatchResult(False, reason="No team keyword found")

    if require_open_keyword:
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
