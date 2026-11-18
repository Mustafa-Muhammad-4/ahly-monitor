from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

try:
    import winsound
except ImportError:  # pragma: no cover
    winsound = None  # type: ignore[assignment]

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:  # pragma: no cover
    async_playwright = None  # type: ignore[assignment]

    class PlaywrightTimeoutError(TimeoutError):  # type: ignore[no-redef]
        pass

from .config import Settings
from .matcher import MatchInfo, MatchResult, detect_availability
from .telegram_client import TelegramClient

LOG = logging.getLogger("ahly_assistant")
STATE_PATH = Path("state/monitor_state.json")
RESET_CONFIRMATIONS_REQUIRED = 5

CARDS_EXTRACTOR_JS = r"""() => {
    const cards = Array.from(document.querySelectorAll('.match'));
    const data = [];
    for (const c of cards) {
        let team1 = c.querySelector('.team-name.first')?.innerText?.trim() || '';
        let team2 = c.querySelector('.team-name.second')?.innerText?.trim() || '';
        if (!team1 || !team2) {
            const names = Array.from(c.querySelectorAll('.team-name'));
            if (names.length >= 2) {
                team1 = names[0].innerText?.trim() || '';
                team2 = names[1].innerText?.trim() || '';
            }
        }
        if (!team1 && !team2) continue;
        
        const stadium = c.querySelector('.stadium .info, .stadium span, .stadium')?.innerText?.trim() || '';
        const date = c.querySelector('.when .info .first, .when .first')?.innerText?.trim() || '';
        const time = c.querySelector('.when .info .second, .when .second')?.innerText?.trim() || '';
        
        const bottomOnes = Array.from(c.querySelectorAll('.bottom .one, .bottom > div'));
        let tournament = '';
        for (const b of bottomOnes) {
            const label = b.querySelector('.first')?.innerText?.trim() || '';
            const val = b.querySelector('.second')?.innerText?.trim() || '';
            const fullText = b.innerText?.trim() || '';
            const isTournLabel = label.toLowerCase().includes('tournament') || 
                                label.toLowerCase().includes('competition') || 
                                label.toLowerCase().includes('championship') || 
                                label.toLowerCase().includes('league') || 
                                label.includes('بطولة') || 
                                label.includes('البطولة') || 
                                label.includes('المسابقة') || 
                                label.includes('الدوري');
            if (isTournLabel) {
                tournament = val || label.replace(/^(البطولة|بطولة|المسابقة|الدوري|tournament|competition|league|championship)\s*:?\s*/i, '');
            } else if (!tournament) {
                const lowerFull = fullText.toLowerCase();
                if (lowerFull.includes('tournament') || lowerFull.includes('championship') || lowerFull.includes('league') || 
                    fullText.includes('بطولة') || fullText.includes('البطولة') || fullText.includes('المسابقة') || fullText.includes('الدوري')) {
                    tournament = fullText.replace(/^(البطولة|بطولة|المسابقة|الدوري|tournament|competition|league|championship)\s*:?\s*/i, '');
                }
            }
        }
        
        const btn = c.querySelector('button, a.button, .button, [role="button"]');
        const btnText = (btn?.innerText || '').trim();
        const cardText = c.innerText || '';
        
        const isDisabled = btn?.disabled || 
                           btn?.classList?.contains('disabled') || 
                           btn?.getAttribute('aria-disabled') === 'true';
        
        const isEnded = isDisabled || 
                        btnText.includes('انتهت') || 
                        btnText.includes('نفدت') || 
                        btnText.toLowerCase().includes('ended') || 
                        btnText.toLowerCase().includes('closed') ||
                        btnText.toLowerCase().includes('sold out');
        
        const isBookable = !isEnded && (
            btnText.includes('حجز') || 
            btnText.includes('احجز') || 
            btnText.toLowerCase().includes('book') || 
            cardText.includes('متاح') || 
            cardText.toLowerCase().includes('available')
        );
        
        data.push({
            team1,
            team2,
            title: `${team1} vs ${team2}`.trim(),
            stadium,
            date,
            time,
            tournament,
            btnText,
            is_bookable: isBookable,
            status: isBookable ? 'متاح / Available' : (btnText || (isEnded ? 'انتهت / Ended' : 'غير متاح'))
        });
    }
    return data;
}"""


class AccessRestricted(RuntimeError):
    """Raised when the site asks us to stop (login, challenge, 401, 403, or 429)."""


def _default_state() -> dict:
    return {
        "active": False,
        "alerted_match_keys": {},
        "last_notified_at": None,
        "last_signature": None,
        "missing_streak": 0,
    }


def _read_state(path: Path = STATE_PATH) -> dict:
    if not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_state()
        merged = _default_state()
        merged.update(data)
        if not isinstance(merged.get("alerted_match_keys"), dict):
            merged["alerted_match_keys"] = {}
        return merged
    except (OSError, json.JSONDecodeError):
        return _default_state()


def _sync_state_to_git() -> None:
    if os.getenv("GITHUB_ACTIONS") != "true":
        return
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False, capture_output=True)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False, capture_output=True)
        subprocess.run(["git", "pull", "--rebase"], check=False, capture_output=True)
        subprocess.run(["git", "add", "state/monitor_state.json"], check=False, capture_output=True)
        res = subprocess.run(["git", "commit", "-m", "chore: sync monitor state [skip ci]"], check=False, capture_output=True)
        if res.returncode == 0:
            push_res = subprocess.run(["git", "push"], check=False, capture_output=True)
            if push_res.returncode == 0:
                LOG.info("State committed and synced to GitHub repository.")
    except Exception as exc:  # pragma: no cover
        LOG.warning("Could not sync state to git: %s", exc)


def _write_state(state: dict, path: Path = STATE_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def should_notify_and_update_state(
    state: dict,
    result: MatchResult,
    now_iso: str,
) -> tuple[bool, list[MatchInfo], dict]:
    """Determine whether to send an alert and return brand-new matches and next state."""
    alerted_keys = dict(state.get("alerted_match_keys", {}))
    missing_streak = int(state.get("missing_streak", 0))

    if result.is_open:
        # Check structured matches
        if result.matches:
            new_matches: list[MatchInfo] = []
            for match in result.matches:
                if match.match_key not in alerted_keys:
                    new_matches.append(match)
                    alerted_keys[match.match_key] = {
                        "title": match.title,
                        "sport": match.sport,
                        "date": match.date,
                        "tournament": match.tournament,
                        "notified_at": now_iso,
                    }

            if new_matches:
                next_state = {
                    "active": True,
                    "alerted_match_keys": alerted_keys,
                    "last_notified_at": now_iso,
                    "last_signature": result.signature,
                    "missing_streak": 0,
                }
                return True, new_matches, next_state

            # Matches exist but all are already alerted
            return False, [], {
                "active": True,
                "alerted_match_keys": alerted_keys,
                "last_notified_at": state.get("last_notified_at"),
                "last_signature": result.signature,
                "missing_streak": 0,
            }

        # Fallback when no structured matches (e.g. plain text match)
        sig = result.signature or "text_match"
        if sig not in alerted_keys:
            alerted_keys[sig] = {
                "title": result.matched_team or "Ahly",
                "notified_at": now_iso,
            }
            next_state = {
                "active": True,
                "alerted_match_keys": alerted_keys,
                "last_notified_at": now_iso,
                "last_signature": sig,
                "missing_streak": 0,
            }
            return True, [], next_state

        return False, [], {
            "active": True,
            "alerted_match_keys": alerted_keys,
            "last_notified_at": state.get("last_notified_at"),
            "last_signature": sig,
            "missing_streak": 0,
        }

    # Not open
    if state.get("active", False):
        next_streak = missing_streak + 1
        if next_streak >= RESET_CONFIRMATIONS_REQUIRED:
            return False, [], {
                "active": False,
                "alerted_match_keys": alerted_keys,
                "last_notified_at": state.get("last_notified_at"),
                "last_signature": None,
                "missing_streak": 0,
            }
        return False, [], {
            "active": True,
            "alerted_match_keys": alerted_keys,
            "last_notified_at": state.get("last_notified_at"),
            "last_signature": state.get("last_signature"),
            "missing_streak": next_streak,
        }

    return False, [], {
        "active": False,
        "alerted_match_keys": alerted_keys,
        "last_notified_at": state.get("last_notified_at"),
        "last_signature": None,
        "missing_streak": 0,
    }


def _looks_like_login(page_url: str, page_text: str, has_password_input: bool) -> bool:
    route = page_url.lower()
    if re.search(r"/(login|signin|sign-in)(/|$)|#/login", route):
        return True
    text = page_text.lower()
    login_words = ("sign in to your account", "تسجيل الدخول", "ادخل كلمة المرور")
    return has_password_input and any(word in text for word in login_words)


def _looks_like_interstitial(page_text: str) -> bool:
    text = (page_text or "").lower()
    interstitial_terms = (
        "checking your browser",
        "verify you are human",
        "challenges.cloudflare.com",
        "attention required! | cloudflare",
        "تحقق من أنك إنسان",
    )
    return any(term in text for term in interstitial_terms)


def _trigger_local_alerts(settings: Settings) -> None:
    if settings.open_browser_on_match:
        try:
            webbrowser.open(settings.watch_url, new=2)
        except Exception as exc:  # pragma: no cover
            LOG.warning("Could not auto-open browser: %s", exc)
    if settings.sound_alert_on_match and winsound is not None:
        try:
            for freq in (1000, 1400, 1000, 1400):
                winsound.Beep(freq, 250)
        except Exception:  # pragma: no cover
            pass


async def read_public_page(settings: Settings, page) -> tuple[str, str, list[dict]]:
    if page.url == settings.watch_url and hasattr(page, "reload"):
        response = await page.reload(wait_until="domcontentloaded", timeout=20_000)
    else:
        response = await page.goto(settings.watch_url, wait_until="domcontentloaded", timeout=20_000)
    if response is not None:
        status = response.status
        if status in (401, 403, 429):
            raise AccessRestricted(f"Website returned HTTP {status}. Monitoring stopped; do not try to bypass this response.")
        if status >= 500:
            raise RuntimeError(f"Website temporarily returned HTTP {status}.")

    await page.wait_for_timeout(1_200)
    current_url = page.url
    password_inputs = await page.locator('input[type="password"]').count()

    if settings.watch_selector:
        locator = page.locator(settings.watch_selector)
        if await locator.count() == 0:
            raise RuntimeError(f"WATCH_SELECTOR did not match any element: {settings.watch_selector}")
        text = await locator.first.inner_text(timeout=8_000)
    else:
        text = await page.locator("body").inner_text(timeout=8_000)

    if _looks_like_login(current_url, text, password_inputs > 0):
        raise AccessRestricted("The page appears to require sign-in. This tool only monitors a public page and will not log in.")

    if _looks_like_interstitial(text):
        raise AccessRestricted("The site displayed an anti-automation/interstitial page. Monitoring stopped; no bypass will be attempted.")

    if not text or not text.strip():
        raise RuntimeError("Page rendered with empty visible text; skipping state transition.")

    cards_data: list[dict] = []
    try:
        cards_data = await page.evaluate(CARDS_EXTRACTOR_JS)
    except Exception as exc:  # pragma: no cover
        LOG.debug("Cards extractor returned error: %s", exc)

    return text, current_url, cards_data


async def _send_match_alerts(
    telegram: TelegramClient,
    settings: Settings,
    matches: list[MatchInfo],
    fallback_result: MatchResult,
) -> None:
    _trigger_local_alerts(settings)

    if matches:
        for match in matches:
            text = (
                f"🚨🔔 تذاكر جديدة للأهلي على تذكرتي!\n\n"
                f"{match.sport_label}\n"
                f"🏆 البطولة: {match.tournament or 'بطولة رسمية'}\n"
                f"⚔️ المباراة: {match.title}\n"
                f"📅 الميعاد: {match.date} | {match.time}\n"
                f"🏟️ الملعب: {match.stadium}\n"
                f"🎟️ الحالة: {match.status} (متاح الحجز الآن)\n\n"
                "ادخل فوراً واضغط على الزرار بالأسفل للحجز قبل الزحمة!\n"
                f"{settings.watch_url}"
            )
            await telegram.send(
                text,
                button_text="🎟️ افتح صفحة المباريات الآن",
                button_url=settings.watch_url,
                pin_loudly=True,
            )
            for idx in range(1, 4):
                try:
                    await asyncio.sleep(1.2)
                    await telegram.send(
                        f"🚨🚨 تنبيه ({idx}/3): تذاكر {match.title} ({match.sport_label}) نزلت دلوقتي! الحق احجز!",
                        button_text="🎟️ افتح صفحة المباريات الآن",
                        button_url=settings.watch_url,
                    )
                except Exception as exc:  # pragma: no cover
                    LOG.warning("Follow-up alert (%d/3) skipped due to network/rate-limit: %s", idx, exc)
    else:
        await telegram.send(
            f"🚨🔔 عاجل: ظهور مباراة {settings.match_label} على تذكرتي!\n\n"
            f"الكلمة المطابقة: {fallback_result.matched_team}\n"
            f"مؤشر الإتاحة: {fallback_result.matched_open or 'غير مطلوب في الإعدادات'}\n"
            "ادخل فوراً واضغط على الزرار بالأسفل للحجز قبل الزحمة!\n"
            f"{settings.watch_url}",
            button_text="🎟️ افتح صفحة تذكرتي الآن",
            button_url=settings.watch_url,
            pin_loudly=True,
        )


async def run_monitor(settings: Settings, telegram: TelegramClient, check_once: bool = False) -> None:
    state = _read_state()

    filter_desc = "كرة قدم فقط ⚽" if settings.football_only else "جميع الرياضات (قدم، سلة، طائرة، يد)"
    LOG.info(
        "Ahly Ticket Assistant started. Watching: %s | Target: %s | Filter: %s | Interval: %ss",
        settings.watch_url,
        settings.match_label,
        filter_desc,
        settings.poll_interval_seconds,
    )

    start_time = asyncio.get_running_loop().time()
    max_duration_seconds = (settings.run_duration_minutes * 60) if settings.run_duration_minutes else None

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(locale="ar-EG")
        await context.route("**/*", lambda route: route.abort() if route.request.resource_type in {"image", "media", "font"} else route.continue_())
        page = await context.new_page()
        iteration = 0
        try:
            while True:
                iteration += 1
                started = asyncio.get_running_loop().time()

                # Browser context recycling every 400 checks to prevent memory leaks
                if iteration % 400 == 0:
                    LOG.info("Recycling browser page context (iteration %d) to keep memory lean...", iteration)
                    await page.close()
                    await context.close()
                    context = await browser.new_context(locale="ar-EG")
                    await context.route("**/*", lambda route: route.abort() if route.request.resource_type in {"image", "media", "font"} else route.continue_())
                    page = await context.new_page()

                try:
                    page_text, current_url, cards_data = await read_public_page(settings, page)
                    result = detect_availability(
                        page_text,
                        settings.team_keywords,
                        settings.open_keywords,
                        settings.require_open_keyword,
                        cards_data=cards_data,
                        football_only=settings.football_only,
                    )
                    LOG.info("Page checked. %s | URL: %s", result.reason, current_url)

                    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    should_notify, new_matches, next_state = should_notify_and_update_state(state, result, now)

                    if should_notify:
                        await _send_match_alerts(telegram, settings, new_matches, result)
                        state = next_state
                        _write_state(state)
                        _sync_state_to_git()
                    elif next_state != state:
                        state = next_state
                        _write_state(state)

                except AccessRestricted as exc:
                    LOG.error("Monitoring stopped: %s", exc)
                    await telegram.send(f"⛔ تم إيقاف مراقبة Ahly Ticket Assistant.\nالسبب: {exc}")
                    break
                except PlaywrightTimeoutError:
                    LOG.warning("Timed out while loading/reading the page.")
                except Exception as exc:
                    LOG.exception("Temporary monitoring error: %s", exc)

                if check_once:
                    break

                # Self-relay check: if max_duration reached, exit cleanly so the next relay starts
                if max_duration_seconds is not None:
                    total_elapsed = asyncio.get_running_loop().time() - start_time
                    if total_elapsed >= max_duration_seconds:
                        LOG.info("Reached planned duration (%d min). Clean handoff to next runner.", settings.run_duration_minutes)
                        break

                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(max(0.5, settings.poll_interval_seconds - elapsed))
        finally:
            await context.close()
            await browser.close()
