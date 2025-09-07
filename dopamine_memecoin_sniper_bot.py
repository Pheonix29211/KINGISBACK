import asyncio
import requests
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solana.transaction import Transaction
from spl.token.instructions import create_associated_token_account, get_associated_token_address
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import json
import os
import csv
from datetime import datetime, timedelta
import logging
import pandas as pd
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from solders.instruction import Instruction, AccountMeta
from solders.pubkey import Pubkey

# Setup logging to Render disk
logging.basicConfig(filename='/opt/render/project/src/data/sniper_bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Configuration
WALLET_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
SHYFT_API_KEY = os.getenv("SHYFT_API_KEY")
DEXSCREENER_TOKEN_API = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_PAIRS_API = "https://api.dexscreener.com/latest/dex/pairs/solana"
SHYFT_API = "https://api.shyft.to/sol/v1/token"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BACKTEST_MODE = os.getenv("BACKTEST_MODE", "False") == "True"
MODE_PIN = "1234"  # Hardcoded PIN for /mode live
ENTRY_MC_MIN = 75000  # $75k
ENTRY_MC_MAX = 2000000  # $2M
ENTRY_LP_MIN_USD = 30000  # $30k
ENTRY_LP_TO_MCAP_MIN = 0.15  # 15%
ENTRY_POOL_AGE_MIN = 60  # 60 seconds
VOL1H_MIN = 50000  # $50k
ACCEL_MIN = 0.8  # 0.8
BUY_AMOUNT_MIN = 0.048387  # ~$15 at $310/SOL
BUY_AMOUNT_MAX = 0.048387
PROFIT_REINVEST_RATIO = 0.5
LOSS_STREAK_THRESHOLD = 3
MAX_TRADES_PER_DAY = 4
ATR_PERIOD = 12
ATR_MULTIPLIER = 2.8
SLIPPAGE = 0.03
MAX_PRICE_IMPACT = 0.05
MAX_TOKEN_AGE = 6 * 3600
HEALTH_CHECK_INTERVAL = 3600
DATA_POLL_INTERVAL = 10
PRIORITY_FEE = 0.002
MIN_SOL_BALANCE = 0.15
PORT = int(os.getenv("PORT", 8080))

# HTTP session with retries
session = requests.Session()
retries = Retry(total=5, backoff_factor=3, status_forcelist=[429, 500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retries))

# Global state
loss_streak = 0
trade_count = 0
last_trade_day = datetime.now().date()
current_buy_amount = BUY_AMOUNT_MIN
paper_trades = []
active_positions = {}  # token: {"buy_price": float, "gain": float, "atr": float, "trailing_stop": float}
price_history = {}
api_cache = {}
processed_tokens = set()
paper_trading = False
auto_paper = False

async def send_notification(message, context=None, is_win=True):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram bot token or chat ID missing")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    last_sent = api_cache.get(url, (None, 0))[1]
    if datetime.now().timestamp() - last_sent < 5:
        return
    for _ in range(3):
        try:
            response = session.post(url, json=payload)
            if response.status_code == 200:
                api_cache[url] = (None, datetime.now().timestamp())
                break
            logging.error(f"Telegram notification failed: {response.status_code} - {response.text}")
        except Exception as e:
            logging.error(f"Telegram notification error: {str(e)}")
        await asyncio.sleep(1)
    logging.info(f"{datetime.now()}: {message}")

async def check_wallet_balance(sol_client):
    try:
        keypair = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
        for _ in range(3):
            try:
                balance = await sol_client.get_balance(keypair.pubkey())
                sol_balance = balance.value / 1_000_000_000
                if sol_balance < MIN_SOL_BALANCE:
                    await send_notification(f"😿 Low balance! Only {sol_balance:.4f} SOL left, need {MIN_SOL_BALANCE} SOL! 💔")
                    return False, sol_balance
                return True, sol_balance
            except Exception as e:
                logging.error(f"Wallet balance check attempt failed: {str(e)}")
                await asyncio.sleep(5)
        await send_notification(f"😿 Wallet balance check failed after retries! 💔")
        return False, 0
    except Exception as e:
        logging.error(f"Wallet balance check error: {str(e)}")
        await send_notification(f"😿 Wallet balance check error: {str(e)} 💔")
        return False, 0

async def check_rug(token_address):
    if not SHYFT_API_KEY:
        logging.error("Shyft API key missing")
        await send_notification("😿 Shyft API key missing! Cannot perform rug checks. 💔")
        return False
    try:
        headers = {"x-api-key": SHYFT_API_KEY}
        for _ in range(3):
            try:
                response = session.get(f"{SHYFT_API}/{token_address}", headers=headers)
                if response.status_code == 200:
                    data = response.json().get("result", {})
                    if data.get("is_suspicious") or not data.get("liquidity_locked"):
                        logging.info(f"Rug detected for {token_address}: Suspicious or unlocked liquidity")
                        return True
                    return False
                logging.error(f"Shyft rug check failed for {token_address}: Status {response.status_code} - {response.text}")
            except Exception as e:
                logging.error(f"Shyft rug check attempt failed for {token_address}: {str(e)}")
            await asyncio.sleep(3)
        logging.error(f"Shyft rug check failed for {token_address} after retries")
        return False
    except Exception as e:
        logging.error(f"Shyft rug check error for {token_address}: {str(e)}")
        return False

async def calculate_atr(token_address, current_price):
    try:
        if token_address not in price_history:
            price_history[token_address] = []
        high = current_price
        low = current_price
        close = current_price
        if price_history[token_address]:
            prev_close = price_history[token_address][-1]["close"]
            high = max(high, prev_close)
            low = min(low, prev_close)
        price_history[token_address].append({"high": high, "low": low, "close": close})
        if len(price_history[token_address]) > ATR_PERIOD:
            price_history[token_address] = price_history[token_address][-ATR_PERIOD:]
        true_ranges = []
        for i in range(1, len(price_history[token_address])):
            high_low = price_history[token_address][i]["high"] - price_history[token_address][i]["low"]
            high_prev_close = abs(price_history[token_address][i]["high"] - price_history[token_address][i-1]["close"])
            low_prev_close = abs(price_history[token_address][i]["low"] - price_history[token_address][i-1]["close"])
            true_ranges.append(max(high_low, high_prev_close, low_prev_close))
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0
    except Exception as e:
        logging.error(f"ATR calculation error for {token_address}: {str(e)}")
        return 0

async def check_token(token_address):
    cache_key = f"{DEXSCREENER_PAIRS_API}/{token_address}"
    cached_data, cached_time = api_cache.get(cache_key, (None, 0))
    if cached_data and datetime.now().timestamp() - cached_time < 30:
        data = cached_data
    else:
        for _ in range(3):
            try:
                response = session.get(f"{DEXSCREENER_PAIRS_API}/{token_address}")
                if response.status_code == 200:
                    try:
                        data = response.json()
                        if data is None or not isinstance(data, dict) or "pair" not in data or not data["pair"]:
                            logging.error(f"DexScreener token check failed for {token_address}: Invalid JSON response - {response.text}")
                            data = None
                            continue
                        api_cache[cache_key] = (data, datetime.now().timestamp())
                        break
                    except json.JSONDecodeError as e:
                        logging.error(f"DexScreener token check failed for {token_address}: JSON decode error - {str(e)}")
                        data = None
                        continue
                logging.error(f"DexScreener token check failed for {token_address}: Status {response.status_code} - {response.text}")
            except Exception as e:
                logging.error(f"Token check error for {token_address}: {str(e)}")
            await asyncio.sleep(3)
        if data is None or not isinstance(data, dict):
            logging.error(f"Token check failed for {token_address}: No valid response data after retries")
            return None, None, None
    try:
        market_cap = float(data.get("pair", {}).get("marketCap", 0))
        liquidity = float(data.get("pair", {}).get("liquidity", {}).get("usd", 0))
        price = float(data.get("pair", {}).get("priceUsd", 0))
        price_impact = float(data.get("pair", {}).get("priceChange", {}).get("m5", 0))
        created_at = data.get("pair", {}).get("createdAt", None)
        volume_1h = float(data.get("pair", {}).get("volume", {}).get("h1", 0))
        price_change_1h = float(data.get("pair", {}).get("priceChange", {}).get("h1", 0))
        acceleration = price_change_1h / 60 if price_change_1h > 0 else 0
    except (ValueError, TypeError) as e:
        logging.error(f"Data parsing error for {token_address}: {str(e)}")
        return None, None, None
    max_cap = ENTRY_MC_MAX / (2 if loss_streak >= LOSS_STREAK_THRESHOLD else 1)
    if not (ENTRY_MC_MIN <= market_cap <= max_cap) or liquidity < ENTRY_LP_MIN_USD or (liquidity / market_cap) < ENTRY_LP_TO_MCAP_MIN or abs(price_impact) > MAX_PRICE_IMPACT or volume_1h < VOL1H_MIN or acceleration < ACCEL_MIN:
        logging.info(f"Token {token_address} filtered out: market_cap={market_cap}, liquidity={liquidity}, lp_to_mcap={liquidity / market_cap}, price_impact={price_impact}, volume_1h={volume_1h}, acceleration={acceleration}")
        return None, None, None
    if created_at:
        try:
            created_time = datetime.fromtimestamp(created_at / 1000)
            if (datetime.now() - created_time).total_seconds() < ENTRY_POOL_AGE_MIN:
                logging.info(f"Token {token_address} filtered out: Too new (age < 60 seconds)")
                return None, None, None
            if (datetime.now() - created_time).total_seconds() > MAX_TOKEN_AGE:
                logging.info(f"Token {token_address} filtered out: Too old")
                return None, None, None
        except (TypeError, ValueError):
            logging.warning(f"Invalid created_at for {token_address}, skipping age check")
    if await check_rug(token_address):
        logging.info(f"Token {token_address} filtered out: Rug detected")
        return None, None, None
    price_volatility = float(data.get("pair", {}).get("priceChange", {}).get("m5", 0))
    if abs(price_volatility) > 15:
        logging.info(f"Token {token_address} filtered out: High volatility")
        return None, None, None
    logging.info(f"Token {token_address} passed checks: market_cap={market_cap}, price={price}, liquidity={liquidity}")
    return market_cap, price, liquidity

async def execute_trade(token_address, buy=True, paper=False):
    try:
        global loss_streak, trade_count, active_positions, current_buy_amount, paper_trades
        if paper or paper_trading:
            logging.info(f"Paper trade: {'Buying' if buy else 'Selling'} {token_address} with {current_buy_amount} SOL")
            if buy:
                market_cap, buy_price, _ = await check_token(token_address)
                if not market_cap:
                    return False
                atr = await calculate_atr(token_address, buy_price)
                active_positions[token_address] = {"buy_price": buy_price, "gain": 1.0, "atr": atr, "trailing_stop": buy_price - atr * ATR_MULTIPLIER}
                paper_trades.append({"token": token_address, "buy_price": buy_price, "amount": current_buy_amount, "timestamp": datetime.now().isoformat(), "type": "buy"})
            else:
                profit = (active_positions[token_address]["gain"] - 1) * current_buy_amount * 310
                paper_trades.append({"token": token_address, "sell_price": active_positions[token_address]["buy_price"] * active_positions[token_address]["gain"], "profit": profit, "timestamp": datetime.now().isoformat(), "type": "sell"})
                if profit > 0:
                    current_buy_amount = min(BUY_AMOUNT_MAX * 2, current_buy_amount + profit * PROFIT_REINVEST_RATIO / 310)
                active_positions.pop(token_address, None)
            return True
        async with AsyncClient(SOLANA_RPC) as sol_client:
            keypair = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
            if not (await check_wallet_balance(sol_client))[0]:
                return False
            token_mint = Pubkey.from_string(token_address)
            token_account = get_associated_token_address(keypair.pubkey(), token_mint)
            tx = Transaction()
            account_info = await sol_client.get_account_info(token_account)
            if not account_info.value:
                tx.add(create_associated_token_account(keypair.pubkey(), keypair.pubkey(), token_mint))
            tx.add(
                Instruction(
                    program_id=Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"),
                    data=bytes([1 if buy else 2]),
                    accounts=[
                        AccountMeta(pubkey=keypair.pubkey(), is_signer=True, is_writable=True),
                        AccountMeta(pubkey=token_account, is_signer=False, is_writable=True),
                    ]
                )
            )
            for _ in range(3):
                try:
                    blockhash = await sol_client.get_latest_blockhash()
                    tx.recent_blockhash = blockhash.value.blockhash
                    tx.fee_payer = keypair.pubkey()
                    # await sol_client.send_transaction(tx, keypair, opts={"priority_fee": PRIORITY_FEE})
                    if buy:
                        market_cap, buy_price, _ = await check_token(token_address)
                        if not market_cap:
                            return False
                        atr = await calculate_atr(token_address, buy_price)
                        await send_notification(f"🚀 Sniping {token_address} at ${market_cap} with {current_buy_amount} SOL (~$15)! MOON TIME! 😘")
                        active_positions[token_address] = {"buy_price": buy_price, "gain": 1.0, "atr": atr, "trailing_stop": buy_price - atr * ATR_MULTIPLIER}
                    else:
                        profit = (active_positions[token_address]["gain"] - 1) * current_buy_amount * 310
                        await send_notification(
                            f"💸 Sold {token_address}! Profit: {active_positions[token_address]['gain']:.2f}x 🤑"
                            if active_positions[token_address]["gain"] > 1
                            else f"😢 Sold {token_address}, loss taken. Let’s bounce back! 💔",
                            is_win=active_positions[token_address]["gain"] > 1
                        )
                        if profit > 0:
                            current_buy_amount = min(BUY_AMOUNT_MAX * 2, current_buy_amount + profit * PROFIT_REINVEST_RATIO / 310)
                        active_positions.pop(token_address, None)
                    trade_count += 1
                    return True
                except Exception as e:
                    await send_notification(f"😿 Trade error for {token_address}! {str(e)} Retrying... 💔")
                    logging.error(f"Trade error for {token_address}: {str(e)}")
                    await asyncio.sleep(1)
            await send_notification(f"😿 Trade failed for {token_address} after retries! Check SOLANA_RPC or balance! 💔")
            return False
    except Exception as e:
        logging.error(f"Execute trade error for {token_address}: {str(e)}")
        return False

async def monitor_price(token_address, buy_price, market_cap, paper=False):
    try:
        global loss_streak, paper_trades
        start_time = datetime.now()
        while (datetime.now() - start_time).seconds < 7200:
            if paper or paper_trading:
                cache_key = f"{DEXSCREENER_PAIRS_API}/{token_address}"
                cached_data, cached_time = api_cache.get(cache_key, (None, 0))
                if cached_data and datetime.now().timestamp() - cached_time < 30:
                    data = cached_data
                else:
                    response = session.get(f"{DEXSCREENER_PAIRS_API}/{token_address}")
                    if response.status_code != 200:
                        logging.error(f"Price check failed for {token_address}: Status {response.status_code} - {response.text}")
                        break
                    try:
                        data = response.json()
                        if data is None or not isinstance(data, dict) or "pair" not in data or not data["pair"]:
                            logging.error(f"Price check failed for {token_address}: Invalid JSON response - {response.text}")
                            break
                        api_cache[cache_key] = (data, datetime.now().timestamp())
                    except json.JSONDecodeError as e:
                        logging.error(f"Price check failed for {token_address}: JSON decode error - {str(e)}")
                        break
                current_price = float(data.get("pair", {}).get("priceUsd", 0))
                market_cap = float(data.get("pair", {}).get("marketCap", 0))
            else:
                cache_key = f"{DEXSCREENER_PAIRS_API}/{token_address}"
                cached_data, cached_time = api_cache.get(cache_key, (None, 0))
                if cached_data and datetime.now().timestamp() - cached_time < 30:
                    data = cached_data
                else:
                    response = session.get(f"{DEXSCREENER_PAIRS_API}/{token_address}")
                    if response.status_code != 200:
                        logging.error(f"Price check failed for {token_address}: Status {response.status_code} - {response.text}")
                        break
                    try:
                        data = response.json()
                        if data is None or not isinstance(data, dict) or "pair" not in data or not data["pair"]:
                            logging.error(f"Price check failed for {token_address}: Invalid JSON response - {response.text}")
                            break
                        api_cache[cache_key] = (data, datetime.now().timestamp())
                    except json.JSONDecodeError as e:
                        logging.error(f"Price check failed for {token_address}: JSON decode error - {str(e)}")
                        break
                current_price = float(data.get("pair", {}).get("priceUsd", 0))
                market_cap = float(data.get("pair", {}).get("marketCap", 0))
            atr = await calculate_atr(token_address, current_price)
            active_positions[token_address]["atr"] = atr
            active_positions[token_address]["trailing_stop"] = current_price - atr * ATR_MULTIPLIER
            active_positions[token_address]["gain"] = current_price / buy_price
            if not (paper or paper_trading) and await check_rug(token_address):
                await execute_trade(token_address, buy=False, paper=paper)
                profit = (current_price - buy_price) * current_buy_amount * 310
                await send_notification(f"😾 Rug alert on {token_address}! Sold at ${current_price:.6f} for {profit:.1f}%! Saved our bag! 😿", is_win=profit > 0)
                loss_streak = loss_streak + 1 if current_price < buy_price else 0
                paper_trades.append({"token": token_address, "sell_price": current_price, "profit": profit, "timestamp": datetime.now().isoformat(), "type": "sell"})
                break
            if current_price <= active_positions[token_address]["trailing_stop"]:
                await execute_trade(token_address, buy=False, paper=paper)
                profit = (current_price - buy_price) * current_buy_amount * 310
                await send_notification(f"💸 Trailing stop hit for {token_address} at ${current_price:.6f} for {profit:.1f}%! 💪", is_win=profit > 0)
                loss_streak = loss_streak + 1 if current_price < buy_price else 0
                paper_trades.append({"token": token_address, "sell_price": current_price, "profit": profit, "timestamp": datetime.now().isoformat(), "type": "sell"})
                break
            await asyncio.sleep(DATA_POLL_INTERVAL)
    except Exception as e:
        logging.error(f"Monitor price error for {token_address}: {str(e)}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message to start the bot."""
    try:
        await send_notification("💃 Dopamine Memecoin Sniper Bot v3.5 is LIVE! Ready to snipe Solana MOONSHOTS! 🌟😘", context)
    except Exception as e:
        logging.error(f"Error in /start command: {str(e)}")
        await send_notification(f"😿 Error in /start command: {str(e)} 💔", context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the list of available commands."""
    try:
        help_message = (
            "🧭 Dopamine Memecoin Sniper Bot Commands\n"
            "/start — Sends a welcome message\n"
            "/help — Shows this command list\n"
            "/status — Displays bot mode, API statuses, and trading info\n"
            "/mode — Shows or switches mode (/mode live [PIN], /mode paper)\n"
            "/preflight — Checks readiness for live trading (balance, APIs, RPC)\n"
            "/wallet — Shows wallet public key and SOL balance\n"
            "/backtest — Runs a backtest using DexScreener data\n"
            "/portfolio — Shows paper trading balance and open positions\n"
            "/trades — Saves and shows paper trade history CSV path\n"
            "/autopaper on|off — Toggles auto paper trading\n"
            "/export — Shows paths to backtest and trade CSVs\n"
            "/ping — Checks if the bot is running"
        )
        await send_notification(help_message, context)
    except Exception as e:
        logging.error(f"Error in /help command: {str(e)}")
        await send_notification(f"😿 Error in /help command: {str(e)} 💔", context)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows bot mode, API statuses, wallet balance, and trading info."""
    try:
        async with AsyncClient(SOLANA_RPC) as sol_client:
            balance_ok, sol_balance = await check_wallet_balance(sol_client)
            balance_status = "✅ Sufficient" if balance_ok else "❌ Low"
            dex_token_status = "✅ OK" if session.get(f"{DEXSCREENER_TOKEN_API}").status_code == 200 else "❌ Failed"
            dex_pairs_status = "✅ OK" if session.get(f"{DEXSCREENER_PAIRS_API}/So11111111111111111111111111111111111111112").status_code == 200 else "❌ Failed"
            shyft_status = "✅ OK" if SHYFT_API_KEY and session.get(f"{SHYFT_API}/So11111111111111111111111111111111111111112", headers={"x-api-key": SHYFT_API_KEY}).status_code == 200 else "❌ Failed"
            rpc_ok = True
            try:
                await sol_client.get_latest_blockhash()
            except Exception:
                rpc_ok = False
            mode = "Paper" if paper_trading else "Live"
            status_message = (
                f"🔍 Dopamine Sniper Bot Status Report\n"
                f"Mode: {mode}\n"
                f"Wallet Balance: {balance_status} ({sol_balance:.4f} SOL)\n"
                f"DexScreener Token API: {dex_token_status}\n"
                f"DexScreener Pairs API: {dex_pairs_status}\n"
                f"Shyft API: {shyft_status}\n"
                f"Solana RPC: {'✅ OK' if rpc_ok else '❌ Failed'}\n"
                f"Active Positions: {len(active_positions)}\n"
                f"Trade Count Today: {trade_count}/{MAX_TRADES_PER_DAY}\n"
                f"Last Trade Day: {last_trade_day}"
            )
            await send_notification(status_message, context)
    except Exception as e:
        logging.error(f"Error in /status command: {str(e)}")
        await send_notification(f"😿 Error in /status command: {str(e)} 💔", context)

async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows current mode or switches between live and paper trading."""
    try:
        global paper_trading
        args = context.args
        if not args:
            mode = "Paper" if paper_trading else "Live"
            await send_notification(f"Current mode: {mode}", context)
            return
        if args[0].lower() == "live" and len(args) == 2 and args[1] == MODE_PIN:
            paper_trading = False
            await send_notification("Switched to LIVE mode! 🚀 Ready to snipe real SOL! 😘", context)
        elif args[0].lower() == "paper":
            paper_trading = True
            await send_notification("Switched to PAPER mode! 📝 Simulating trades safely! 😊", context)
        else:
            await send_notification("Invalid mode or PIN! Use /mode live [PIN] or /mode paper", context)
    except Exception as e:
        logging.error(f"Error in /mode command: {str(e)}")
        await send_notification(f"😿 Error in /mode command: {str(e)} 💔", context)

async def preflight_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checks readiness for live trading (balance, APIs, RPC)."""
    try:
        async with AsyncClient(SOLANA_RPC) as sol_client:
            balance_ok, sol_balance = await check_wallet_balance(sol_client)
            dex_ok = session.get(f"{DEXSCREENER_PAIRS_API}/So11111111111111111111111111111111111111112").status_code == 200
            shyft_ok = SHYFT_API_KEY and session.get(f"{SHYFT_API}/So11111111111111111111111111111111111111112", headers={"x-api-key": SHYFT_API_KEY}).status_code == 200
            rpc_ok = True
            try:
                await sol_client.get_latest_blockhash()
            except Exception:
                rpc_ok = False
            message = (
                f"🛫 Preflight Checks\n"
                f"Wallet Balance: {'✅' if balance_ok else '❌'} ({sol_balance:.4f} SOL, min {MIN_SOL_BALANCE})\n"
                f"DexScreener API: {'✅' if dex_ok else '❌'}\n"
                f"Shyft API: {'✅' if shyft_ok else '❌'}\n"
                f"Solana RPC: {'✅' if rpc_ok else '❌'}\n"
                f"{'Ready for LIVE trading! 🚀' if balance_ok and dex_ok and shyft_ok and rpc_ok else 'Issues detected! Check logs. 😿'}"
            )
            await send_notification(message, context)
    except Exception as e:
        logging.error(f"Error in /preflight command: {str(e)}")
        await send_notification(f"😿 Error in /preflight command: {str(e)} 💔", context)

async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows wallet public key and SOL balance."""
    try:
        async with AsyncClient(SOLANA_RPC) as sol_client:
            keypair = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
            _, sol_balance = await check_wallet_balance(sol_client)
            message = (
                f"💰 Wallet Info\n"
                f"Public Key: {keypair.pubkey()}\n"
                f"SOL Balance: {sol_balance:.4f} SOL"
            )
            await send_notification(message, context)
    except Exception as e:
        logging.error(f"Error in /wallet command: {str(e)}")
        await send_notification(f"😿 Error in /wallet command: {str(e)} 💔", context)

async def backtest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs a backtest using DexScreener data and saves results to CSV."""
    try:
        global paper_trades, current_buy_amount
        paper_trades = []
        current_buy_amount = BUY_AMOUNT_MIN
        await send_notification("🚀 Starting backtest! Results coming soon... 📊", context)
        tokens = []
        for attempt in range(3):
            try:
                response = session.get(DEXSCREENER_TOKEN_API)
                if response.status_code == 200:
                    data = response.json()
                    if data is None or not isinstance(data, list) or not data:
                        logging.error(f"DexScreener Token API invalid response: {response.text}")
                        continue
                    tokens = [token for token in data if token.get("chainId") == "solana" and token.get("tokenAddress")]
                    break
            except Exception as e:
                logging.error(f"DexScreener Token API error: {str(e)}")
            await asyncio.sleep(3 ** attempt)
        if not tokens:
            logging.warning("No Solana tokens found, skipping backtest")
            await send_notification("😿 No Solana tokens found for backtest! Try again later. 💔", context)
            return
        for token in tokens[:100]:
            if len([t for t in paper_trades if t["type"] == "sell" and t["profit"] > 0]) >= MAX_TRADES_PER_DAY and datetime.now().date() == last_trade_day:
                break
            market_cap, buy_price, liquidity = await check_token(token["tokenAddress"])
            if market_cap:
                await execute_trade(token["tokenAddress"], buy=True, paper=True)
                await monitor_price(token["tokenAddress"], buy_price, market_cap, paper=True)
        df = pd.DataFrame(paper_trades)
        win_rate = len(df[(df["type"] == "sell") & (df["profit"] > 0)]) / len(df[df["type"] == "sell"]) * 100 if len(df[df["type"] == "sell"]) > 0 else 0
        avg_profit = df[df["type"] == "sell"]["profit"].mean() if len(df[df["type"] == "sell"]) > 0 else 0
        total_profit = df[df["type"] == "sell"]["profit"].sum() if len(df[df["type"] == "sell"]) > 0 else 0
        csv_path = "/opt/render/project/src/data/backtest_results.csv"
        try:
            with open(csv_path, "w", newline="") as f:
                df.to_csv(f, index=False)
            result = (
                f"📊 Backtest Results\n"
                f"Win Rate: {win_rate:.1f}%\n"
                f"Avg Profit: {avg_profit:.1f}%\n"
                f"Total Profit: {total_profit:.1f}%\n"
                f"Results saved to {csv_path}"
            )
            await send_notification(result, context)
        except Exception as e:
            logging.error(f"Error saving backtest results: {str(e)}")
            await send_notification(f"😿 Failed to save backtest results! {str(e)} 💔", context)
    except Exception as e:
        logging.error(f"Error in /backtest command: {str(e)}")
        await send_notification(f"😿 Error in /backtest command: {str(e)} 💔", context)

async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows paper trading balance and open positions."""
    try:
        paper_balance = BUY_AMOUNT_MIN * 310
        for trade in paper_trades:
            if trade["type"] == "sell":
                paper_balance += trade["profit"]
        positions = "\n".join([f"{token}: ${pos['buy_price']:.6f} (Gain: {pos['gain']:.2f}x, Trailing Stop: ${pos['trailing_stop']:.6f})" for token, pos in active_positions.items()])
        message = (
            f"📈 Paper Portfolio\n"
            f"Balance: ${paper_balance:.2f}\n"
            f"Open Positions ({len(active_positions)}):\n{positions or 'None'}"
        )
        await send_notification(message, context)
    except Exception as e:
        logging.error(f"Error in /portfolio command: {str(e)}")
        await send_notification(f"😿 Error in /portfolio command: {str(e)} 💔", context)

async def trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves and shows paper trade history CSV path."""
    try:
        csv_path = "/opt/render/project/src/data/paper_trades.csv"
        with open(csv_path, "w", newline="") as f:
            pd.DataFrame(paper_trades).to_csv(f, index=False)
        await send_notification(f"📜 Paper Trade History\nSaved to {csv_path}", context)
    except Exception as e:
        logging.error(f"Error in /trades command: {str(e)}")
        await send_notification(f"😿 Failed to save trade history! {str(e)} 💔", context)

async def autopaper_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggles auto paper trading."""
    try:
        global auto_paper
        args = context.args
        if not args:
            await send_notification(f"Auto Paper Trading: {'ON' if auto_paper else 'OFF'}", context)
            return
        if args[0].lower() == "on":
            auto_paper = True
            await send_notification("Auto Paper Trading: ON 📝", context)
        elif args[0].lower() == "off":
            auto_paper = False
            await send_notification("Auto Paper Trading: OFF 🚫", context)
        else:
            await send_notification("Use /autopaper on or /autopaper off", context)
    except Exception as e:
        logging.error(f"Error in /autopaper command: {str(e)}")
        await send_notification(f"😿 Error in /autopaper command: {str(e)} 💔", context)

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows paths to backtest and trade CSVs."""
    try:
        backtest_path = "/opt/render/project/src/data/backtest_results.csv"
        trades_path = "/opt/render/project/src/data/paper_trades.csv"
        message = (
            f"📂 Export Paths\n"
            f"Backtest Results: {backtest_path if os.path.exists(backtest_path) else 'Not generated'}\n"
            f"Paper Trades: {trades_path if os.path.exists(trades_path) else 'Not generated'}"
        )
        await send_notification(message, context)
    except Exception as e:
        logging.error(f"Error in /export command: {str(e)}")
        await send_notification(f"😿 Error in /export command: {str(e)} 💔", context)

async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checks if the bot is running."""
    try:
        await send_notification("🏓 Bot is alive and sniping! 😘", context)
    except Exception as e:
        logging.error(f"Error in /ping command: {str(e)}")
        await send_notification(f"😿 Error in /ping command: {str(e)} 💔", context)

async def start_telegram_bot():
    """Initializes and starts the Telegram bot with all commands."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram bot token or chat ID missing")
        return
    try:
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("status", status_command))
        application.add_handler(CommandHandler("mode", mode_command))
        application.add_handler(CommandHandler("preflight", preflight_command))
        application.add_handler(CommandHandler("wallet", wallet_command))
        application.add_handler(CommandHandler("backtest", backtest_command))
        application.add_handler(CommandHandler("portfolio", portfolio_command))
        application.add_handler(CommandHandler("trades", trades_command))
        application.add_handler(CommandHandler("autopaper", autopaper_command))
        application.add_handler(CommandHandler("export", export_command))
        application.add_handler(CommandHandler("ping", ping_command))
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        logging.info("Telegram bot started successfully")
        await send_notification("💃 Telegram bot started! Ready to accept commands! 😘")
    except Exception as e:
        logging.error(f"Failed to start Telegram bot: {str(e)}")
        await send_notification(f"😿 Telegram bot failed to start! {str(e)} 💔")

async def health_check():
    try:
        while True:
            await send_notification("💖 Dopamine Sniper Bot is running and scanning for MOONSHOTS! 😘")
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
    except Exception as e:
        logging.error(f"Health check error: {str(e)}")

async def handle_callback(request):
    try:
        data = await request.json()
        logging.info(f"Shyft callback received: {data}")
        return web.Response(text="OK")
    except Exception as e:
        logging.error(f"Shyft callback error: {str(e)}")
        return web.Response(text="Error", status=500)

async def handle_health(request):
    logging.info(f"Health check received at {datetime.now()}")
    return web.Response(text="Dopamine Memecoin Sniper Bot is running")

async def start_server():
    try:
        app = web.Application()
        app.add_routes([web.get("/", handle_health), web.post("/callback", handle_callback)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logging.info(f"HTTP server started on port {PORT}")
    except Exception as e:
        logging.error(f"Failed to start HTTP server: {str(e)}")
        await send_notification(f"😿 HTTP server failed to start! {str(e)} 💔")

async def main():
    global trade_count, last_trade_day, processed_tokens, paper_trading
    if BACKTEST_MODE:
        await backtest_command(None)
        return
    asyncio.create_task(start_telegram_bot())
    asyncio.create_task(health_check())
    asyncio.create_task(start_server())
    await send_notification("💃 Dopamine Memecoin Sniper Bot v3.5 is LIVE! Scanning Solana for 1000x MOONSHOTS! 🌟😘")
    while True:
        if trade_count >= MAX_TRADES_PER_DAY and datetime.now().date() == last_trade_day:
            await asyncio.sleep(3600)
            trade_count = 0
            last_trade_day = datetime.now().date()
            processed_tokens.clear()
            logging.info("Reset trade count and processed tokens for new day")
            continue
        tokens = []
        cache_key = DEXSCREENER_TOKEN_API
        cached_data, cached_time = api_cache.get(cache_key, (None, 0))
        if cached_data and datetime.now().timestamp() - cached_time < 30:
            tokens = cached_data
        else:
            for attempt in range(3):
                try:
                    response = session.get(DEXSCREENER_TOKEN_API)
                    if response.status_code == 200:
                        try:
                            data = response.json()
                            if data is None or not isinstance(data, list) or not data:
                                logging.error(f"DexScreener Token API invalid response: {response.text}")
                                continue
                            tokens = [token for token in data if token.get("chainId") == "solana" and token.get("tokenAddress")]
                            api_cache[cache_key] = (tokens, datetime.now().timestamp())
                            break
                        except json.JSONDecodeError as e:
                            logging.error(f"DexScreener Token API JSON decode error: {str(e)}")
                            continue
                    await send_notification(f"😿 DexScreener Token API failed! Status {response.status_code}, attempt {attempt+1}/3 💔")
                    logging.error(f"DexScreener Token API failed: {response.status_code} - {response.text}")
                except Exception as e:
                    await send_notification(f"😿 DexScreener Token API error! {str(e)}, attempt {attempt+1}/3 💔")
                    logging.error(f"DexScreener Token API exception: {str(e)}")
                await asyncio.sleep(3 ** attempt)
        if not tokens:
            logging.warning("No Solana tokens found in DexScreener Token API, skipping this scan")
            await asyncio.sleep(DATA_POLL_INTERVAL)
            continue
        for token in tokens:
            token_address = token.get("tokenAddress")
            if not token_address or token_address in processed_tokens:
                continue
            for attempt in range(3):
                try:
                    market_cap, buy_price, liquidity = await check_token(token_address)
                    if market_cap:
                        logging.info(f"Found {token_address}: ${market_cap}, liquidity ${liquidity}")
                        processed_tokens.add(token_address)
                        success = await execute_trade(token_address, buy=True, paper=auto_paper)
                        if success:
                            asyncio.create_task(monitor_price(token_address, buy_price, market_cap, paper=auto_paper))
                        break
                    break
                except Exception as e:
                    logging.error(f"Token validation error for {token_address}: {str(e)}")
                await asyncio.sleep(3 ** attempt)
        await asyncio.sleep(DATA_POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())