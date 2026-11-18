from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv() -> bool:
        env_path = Path(".env")
        if not env_path.exists():
            return False
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, val = stripped.partition("=")
            os.environ.setdefault(key.strip(), val.strip())
        return True


DEFAULT_TEAMS = (
    "الأهلي",
    "الاهلي",
    "الأهلى",
    "الاهلى",
    "النادي الأهلي",
    "النادى الاهلى",
    "Al Ahly",
    "Al-Ahly",
    "AlAhly",
    "Al Ahli",
    "Ahly",
    "منتخب مصر",
    "مصر",
    "Egypt",
    "Egyptian National Team",
    "الفراعنة",
    "فراعنة",
    "The Pharaohs",
    "Pharaohs",
)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = field(repr=False)
    telegram_chat_id: str = field(repr=False)
    watch_url: str = "https://tazkarti.com/#/matches"
    team_keywords: tuple[str, ...] = DEFAULT_TEAMS
    open_keywords: tuple[str, ...] = ("احجز الآن", "Book now", "Available", "Book Ticket")
    require_open_keyword: bool = False
    poll_interval_seconds: int = 2
    watch_selector: str | None = None
    match_label: str = "الأهلي / منتخب مصر"
    open_browser_on_match: bool = False
    sound_alert_on_match: bool = False
    football_only: bool = True
    run_duration_minutes: int | None = None


def _csv_env(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, default).split(",") if item.strip())


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    watch_url = os.getenv("WATCH_URL", "https://tazkarti.com/#/matches").strip()
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "2"))
    parsed = urlparse(watch_url)

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and add your bot token.")
    if not chat_id:
        raise ValueError("TELEGRAM_CHAT_ID is missing. Run tools/find_chat_id.py first.")
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("WATCH_URL must be a valid HTTPS URL.")
    if poll_interval < 2:
        raise ValueError("POLL_INTERVAL_SECONDS must be at least 2 seconds.")

    team_keywords_env = os.getenv("TEAM_KEYWORDS", "")
    if team_keywords_env.strip():
        team_keywords = _csv_env("TEAM_KEYWORDS")
    else:
        team_keywords = DEFAULT_TEAMS

    duration_str = os.getenv("RUN_DURATION_MINUTES", "").strip()
    run_duration_minutes = int(duration_str) if duration_str.isdigit() and int(duration_str) > 0 else None

    return Settings(
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        watch_url=watch_url,
        team_keywords=team_keywords,
        open_keywords=_csv_env("OPEN_KEYWORDS", "احجز الآن,Book now,Available,Book Ticket"),
        require_open_keyword=os.getenv("REQUIRE_OPEN_KEYWORD", "false").lower() in {"1", "true", "yes"},
        poll_interval_seconds=poll_interval,
        watch_selector=os.getenv("WATCH_SELECTOR", "").strip() or None,
        match_label=os.getenv("MATCH_LABEL", "الأهلي / منتخب مصر").strip() or "الأهلي / منتخب مصر",
        open_browser_on_match=os.getenv("OPEN_BROWSER_ON_MATCH", "false").lower() in {"1", "true", "yes"},
        sound_alert_on_match=os.getenv("SOUND_ALERT_ON_MATCH", "false").lower() in {"1", "true", "yes"},
        football_only=os.getenv("FOOTBALL_ONLY", "true").lower() in {"1", "true", "yes"},
        run_duration_minutes=run_duration_minutes,
    )
