"""
signal_parser.py
Uses GPT-4o Mini to parse unstructured Telegram trading signals into strict JSON.
Handles malformed input, retries, and validation of parsed fields.
"""

import json
import logging
import re
from typing import Optional
from openai import AsyncOpenAI, OpenAIError
from config_manager import get_openai_key

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a precision trading signal parser for cryptocurrency futures markets.
Your ONLY job is to extract structured data from trading signals and return valid JSON.

RULES:
1. Always return ONLY a valid JSON object — no preamble, no explanation, no markdown fences.
2. Symbol must be in Bybit Linear format: e.g., BTCUSDT, ETHUSDT, SOLUSDT (uppercase, no slash).
3. side must be exactly "Buy" or "Sell".
4. entry_type must be exactly "Market" or "Limit".
5. All price/amount values must be numbers (float), not strings.
6. If a field cannot be determined, use null.
7. take_profit can be a single float or a list of floats (for multiple TP targets).
8. leverage must be an integer between 1 and 125. Default to 10 if not specified.

REQUIRED OUTPUT SCHEMA:
{
  "symbol": "BTCUSDT",
  "side": "Buy",
  "entry_type": "Market",
  "entry_price": 65000.0,
  "stop_loss": 63000.0,
  "take_profit": [66000.0, 67000.0],
  "leverage": 10
}"""

USER_PROMPT_TEMPLATE = """Parse this trading signal and return the JSON object:

---
{signal_text}
---"""


async def parse_signal(signal_text: str) -> Optional[dict]:
    """
    Parse a raw trading signal string using GPT-4o Mini.
    Returns a validated dict or None if parsing fails.
    """
    api_key = get_openai_key()
    if not api_key:
        logger.error("OpenAI API key not configured")
        return None

    client = AsyncOpenAI(api_key=api_key)

    for attempt in range(3):
        try:
            logger.info(f"Parsing signal (attempt {attempt + 1})")
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": USER_PROMPT_TEMPLATE.format(
                            signal_text=signal_text[:2000]  # Safety truncation
                        ),
                    },
                ],
                temperature=0.0,  # Deterministic output
                max_tokens=300,
                response_format={"type": "json_object"},
            )

            raw = response.choices[0].message.content.strip()
            logger.debug(f"Raw GPT response: {raw}")

            parsed = json.loads(raw)
            validated = _validate_signal(parsed)

            if validated:
                logger.info(f"Parsed signal: {validated['symbol']} {validated['side']}")
                return validated
            else:
                logger.warning(f"Signal validation failed on attempt {attempt + 1}")

        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error on attempt {attempt + 1}: {e}")
        except OpenAIError as e:
            logger.error(f"OpenAI API error on attempt {attempt + 1}: {e}")
            if "rate_limit" in str(e).lower():
                import asyncio
                await asyncio.sleep(5 * (attempt + 1))  # Exponential backoff
        except Exception as e:
            logger.error(f"Unexpected error parsing signal: {e}")

    logger.error("All parse attempts failed")
    return None


def _validate_signal(data: dict) -> Optional[dict]:
    """Validate and normalize parsed signal fields."""
    errors = []

    # Symbol validation
    symbol = data.get("symbol")
    if not symbol or not isinstance(symbol, str):
        errors.append("Missing or invalid symbol")
    else:
        symbol = symbol.upper().replace("/", "").replace("-", "")
        if not symbol.endswith("USDT"):
            logger.warning(f"Symbol {symbol} may not be a USDT linear pair")

    # Side validation
    side = data.get("side")
    if side not in ("Buy", "Sell"):
        errors.append(f"Invalid side: {side}")

    # Entry type
    entry_type = data.get("entry_type", "Market")
    if entry_type not in ("Market", "Limit"):
        entry_type = "Market"

    # Numeric fields
    entry_price = _to_float(data.get("entry_price"))
    stop_loss = _to_float(data.get("stop_loss"))

    # Take profit — can be scalar or list
    tp_raw = data.get("take_profit")
    if isinstance(tp_raw, list):
        take_profit = [_to_float(x) for x in tp_raw if _to_float(x) is not None]
        if not take_profit:
            take_profit = None
    else:
        take_profit = _to_float(tp_raw)
        if take_profit is not None:
            take_profit = [take_profit]

    # Leverage
    leverage = data.get("leverage", 10)
    try:
        leverage = max(1, min(125, int(leverage)))
    except (TypeError, ValueError):
        leverage = 10

    if errors:
        logger.warning(f"Signal validation errors: {errors}")
        return None

    return {
        "symbol": symbol,
        "side": side,
        "entry_type": entry_type,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "leverage": leverage,
    }


def _to_float(value) -> Optional[float]:
    """Safely convert a value to float."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        # Try stripping commas / currency symbols
        if isinstance(value, str):
            cleaned = re.sub(r"[^\d.]", "", value)
            try:
                return float(cleaned)
            except ValueError:
                pass
    return None
