from __future__ import annotations

import asyncio
import json
import logging
import re
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
from .matcher import MatchResult, detect_availability
from .telegram_client import TelegramClient

LOG = logging.getLogger("ahly_assistant")
STATE_PATH = Path("state/monitor_state.json")
RESET_CONFIRMATIONS_REQUIRED = 2


class AccessRestricted(RuntimeError):
    """Raised when the site asks us to stop (login, challenge, 401, 403, or 429)."""


def _default_state() -> dict:
    return {
        "active": False,
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
        return merged
    except (OSError, json.JSONDecodeError):
        return _default_state()


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
) -> tuple[bool, dict]:
    """Determine whether to send a Telegram alert and compute the next state."""
    active = bool(state.get("active", False))
    last_notified_at = state.get("last_notified_at")
    last_signature = state.get("last_signature")
    missing_streak = int(state.get("missing_streak", 0))

    if result.is_open:
        is_new_match = not active or (
            result.signature is not None
            and last_signature is not None
            and result.signature != last_signature
        )
        if is_new_match:
            return True, {
                "active": True,
                "last_notified_at": now_iso,
                "last_signature": result.signature,
                "missing_streak": 0,
            }
        return False, {
            "active": True,
            "last_notified_at": last_notified_at,
            "last_signature": result.signature or last_signature,
            "missing_streak": 0,
        }

    if active:
        next_streak = missing_streak + 1
        if next_streak >= RESET_CONFIRMATIONS_REQUIRED:
            return False, {
                "active": False,
                "last_notified_at": last_notified_at,
                "last_signature": None,
                "missing_streak": 0,
            }
        return False, {
            "active": True,
            "last_notified_at": last_notified_at,
            "last_signature": last_signature,
            "missing_streak": next_streak,
        }

    return False, {
        "active": False,
        "last_notified_at": last_notified_at,
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
        "captcha",
        "unusual traffic",
        "attention required",
        "just a moment",
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


async def read_public_page(settings: Settings, page) -> tuple[str, str]:
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

    # Allow the public, client-rendered page a short time to populate its text.
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

    # Detect common interstitials and stop instead of attempting any evasion.
    if _looks_like_interstitial(text):
        raise AccessRestricted("The site displayed an anti-automation/interstitial page. Monitoring stopped; no bypass will be attempted.")

    if not text or not text.strip():
        raise RuntimeError("Page rendered with empty visible text; skipping state transition.")

    return text, current_url


async def run_monitor(settings: Settings, telegram: TelegramClient, check_once: bool = False) -> None:
    state = _read_state()

    if not check_once:
        await telegram.send(
            "🟢 Ahly Ticket Assistant بدأ المراقبة.\n"
            f"المصدر: {urlparse(settings.watch_url).netloc}\n"
            f"الفريق: {settings.match_label}\n"
            f"الفحص كل {settings.poll_interval_seconds} ثانية.\n"
            "المراقبة لصفحة عامة فقط؛ لا يوجد تسجيل دخول أو حجز تلقائي."
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(locale="ar-EG")
        # Do not load media assets: the monitor only needs visible page text.
        await context.route("**/*", lambda route: route.abort() if route.request.resource_type in {"image", "media", "font"} else route.continue_())
        page = await context.new_page()
        try:
            while True:
                started = asyncio.get_running_loop().time()
                try:
                    page_text, current_url = await read_public_page(settings, page)
                    result = detect_availability(
                        page_text,
                        settings.team_keywords,
                        settings.open_keywords,
                        settings.require_open_keyword,
                    )
                    LOG.info("Page checked. %s | URL: %s", result.reason, current_url)

                    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    should_notify, next_state = should_notify_and_update_state(state, result, now)
                    if should_notify:
                        _trigger_local_alerts(settings)
                        await telegram.send(
                            f"🚨🔔 عاجل: ظهور مباراة {settings.match_label} على تذكرتي!\n\n"
                            f"الكلمة المطابقة: {result.matched_team}\n"
                            f"مؤشر الإتاحة: {result.matched_open or 'غير مطلوب في الإعدادات'}\n"
                            "ادخل فوراً واضغط على الزرار بالأسفل للحجز قبل الزحمة!\n"
                            f"{settings.watch_url}",
                            button_text="🎟️ افتح صفحة تذكرتي الآن",
                            button_url=settings.watch_url,
                            pin_loudly=True,
                        )
                        # Send rapid burst alerts so phones keep ringing and vibrating like a call
                        for idx in range(1, 5):
                            await asyncio.sleep(1.5)
                            await telegram.send(
                                f"🚨🚨 تنبيه صوتي ({idx}/4): تذاكر {settings.match_label} نزلت دلوقتي! الحق افتح!",
                                button_text="🎟️ افتح صفحة تذكرتي الآن",
                                button_url=settings.watch_url,
                            )
                    if next_state != state:
                        state = next_state
                        _write_state(state)

                except AccessRestricted as exc:
                    LOG.error("Monitoring stopped: %s", exc)
                    await telegram.send(f"⛔ تم إيقاف مراقبة Ahly Ticket Assistant.\nالسبب: {exc}")
                    break
                except PlaywrightTimeoutError:
                    LOG.warning("Timed out while loading/reading the page.")
                except Exception as exc:  # transient network/browser errors
                    LOG.exception("Temporary monitoring error: %s", exc)

                if check_once:
                    break
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(max(0.5, settings.poll_interval_seconds - elapsed))
        finally:
            await context.close()
            await browser.close()
