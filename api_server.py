"""
api_server.py
FastAPI bridge between the Vercel frontend and the GCP VM backend.
Secured with Bearer token authentication.
Exposes: GET /logs, GET /config, POST /config, GET /health
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

# ── Auth ──────────────────────────────────────────────────────────────────────

security = HTTPBearer()
API_BEARER_TOKEN = os.getenv("API_BEARER_TOKEN", "")


def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    if not API_BEARER_TOKEN:
        raise HTTPException(status_code=500, detail="API_BEARER_TOKEN not configured on server")
    if credentials.credentials != API_BEARER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")
    return credentials.credentials


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Trading Bot API Bridge",
    description="Secure bridge between Vercel dashboard and GCP trading worker",
    version="1.0.0",
    docs_url="/docs",       # Disable in production if desired
    redoc_url=None,
)

# CORS — restrict to your Vercel domain in production
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ── Schemas ───────────────────────────────────────────────────────────────────

class ConfigUpdateRequest(BaseModel):
    bybit_api_key: Optional[str] = Field(None, description="Bybit API Key")
    bybit_api_secret: Optional[str] = Field(None, description="Bybit API Secret (will be encrypted)")
    openai_api_key: Optional[str] = Field(None, description="OpenAI API Key (will be encrypted)")
    risk_usdt: Optional[float] = Field(None, ge=1.0, le=10000.0, description="Risk per trade in USDT")
    leverage: Optional[int] = Field(None, ge=1, le=125, description="Default leverage")
    telegram_channel_ids: Optional[List[int]] = Field(None, description="Telegram channel IDs to monitor")
    testnet: Optional[bool] = Field(None, description="Use Bybit testnet")


class LogsResponse(BaseModel):
    lines: List[str]
    total_lines: int


class HealthResponse(BaseModel):
    status: str
    version: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse, tags=["System"])
async def health_check():
    """Public health endpoint — no auth required."""
    return {"status": "ok", "version": "1.0.0"}


@app.get("/logs", response_model=LogsResponse, tags=["Monitoring"])
async def get_logs(token: str = Depends(verify_token)):
    """
    Returns the last 50 lines from activity.log.
    The frontend polls this every 2 seconds for live terminal display.
    """
    if not LOG_FILE.exists():
        return {"lines": ["[INFO] Log file not yet created — waiting for activity..."], "total_lines": 0}

    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        tail = [line.rstrip("\n") for line in all_lines[-LOG_TAIL_LINES:]]
        return {"lines": tail, "total_lines": len(all_lines)}
    except OSError as e:
        logger.error(f"Failed to read log file: {e}")
        raise HTTPException(status_code=500, detail="Failed to read log file")


@app.get("/config", tags=["Configuration"])
async def get_config(token: str = Depends(verify_token)):
    """
    Returns safe (redacted) configuration.
    Secrets are never returned — only boolean flags indicating if they are set.
    """
    try:
        return get_safe_config()
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        raise HTTPException(status_code=500, detail="Failed to load configuration")


@app.post("/config", tags=["Configuration"])
async def update_config(payload: ConfigUpdateRequest, token: str = Depends(verify_token)):
    """
    Updates configuration fields. Provided secrets are immediately encrypted.
    Returns the updated safe config snapshot.
    """
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
        logger.info("Configuration updated via API")
        return {"success": True, "config": updated}
    except Exception as e:
        logger.error(f"Failed to update config: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update configuration: {e}")


@app.get("/status", tags=["Monitoring"])
async def get_status(token: str = Depends(verify_token)):
    """
    Returns live system status: exchange connectivity, Telegram connection state.
    """
    from trader import check_exchange_connection

    exchange_ok = False
    try:
        exchange_ok = await check_exchange_connection()
    except Exception as e:
        logger.warning(f"Exchange status check failed: {e}")

    # Telegram status is tracked by main.py via a shared flag file
    telegram_connected = Path(".telegram_connected").exists()

    return {
        "exchange": "connected" if exchange_ok else "disconnected",
        "telegram": "connected" if telegram_connected else "disconnected",
        "log_file_exists": LOG_FILE.exists(),
    }
