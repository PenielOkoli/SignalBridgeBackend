"""
config_manager.py
Manages encrypted configuration using AES-256 via Fernet (cryptography library).
Generates a master.key on first run. Sensitive values are never stored in plaintext.
"""

import json
import os
import logging
from pathlib import Path
from typing import Any, Optional
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

CONFIG_FILE = Path("config.json")
KEY_FILE = Path("master.key")

DEFAULT_CONFIG = {
    "bybit_api_key": "",
    "bybit_api_secret_enc": "",
    "openai_api_key_enc": "",
    "risk_usdt": 10.0,
    "leverage": 10,
    "telegram_channel_ids": [],
    "testnet": False,
}


def _load_or_create_key() -> Fernet:
    """Load existing master key or generate a new one on first run."""
    if KEY_FILE.exists():
        key = KEY_FILE.read_bytes()
        logger.debug("Loaded existing master.key")
    else:
        key = Fernet.generate_key()
        KEY_FILE.write_bytes(key)
        KEY_FILE.chmod(0o600)  # Owner read/write only
        logger.info("Generated new master.key")
    return Fernet(key)


def _get_fernet() -> Fernet:
    return _load_or_create_key()


def load_config() -> dict:
    """Load config from disk, creating defaults if not present."""
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        logger.info("Created default config.json")
        return DEFAULT_CONFIG.copy()
    with open(CONFIG_FILE, "r") as f:
        data = json.load(f)
    # Merge with defaults to handle missing keys gracefully
    merged = DEFAULT_CONFIG.copy()
    merged.update(data)
    return merged


def save_config(config: dict) -> None:
    """Persist config to disk."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    CONFIG_FILE.chmod(0o600)


def encrypt_value(plaintext: str) -> str:
    """Encrypt a string value and return base64-encoded ciphertext."""
    if not plaintext:
        return ""
    fernet = _get_fernet()
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a base64-encoded ciphertext string."""
    if not ciphertext:
        return ""
    fernet = _get_fernet()
    try:
        return fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt value — invalid token or corrupt key")
        return ""


def get_bybit_credentials() -> tuple[str, str]:
    """Return (api_key, api_secret) in plaintext for runtime use."""
    config = load_config()
    api_key = config.get("bybit_api_key", "")
    api_secret = decrypt_value(config.get("bybit_api_secret_enc", ""))
    return api_key, api_secret


def get_openai_key() -> str:
    """Return OpenAI API key in plaintext for runtime use."""
    config = load_config()
    return decrypt_value(config.get("openai_api_key_enc", ""))


def update_credentials(
    bybit_api_key: Optional[str] = None,
    bybit_api_secret: Optional[str] = None,
    openai_api_key: Optional[str] = None,
    risk_usdt: Optional[float] = None,
    leverage: Optional[int] = None,
    telegram_channel_ids: Optional[list] = None,
    testnet: Optional[bool] = None,
) -> dict:
    """
    Update config fields. Secrets are encrypted before storage.
    Returns safe (non-sensitive) config snapshot.
    """
    config = load_config()

    if bybit_api_key is not None:
        config["bybit_api_key"] = bybit_api_key
    if bybit_api_secret is not None:
        config["bybit_api_secret_enc"] = encrypt_value(bybit_api_secret)
    if openai_api_key is not None:
        config["openai_api_key_enc"] = encrypt_value(openai_api_key)
    if risk_usdt is not None:
        config["risk_usdt"] = float(risk_usdt)
    if leverage is not None:
        config["leverage"] = int(leverage)
    if telegram_channel_ids is not None:
        config["telegram_channel_ids"] = telegram_channel_ids
    if testnet is not None:
        config["testnet"] = testnet

    save_config(config)
    logger.info("Configuration updated successfully")
    return get_safe_config()


def get_safe_config() -> dict:
    """Return config with secrets redacted — safe to send over API."""
    config = load_config()
    return {
        "bybit_api_key": config.get("bybit_api_key", ""),
        "bybit_api_secret_set": bool(config.get("bybit_api_secret_enc")),
        "openai_api_key_set": bool(config.get("openai_api_key_enc")),
        "risk_usdt": config.get("risk_usdt", 10.0),
        "leverage": config.get("leverage", 10),
        "telegram_channel_ids": config.get("telegram_channel_ids", []),
        "testnet": config.get("testnet", False),
    }
