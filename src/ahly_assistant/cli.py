from __future__ import annotations

import argparse
import asyncio
import logging
import os

from .config import load_settings
from .monitor import run_monitor
from .telegram_client import TelegramClient


async def _start_health_server() -> asyncio.AbstractServer | None:
    port_str = os.getenv("PORT", "").strip()
    if not port_str:
        return None

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(1024)
            body = b"OK - Ahly Ticket Assistant is running\n"
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle_client, "0.0.0.0", int(port_str))
    logging.info("Cloud healthcheck server listening on port %s", port_str)
    return server


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Public-page ticket availability alerts via Telegram.")
    parser.add_argument("--test-telegram", action="store_true", help="Verify bot credentials and send a test to the group.")
    parser.add_argument("--check-once", action="store_true", help="Check the configured page once, then exit.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    settings = load_settings()
    telegram = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
    health_server = None
    try:
        bot = await telegram.verify()
        logging.info("Connected to Telegram bot @%s", bot.get("username", "unknown"))
        if args.test_telegram:
            await telegram.send("✅ اختبار ناجح: Ahly Ticket Assistant متصل بجروب Telegram.")
            print("Telegram test message sent successfully.")
            return
        if not args.check_once:
            health_server = await _start_health_server()
        await run_monitor(settings, telegram, check_once=args.check_once)
    finally:
        if health_server is not None:
            health_server.close()
            await health_server.wait_closed()
        await telegram.close()


def entrypoint() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped by user.")
    except Exception as exc:
        logging.error("Startup failed: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    entrypoint()
