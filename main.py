"""
main.py
Orchestrates the trading bot:
  1. Telethon client — listens for signals in configured Telegram channels
  2. FastAPI server (uvicorn) — the Vercel dashboard bridge
Both run concurrently using asyncio.gather().
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
import uvicorn
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, FloodWaitError

from config_manager import load_config
from signal_parser import parse_signal
from trader import execute_trade
from api_server import app as fastapi_app

# ── Environment ───────────────────────────────────────────────────────────────
load_dotenv()

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE", "")
UVICORN_PORT = int(os.getenv("UVICORN_PORT", "8000"))
UVICORN_HOST = os.getenv("UVICORN_HOST", "0.0.0.0")

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE = "activity.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("main")

# ── State ─────────────────────────────────────────────────────────────────────
CONNECTED_FLAG = Path(".telegram_connected")
processing_lock = asyncio.Lock()  # Prevent concurrent trade execution


# ── Telegram Signal Handler ───────────────────────────────────────────────────

async def handle_message(event):
    """
    Called for every new message in monitored channels.
    Filters, parses, and executes trading signals.
    """
    raw_text = event.raw_text.strip()
    if not raw_text or len(raw_text) < 10:
        return  # Ignore empty/trivial messages

    chat = await event.get_chat()
    chat_name = getattr(chat, "title", str(event.chat_id))
    logger.info(f"[SIGNAL RECEIVED] From: {chat_name} | Length: {len(raw_text)} chars")
    logger.info(f"[RAW] {raw_text[:200]}...")

    # Heuristic pre-filter: skip obvious non-signal messages
    keywords = ["buy", "sell", "long", "short", "entry", "sl", "tp", "target", "#"]
    if not any(kw in raw_text.lower() for kw in keywords):
        logger.info("[SKIP] Message does not match signal keywords — ignoring")
        return

    # Only one trade execution at a time
    async with processing_lock:
        logger.info("[PARSING] Sending to GPT-4o Mini for parsing...")
        signal = await parse_signal(raw_text)

        if not signal:
            logger.warning("[PARSE FAILED] Could not extract valid signal from message")
            return

        logger.info(
            f"[PARSED] {signal['symbol']} {signal['side']} | "
            f"Entry: {signal.get('entry_price')} | SL: {signal.get('stop_loss')} | "
            f"TP: {signal.get('take_profit')} | Leverage: {signal.get('leverage')}x"
        )

        logger.info("[EXECUTING] Placing orders on Bybit...")
        result = await execute_trade(signal)

        if result["success"]:
            order_count = len(result.get("orders", []))
            logger.info(
                f"[SUCCESS] Trade executed — {order_count} orders placed | "
                f"Entry: {result.get('entry_price')} | Qty: {result.get('quantity')} | "
                f"Leverage: {result.get('leverage')}x"
            )
            for order in result.get("orders", []):
                logger.info(f"  ↳ [{order['type'].upper()}] ID: {order['id']} | Price: {order.get('price', 'MARKET')}")
        else:
            logger.error(f"[FAILED] Trade execution failed: {result.get('error')}")


# ── Telegram Client Runner ────────────────────────────────────────────────────

async def run_telegram():
    """
    Initializes Telethon, handles phone auth, and starts listening.
    Reconnects automatically on network drops.
    """
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        logger.critical("TELEGRAM_API_ID / TELEGRAM_API_HASH not set in .env — cannot start")
        return

    client = TelegramClient(
        "trading_bot_session",
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
        connection_retries=10,
        retry_delay=5,
        auto_reconnect=True,
    )

    while True:
        try:
            logger.info("Connecting to Telegram...")
            await client.start(phone=TELEGRAM_PHONE)

            if not await client.is_user_authorized():
                logger.error("Telegram session not authorized — run interactively first for OTP")
                CONNECTED_FLAG.unlink(missing_ok=True)
                await asyncio.sleep(30)
                continue

            me = await client.get_me()
            logger.info(f"[TELEGRAM] Connected as: {me.username or me.first_name} (ID: {me.id})")
            CONNECTED_FLAG.touch()

            # Load channel IDs from config
            config = load_config()
            channel_ids = config.get("telegram_channel_ids", [])

            if not channel_ids:
                logger.warning("[TELEGRAM] No channel_ids configured — listening to ALL accessible channels")
                @client.on(events.NewMessage())
                async def _handler(event):
                    await handle_message(event)
            else:
                logger.info(f"[TELEGRAM] Monitoring {len(channel_ids)} channel(s): {channel_ids}")
                @client.on(events.NewMessage(chats=channel_ids))
                async def _handler(event):
                    await handle_message(event)

            logger.info("[TELEGRAM] Listener active — waiting for signals...")
            await client.run_until_disconnected()

        except FloodWaitError as e:
            logger.warning(f"[TELEGRAM] Flood wait: sleeping {e.seconds}s")
            CONNECTED_FLAG.unlink(missing_ok=True)
            await asyncio.sleep(e.seconds)
        except SessionPasswordNeededError:
            logger.critical("[TELEGRAM] 2FA enabled — please run once interactively to authenticate")
            CONNECTED_FLAG.unlink(missing_ok=True)
            break
        except Exception as e:
            logger.error(f"[TELEGRAM] Connection error: {e} — retrying in 15s")
            CONNECTED_FLAG.unlink(missing_ok=True)
            await asyncio.sleep(15)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


# ── FastAPI Runner ────────────────────────────────────────────────────────────

async def run_api_server():
    """Run the uvicorn FastAPI server as an asyncio task."""
    config = uvicorn.Config(
        app=fastapi_app,
        host=UVICORN_HOST,
        port=UVICORN_PORT,
        log_level="info",
        access_log=True,
        reload=False,
    )
    server = uvicorn.Server(config)
    logger.info(f"[API] FastAPI bridge starting on {UVICORN_HOST}:{UVICORN_PORT}")
    await server.serve()


# ── Entry Point ───────────────────────────────────────────────────────────────

async def main():
    logger.info("=" * 60)
    logger.info("   TRADING BOT STARTING")
    logger.info("=" * 60)

    config = load_config()
    logger.info(f"Config loaded | Testnet: {config.get('testnet')} | Risk: ${config.get('risk_usdt')} USDT")

    # Run both services concurrently
    await asyncio.gather(
        run_telegram(),
        run_api_server(),
        return_exceptions=True,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        CONNECTED_FLAG.unlink(missing_ok=True)
