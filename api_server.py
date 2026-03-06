"""
api_server.py - with /telegram/channels endpoint
"""

import os
import logging
from pathlib import Path
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from config_manager import get_safe_config, update_credentials

logger = logging.getLogger(__name__)
LOG_FILE = Path("activity.log")
LOG_TAIL_LINES = 50

security = HTTPBearer()
API_BEARER_TOKEN = os.getenv("API_BEARER_TOKEN", "")


def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    if not API_BEARER_TOKEN:
        raise HTTPException(status_code=500, detail="API_BEARER_TOKEN not configured on server")
    if credentials.credentials != API_BEARER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")
    return credentials.credentials


app = FastAPI(title="Trading Bot API Bridge", version="1.0.0", docs_url="/docs", redoc_url=None)

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


class ConfigUpdateRequest(BaseModel):
    bybit_api_key: Optional[str] = Field(None)
    bybit_api_secret: Optional[str] = Field(None)
    openai_api_key: Optional[str] = Field(None)
    risk_usdt: Optional[float] = Field(None, ge=1.0, le=10000.0)
    leverage: Optional[int] = Field(None, ge=1, le=125)
    telegram_channel_ids: Optional[List[int]] = Field(None)
    testnet: Optional[bool] = Field(None)


class TelegramChannel(BaseModel):
    id: int
    name: str
    type: str
    username: Optional[str] = None
    members: Optional[int] = None


@app.get("/health")
async def health_check():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/logs")
async def get_logs(token: str = Depends(verify_token)):
    if not LOG_FILE.exists():
        return {"lines": ["[INFO] Waiting for activity..."], "total_lines": 0}
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = [line.rstrip("\n") for line in all_lines[-LOG_TAIL_LINES:]]
        return {"lines": tail, "total_lines": len(all_lines)}
    except OSError as e:
        raise HTTPException(status_code=500, detail="Failed to read log file")


@app.get("/config")
async def get_config(token: str = Depends(verify_token)):
    try:
        return get_safe_config()
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to load configuration")


@app.post("/config")
async def update_config(payload: ConfigUpdateRequest, token: str = Depends(verify_token)):
    try:
        updated = update_credentials(
            bybit_api_key=payload.bybit_api_key,
            bybit_api_secret=payload.bybit_api_secret,
            openai_api_key=payload.openai_api_key,
            risk_usdt=payload.risk_usdt,
            leverage=payload.leverage,
            telegram_channel_ids=payload.telegram_channel_ids,
            testnet=payload.testnet,
        )
        return {"success": True, "config": updated}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update configuration: {e}")


@app.get("/status")
async def get_status(token: str = Depends(verify_token)):
    from trader import check_exchange_connection
    exchange_ok = False
    try:
        exchange_ok = await check_exchange_connection()
    except Exception:
        pass
    telegram_connected = Path(".telegram_connected").exists()
    return {
        "exchange": "connected" if exchange_ok else "disconnected",
        "telegram": "connected" if telegram_connected else "disconnected",
        "log_file_exists": LOG_FILE.exists(),
    }


@app.get("/telegram/channels", response_model=List[TelegramChannel])
async def get_telegram_channels(token: str = Depends(verify_token)):
    """Returns all Telegram channels/groups the account is a member of."""
    try:
        from telethon import TelegramClient
        from telethon.tl.types import Channel, Chat, ChatForbidden, ChannelForbidden

        api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
        api_hash = os.getenv("TELEGRAM_API_HASH", "")

        if not api_id or not api_hash:
            raise HTTPException(status_code=500, detail="Telegram credentials not configured")

        client = TelegramClient("trading_bot_session", api_id, api_hash)
        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=401, detail="Telegram session not authorized")

        dialogs = await client.get_dialogs()
        channels = []

        for dialog in dialogs:
            entity = dialog.entity
            if isinstance(entity, (ChatForbidden, ChannelForbidden)):
                continue
            if isinstance(entity, Channel):
                channel_type = "channel" if entity.broadcast else "supergroup"
                channels.append(TelegramChannel(
                    id=int(f"-100{entity.id}"),
                    name=dialog.name or entity.title or "Unknown",
                    type=channel_type,
                    username=getattr(entity, "username", None),
                    members=getattr(entity, "participants_count", None),
                ))
            elif isinstance(entity, Chat):
                channels.append(TelegramChannel(
                    id=-entity.id,
                    name=dialog.name or entity.title or "Unknown",
                    type="group",
                    username=None,
                    members=getattr(entity, "participants_count", None),
                ))

        await client.disconnect()

        type_order = {"channel": 0, "supergroup": 1, "group": 2}
        channels.sort(key=lambda c: (type_order.get(c.type, 3), c.name.lower()))
        logger.info(f"Returned {len(channels)} Telegram channels")
        return channels

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch Telegram channels: {e}")
        raise HTTPException(status_code=500, detail=str(e))
