"""
trader.py
Executes trades on Bybit Futures using CCXT.
Calculates position size from fixed USDT risk, sets leverage,
places market/limit entry then attaches SL and TP conditional orders.
"""

import asyncio
import logging
from typing import Optional
import ccxt.async_support as ccxt
from config_manager import get_bybit_credentials, load_config

logger = logging.getLogger(__name__)


def _build_exchange(api_key: str, api_secret: str, testnet: bool) -> ccxt.bybit:
    """Construct and configure a Bybit CCXT exchange instance."""
    exchange = ccxt.bybit(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "linear",  # USDT-margined perpetuals
                "adjustForTimeDifference": True,
            },
        }
    )
    if testnet:
        exchange.set_sandbox_mode(True)
        logger.info("Exchange running in TESTNET mode")
    return exchange


async def execute_trade(signal: dict) -> dict:
    """
    Main trade execution function.
    1. Validates credentials
    2. Fetches ticker to confirm market is live
    3. Sets leverage
    4. Calculates position size based on risk_usdt
    5. Places entry order (Market or Limit)
    6. Places SL and TP conditional orders
    Returns a result dict with order IDs and status.
    """
    api_key, api_secret = get_bybit_credentials()
    if not api_key or not api_secret:
        return {"success": False, "error": "Bybit credentials not configured"}

    config = load_config()
    risk_usdt = float(config.get("risk_usdt", 10.0))
    testnet = config.get("testnet", False)
    signal_leverage = signal.get("leverage", config.get("leverage", 10))

    symbol = signal["symbol"]
    side = signal["side"]
    entry_type = signal.get("entry_type", "Market")
    entry_price = signal.get("entry_price")
    stop_loss = signal.get("stop_loss")
    take_profits = signal.get("take_profit", [])

    if not take_profits:
        take_profits = []
    elif not isinstance(take_profits, list):
        take_profits = [take_profits]

    exchange = _build_exchange(api_key, api_secret, testnet)
    result = {"success": False, "symbol": symbol, "side": side, "orders": []}

    try:
        # Load markets to confirm symbol is valid
        await exchange.load_markets()
        if symbol not in exchange.markets:
            raise ValueError(f"Symbol {symbol} not found on Bybit")

        market = exchange.markets[symbol]
        logger.info(f"Executing {side} trade on {symbol}")

        # Fetch current market price for position sizing
        ticker = await exchange.fetch_ticker(symbol)
        current_price = ticker["last"]
        ref_price = entry_price if entry_price and entry_type == "Limit" else current_price

        # Set leverage
        try:
            await exchange.set_leverage(signal_leverage, symbol)
            logger.info(f"Leverage set to {signal_leverage}x for {symbol}")
        except Exception as e:
            logger.warning(f"Could not set leverage: {e}")

        # Calculate position size from risk
        qty = _calculate_position_size(
            risk_usdt=risk_usdt,
            entry_price=ref_price,
            stop_loss=stop_loss,
            market=market,
        )

        if qty <= 0:
            raise ValueError(f"Calculated quantity {qty} is invalid")

        logger.info(
            f"Position size: {qty} {symbol} | Risk: ${risk_usdt} | "
            f"Entry: {ref_price} | SL: {stop_loss}"
        )

        # Place entry order
        order_side = side.lower()  # ccxt uses lowercase
        if entry_type == "Market":
            entry_order = await exchange.create_order(
                symbol=symbol,
                type="market",
                side=order_side,
                amount=qty,
                params={"reduceOnly": False, "positionIdx": 0},
            )
        else:
            entry_order = await exchange.create_order(
                symbol=symbol,
                type="limit",
                side=order_side,
                amount=qty,
                price=entry_price,
                params={
                    "timeInForce": "GTC",
                    "reduceOnly": False,
                    "positionIdx": 0,
                },
            )

        logger.info(f"Entry order placed: {entry_order['id']}")
        result["orders"].append({"type": "entry", "id": entry_order["id"], "status": entry_order.get("status")})

        # Small delay to let entry process
        await asyncio.sleep(0.5)

        # Place Stop Loss
        if stop_loss:
            sl_side = "sell" if side == "Buy" else "buy"
            try:
                sl_order = await exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=sl_side,
                    amount=qty,
                    params={
                        "stopLoss": stop_loss,
                        "slTriggerBy": "LastPrice",
                        "reduceOnly": True,
                        "positionIdx": 0,
                        "orderType": "Stop",
                        "triggerPrice": stop_loss,
                        "triggerBy": "LastPrice",
                    },
                )
                logger.info(f"Stop loss order placed at {stop_loss}: {sl_order['id']}")
                result["orders"].append({"type": "stop_loss", "id": sl_order["id"], "price": stop_loss})
            except Exception as e:
                logger.error(f"Failed to place stop loss: {e}")
                result["sl_error"] = str(e)

        # Place Take Profit(s)
        if take_profits:
            tp_side = "sell" if side == "Buy" else "buy"
            # Split quantity evenly across TP targets
            tp_qty = round(qty / len(take_profits), market["precision"]["amount"] if "precision" in market else 8)
            tp_qty = max(tp_qty, market.get("limits", {}).get("amount", {}).get("min", tp_qty))

            for i, tp_price in enumerate(take_profits):
                try:
                    tp_order = await exchange.create_order(
                        symbol=symbol,
                        type="limit",
                        side=tp_side,
                        amount=tp_qty,
                        price=tp_price,
                        params={
                            "reduceOnly": True,
                            "positionIdx": 0,
                            "timeInForce": "GTC",
                        },
                    )
                    logger.info(f"TP{i+1} order placed at {tp_price}: {tp_order['id']}")
                    result["orders"].append({"type": f"take_profit_{i+1}", "id": tp_order["id"], "price": tp_price})
                except Exception as e:
                    logger.error(f"Failed to place TP{i+1} at {tp_price}: {e}")
                    result[f"tp{i+1}_error"] = str(e)

        result["success"] = True
        result["entry_price"] = ref_price
        result["quantity"] = qty
        result["leverage"] = signal_leverage
        logger.info(f"Trade execution complete for {symbol}")

    except ccxt.NetworkError as e:
        logger.error(f"Network error during trade execution: {e}")
        result["error"] = f"Network error: {e}"
    except ccxt.ExchangeError as e:
        logger.error(f"Exchange error during trade execution: {e}")
        result["error"] = f"Exchange error: {e}"
    except Exception as e:
        logger.error(f"Unexpected error during trade execution: {e}")
        result["error"] = str(e)
    finally:
        try:
            await exchange.close()
        except Exception:
            pass

    return result


def _calculate_position_size(
    risk_usdt: float,
    entry_price: float,
    stop_loss: Optional[float],
    market: dict,
) -> float:
    """
    Calculate position size based on fixed dollar risk.
    Formula: qty = risk_usdt / abs(entry_price - stop_loss)
    Falls back to minimum viable size if SL is missing.
    """
    if not entry_price or entry_price <= 0:
        logger.warning("Invalid entry price for position sizing")
        return 0.0

    if stop_loss and stop_loss != entry_price:
        risk_per_unit = abs(entry_price - stop_loss)
        qty = risk_usdt / risk_per_unit
    else:
        # No stop loss: use 1% of entry as risk distance fallback
        logger.warning("No stop loss provided — using 1% risk distance fallback")
        qty = risk_usdt / (entry_price * 0.01)

    # Apply market precision and minimum order size
    min_qty = market.get("limits", {}).get("amount", {}).get("min", 0.001)
    precision = market.get("precision", {}).get("amount", 3)

    qty = max(qty, min_qty)
    qty = round(qty, int(precision) if isinstance(precision, (int, float)) else 3)

    return qty


async def check_exchange_connection() -> bool:
    """Lightweight connectivity check. Returns True if exchange responds."""
    api_key, api_secret = get_bybit_credentials()
    if not api_key or not api_secret:
        return False

    config = load_config()
    testnet = config.get("testnet", False)
    exchange = _build_exchange(api_key, api_secret, testnet)

    try:
        await exchange.fetch_time()
        return True
    except Exception as e:
        logger.warning(f"Exchange connectivity check failed: {e}")
        return False
    finally:
        try:
            await exchange.close()
        except Exception:
            pass
