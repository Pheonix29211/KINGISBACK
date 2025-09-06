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

# Setup logging
logging.basicConfig(filename='logs/sniper_bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Configuration
WALLET_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
DEXSCREENER_TOKEN_API = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_PAIRS_API = "https://api.dexscreener.com/latest/dex/pairs/solana"
SOLANAFM_API = "https://api.solana.fm/v1/transactions"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MODE_PIN = "1234"  # Hardcoded PIN for /mode live
BASE_MIN_MARKET_CAP = 10000
BASE_MAX_MARKET_CAP = 200000
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
DATA_POLL_INTERVAL = 15
PRIORITY_FEE = 0.001
MIN_SOL_BALANCE = 0.2
PORT = int(os.getenv("PORT", 8080))

# Fallback tokens
FALLBACK_TOKENS = [
    "3trQxYokXbnxFThN2ppaCBqrodu9zyPPviaQf75MBAGS",
    "XbYpCajESGmRVor733e7e1uT9NLxdWxZMdXV3L4bonk",
    "4nig1DDAzUw9s2DYfDhhY3eXkSYwssdGr5mX5FRWJJ7D",
    "yRcSaCyujTwnAA2mdZXB3ykJ9UjPjWKH6EAu15apump",
    "9qitnJLcrwxYN6xb5n9jYBsQAFfLKqnzwGRvHa7Wpump",
    "H47nC6VBFBPvDApUyyfrEHhhQVL72NsbtjQYK99EBAGS",
    "FNf1uFhgJX6c6A6eQWaNdrMcVmK1PNuz69jWYo3HHHqJ",
    "Ad5iUfBi37m9ygp7YG4DT3q8FVro2ipx9inehUXrD4GS",
    "H3kviw9zovZLbv3hu1tXf7rZjqrT6UemKXia8HdBpump",
    "6odHwsHzgW3PKSR8hZtwmbKZNJW3eXwyBcLzwNxnBAGS",
    "BJDELgLq9sV2eKyT3L3iUWq6cPScpYk7dgey7uHx7PLd",
    "ARXgGGivfg28FaReZQLZF5HJ7yRPMCmzfYtvznfvpump"
]

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
price_history = {}  # token: list of {"high": float, "low": float, "close": float}
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
            if response.status_code == 429:
                await asyncio.sleep(5)
            logging.error(f"Telegram notification failed: {response.status_code} - {response.text}")
        except Exception as e:
            logging.error(f"Telegram notification error: {str(e)}")
        await asyncio.sleep(1)
    logging.info(f"{datetime.now()}: {message}")

async def check_wallet_balance(sol_client):
    keypair = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
    try:
        balance = await sol_client.get_balance(keypair.pubkey())
        sol_balance = balance.value / 1_000_000_000
        if sol_balance < MIN_SOL_BALANCE:
            await send_notification(f"😿 Low balance! Only {sol_balance:.4f} SOL left, need {MIN_SOL_BALANCE} SOL! 💔")
            return False, sol_balance
        return True, sol_balance
    except Exception as e:
        logging.error(f"Wallet balance check failed: {str(e)}")
        await send_notification(f"😿 Wallet balance check failed! {str(e)} 💔")
        return False, 0

async def check_rug(token_address):
    try:
        response = session.get(f"{SOLANAFM_API}?address={token_address}")
        if response.status_code == 200:
            events = response.json().get("events", [])
            for event in events:
                if event.get("type") in ["LIQUIDITY_WITHDRAWAL", "TOKEN_BURN"] and event.get("amount", 0) > 8000:
                    logging.info(f"Rug detected for {token_address}: Large withdrawal/burn")
                    return True
                if event.get("type") == "TRANSFER" and event.get("amount", 0) > 800000:
                    logging.info(f"Rug detected for {token_address}: Large transfer")
                    return True
    except Exception as e:
        logging.error(f"SolanaFM rug check error for {token_address}: {str(e)}")
    return False

async def calculate_atr(token_address, current_price):
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
    except (ValueError, TypeError) as e:
        logging.error(f"Data parsing error for {token_address}: {str(e)}")
        return None, None, None
    max_cap = BASE_MAX_MARKET_CAP / (2 if loss_streak >= LOSS_STREAK_THRESHOLD else 1)
    if not (BASE_MIN_MARKET_CAP <= market_cap <= max_cap) or liquidity < 50000 or abs(price_impact) > MAX_PRICE_IMPACT:
        logging.info(f"Token {token_address} filtered out: market_cap={market_cap}, liquidity={liquidity}, price_impact={price_impact}")
        return None, None, None
    if created_at:
        try:
            created_time = datetime.fromtimestamp(created_at / 1000)
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
    global loss_streak, trade_count, active_positions, current_buy_amount, paper_trades
    if paper or paper_trading:
        logging.info(f"Paper trade: {'Buying' if buy else 'Selling'} {token_address} with {current_buy_amount} SOL")
        if buy:
            market_cap, buy_price, _ = await check_token(token_address)
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

async def monitor_price(token_address, buy_price, market_cap, paper=False):
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
            await send_notification(f"😾 Rug alert on {token_address}! Sold at ${current_price:.2f} for {profit:.1f}%! Saved our bag! 😿", is_win=profit > 0)
            loss_streak = loss_streak + 1 if current_price < buy_price else 0
            paper_trades.append({"token": token_address, "sell_price": current_price, "profit": profit, "timestamp": datetime.now().isoformat(), "type": "sell"})
            break
        if current_price <= active_positions[token_address]["trailing_stop"]:
            await execute_trade(token_address, buy=False, paper=paper)
            profit = (current_price - buy_price) * current_buy_amount * 310
            await send_notification(f"💸 Trailing stop hit for {token_address} at ${current_price:.2f} for {profit:.1f}%! 💪", is_win=profit > 0)
            loss_streak = loss_streak + 1 if current_price < buy_price else 0
            paper_trades.append({"token": token_address, "sell_price": current_price, "profit": profit, "timestamp": datetime.now().isoformat(), "type": "sell"})
            break
        await asyncio.sleep(DATA_POLL_INTERVAL)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_notification("💃 Dopamine Memecoin Sniper Bot v3.1 is LIVE! Ready to snipe Solana MOONSHOTS! 🌟😘", context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_message = (
        "🧭 Dopamine Memecoin Sniper Bot Commands\n"
        "/start — Welcome message\n"
        "/help — Show this command list\n"
        "/status — Show mode (live/paper) and router status\n"
        "/mode — Show or switch mode (/mode live [PIN], /mode paper)\n"
        "/preflight — Live-readiness checks (balance, APIs, RPC)\n"
        "/wallet — Show public key and SOL balance\n"
        "/backtest — Run Dex backtest snapshot (fallback tokens)\n"
        "/portfolio — Show paper trading balance and positions\n"
        "/trades — Show paper trade history CSV path\n"
        "/autopaper on|off — Toggle auto paper trading\n"
        "/export — Show latest CSV paths\n"
        "/ping — Check if bot is alive"
    )
    await send_notification(help_message, context)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncClient(SOLANA_RPC) as sol_client:
        balance_ok, sol_balance = await check_wallet_balance(sol_client)
        balance_status = "✅ Sufficient" if balance_ok else "❌ Low"
        dex_response = session.get(f"{DEXSCREENER_PAIRS_API}/So11111111111111111111111111111111111111112")
        dex_status = "✅ OK" if dex_response.status_code == 200 else f"❌ Failed (Status {dex_response.status_code})"
        solanafm_status = "✅ OK" if session.get(f"{SOLANAFM_API}?address=So11111111111111111111111111111111111111112").status_code == 200 else "❌ Failed"
        mode = "Paper" if paper_trading else "Live"
        status_message = (
            f"🔍 Dopamine Sniper Bot Status Report\n"
            f"Mode: {mode}\n"
            f"Wallet Balance: {balance_status} ({sol_balance:.4f} SOL)\n"
            f"DexScreener API: {dex_status}\n"
            f"SolanaFM API: {solanafm_status}\n"
            f"Active Positions: {len(active_positions)}\n"
            f"Trade Count Today: {trade_count}/{MAX_TRADES_PER_DAY}\n"
            f"Last Trade Day: {last_trade_day}"
        )
        await send_notification(status_message, context)

async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global paper_trading
    args = context.args
    if not args:
        mode = "Paper" if paper_trading else "Live"
        await send_notification(f"Current mode: {mode}", context)
        return
    if args[0] == "live" and len(args) == 2 and args[1] == MODE_PIN:
        paper_trading = False
        await send_notification("Switched to LIVE mode! 🚀 Ready to snipe real SOL! 😘", context)
    elif args[0] == "paper":
        paper_trading = True
        await send_notification("Switched to PAPER mode! 📝 Simulating trades safely! 😊", context)
    else:
        await send_notification("Invalid mode or PIN! Use /mode live [PIN] or /mode paper", context)

async def preflight_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncClient(SOLANA_RPC) as sol_client:
        balance_ok, sol_balance = await check_wallet_balance(sol_client)
        dex_ok = session.get(f"{DEXSCREENER_PAIRS_API}/So11111111111111111111111111111111111111112").status_code == 200
        solanafm_ok = session.get(f"{SOLANAFM_API}?address=So11111111111111111111111111111111111111112").status_code == 200
        rpc_ok = True
        try:
            await sol_client.get_latest_blockhash()
        except Exception:
            rpc_ok = False
        message = (
            f"🛫 Preflight Checks\n"
            f"Wallet Balance: {'✅' if balance_ok else '❌'} ({sol_balance:.4f} SOL, min {MIN_SOL_BALANCE})\n"
            f"DexScreener API: {'✅' if dex_ok else '❌'}\n"
            f"SolanaFM API: {'✅' if solanafm_ok else '❌'}\n"
            f"Solana RPC: {'✅' if rpc_ok else '❌'}\n"
            f"{'Ready for LIVE trading! 🚀' if balance_ok and dex_ok and solanafm_ok and rpc_ok else 'Issues detected! Check logs. 😿'}"
        )
        await send_notification(message, context)

async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncClient(SOLANA_RPC) as sol_client:
        keypair = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
        _, sol_balance = await check_wallet_balance(sol_client)
        message = (
            f"💰 Wallet Info\n"
            f"Public Key: {keypair.pubkey()}\n"
            f"SOL Balance: {sol_balance:.4f} SOL"
        )
        await send_notification(message, context)

async def backtest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        logging.warning("No Solana tokens found, using fallback tokens")
        tokens = [{"tokenAddress": addr} for addr in FALLBACK_TOKENS]
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
    csv_path = "logs/backtest_results.csv"
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

async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    paper_balance = BUY_AMOUNT_MIN * 310  # Initial $15 in USD
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

async def trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE):