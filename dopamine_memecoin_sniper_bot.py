import asyncio
import requests
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solana.transaction import Transaction
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
from solders.instruction import Instruction

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
BACKTEST_MODE = os.getenv("BACKTEST_MODE", "False") == "True"
BASE_MIN_MARKET_CAP = 10000  # $10k
BASE_MAX_MARKET_CAP = 200000  # $200k
BUY_AMOUNT_MIN = 0.048387  # ~$15 at $310/SOL
BUY_AMOUNT_MAX = 0.048387  # ~$15 at $310/SOL
PROFIT_REINVEST_RATIO = 0.5  # Reinvest 50% profits
EARLY_SELL_PROFIT = 1.3  # 1.3x for rug/dump
STOP_LOSS = 0.9  # 10% loss
TRAILING_STOP = 0.98  # 2% below peak
SLIPPAGE = 0.03  # 3% slippage
MAX_PRICE_IMPACT = 0.05  # 5% max price impact
LOSS_STREAK_THRESHOLD = 3  # Pain after 3 losses
MAX_TRADES_PER_DAY = 4  # 3-4 signals daily
RAYDIUM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
HEALTH_CHECK_INTERVAL = 300  # 5 minutes
DATA_POLL_INTERVAL = 10  # 10 seconds for faster memecoin sniping
PRIORITY_FEE = 0.0005  # 0.0005 SOL base fee
MIN_SOL_BALANCE = 0.15  # 0.15 SOL minimum
MAX_TOKEN_AGE = 6 * 3600  # 6 hours in seconds
PORT = int(os.getenv("PORT", 8080))  # Render port, default 8080

# Fallback token engine (from provided document)
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
backtest_trades = []
active_positions = {}  # token: buy_price
backtest_data_cache = {}
processed_tokens = set()  # Track processed token addresses

async def send_notification(message, context=None, is_win=True):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram bot token or chat ID missing")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    for _ in range(3):
        try:
            response = session.post(url, json=payload)
            if response.status_code == 200:
                break
            logging.error(f"Telegram notification failed: {response.status_code} - {response.text}")
        except Exception as e:
            logging.error(f"Telegram notification error: {str(e)}")
        await asyncio.sleep(1)
    logging.info(f"{datetime.now()}: {message}")

async def check_wallet_balance(sol_client):
    keypair = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
    try:
        balance = await sol_client.get_balance(keypair.pubkey())
        sol_balance = balance.value / 1_000_000_000  # Lamports to SOL
        if sol_balance < MIN_SOL_BALANCE:
            await send_notification(f"😿 Low balance! Only {sol_balance:.4f} SOL left, need {MIN_SOL_BALANCE} SOL! 💔")
            return False
        return True
    except Exception as e:
        logging.error(f"Wallet balance check failed: {str(e)}")
        await send_notification(f"😿 Wallet balance check failed! {str(e)} 💔")
        return False

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

async def check_token(token_address):
    data = None
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

async def execute_trade(token_address, buy=True, backtest=False):
    global loss_streak, trade_count, active_positions, gain, current_buy_amount
    if backtest:
        logging.info(f"Backtest: {'Buying' if buy else 'Selling'} {token_address} with {current_buy_amount} SOL")
        if buy:
            active_positions[token_address] = buy_price
        else:
            profit = (gain - 1) * current_buy_amount * 310  # Convert to USD at $310/SOL
            if profit > 0:
                current_buy_amount = min(BUY_AMOUNT_MAX * 2, current_buy_amount + profit * PROFIT_REINVEST_RATIO / 310)
            active_positions.pop(token_address, None)
        return True
    async with AsyncClient(SOLANA_RPC) as sol_client:
        keypair = Keypair.from_base58_string(WALLET_PRIVATE_KEY)
        if not await check_wallet_balance(sol_client):
            return False
        tx = Transaction()
        tx.add_fee_payer(keypair.pubkey())
        for _ in range(3):
            try:
                blockhash = await sol_client.get_latest_blockhash()
                tx.recent_blockhash = blockhash.value.blockhash
                tx.fee_payer = keypair.pubkey()
                tx.add_instruction(
                    Instruction(
                        program_id=RAYDIUM_PROGRAM,
                        data=bytes([0]),  # Placeholder: Replace with actual Raydium swap instruction
                        accounts=[{"pubkey": keypair.pubkey(), "is_signer": True, "is_writable": True}]
                    )
                )
                # await sol_client.send_transaction(tx, keypair, opts={"priority_fee": PRIORITY_FEE})
                if buy:
                    await send_notification(f"🚀 Sniping {token_address} at ${market_cap} with {current_buy_amount} SOL (~$15)! MOON TIME! 😘")
                    active_positions[token_address] = buy_price
                else:
                    await send_notification(f"💸 Sold {token_address}! Profit: {gain:.2f}x 🤑" if gain > 1 else f"😢 Sold {token_address}, loss taken. Let’s bounce back! 💔", is_win=gain > 1)
                    profit = (gain - 1) * current_buy_amount * 310
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

async def monitor_price(token_address, buy_price, market_cap, backtest=False):
    global loss_streak, backtest_trades, gain
    peak_price = buy_price
    start_time = datetime.now()
    while (datetime.now() - start_time).seconds < 7200:
        if backtest:
            data = next_backtest_data(token_address, start_time + timedelta(seconds=len(backtest_trades) * 600))
            if not data:
                break
            current_price = float(data["price"])
            market_cap = float(data["market_cap"])
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
            except json.JSONDecodeError as e:
                logging.error(f"Price check failed for {token_address}: JSON decode error - {str(e)}")
                break
            current_price = float(data.get("pair", {}).get("priceUsd", 0))
            market_cap = float(data.get("pair", {}).get("marketCap", 0))
        peak_price = max(peak_price, current_price)
        gain = current_price / buy_price
        if not backtest and await check_rug(token_address):
            await execute_trade(token_address, buy=False, backtest=backtest)
            profit = (current_price - buy_price) * current_buy_amount * 310
            await send_notification(f"😾 Rug alert on {token_address}! Sold at ${current_price:.2f} for {profit:.1f}%! Saved our bag! 😿", is_win=profit > 0)
            loss_streak = loss_streak + 1 if current_price < buy_price else 0
            backtest_trades.append({"token": token_address, "profit": profit, "win": profit > 0})
            break
        if gain >= EARLY_SELL_PROFIT and (await check_rug(token_address) or current_price <= peak_price * TRAILING_STOP):
            await execute_trade(token_address, buy=False, backtest=backtest)
            profit = (current_price - buy_price) * current_buy_amount * 310
            await send_notification(f"💸 Early sell on {token_address} at ${current_price:.2f} for {profit:.1f}%! Dodged a dump! 💪", is_win=profit > 0)
            loss_streak = loss_streak + 1 if current_price < buy_price else 0
            backtest_trades.append({"token": token_address, "profit": profit, "win": profit > 0})
            break
        if current_price <= buy_price * STOP_LOSS:
            await execute_trade(token_address, buy=False, backtest=backtest)
            await send_notification(f"😡 Stop loss hit for {token_address} at ${current_price:.2f}! Let’s chase the next MOONSHOT! 😢")
            loss_streak += 1
            backtest_trades.append({"token": token_address, "profit": -10 * current_buy_amount * 310, "win": False})
            break
        await asyncio.sleep(DATA_POLL_INTERVAL if not backtest else 0.1)

def next_backtest_data(token_address, timestamp):
    if token_address not in backtest_data_cache:
        try:
            with open(BACKTEST_DATA, "r") as f:
                reader = csv.DictReader(f)
                backtest_data_cache[token_address] = [
                    {"price": row["price"], "market_cap": row["market_cap"], "timestamp": datetime.fromisoformat(row["timestamp"])}
                    for row in reader if row["token"] == token_address
                ]
        except FileNotFoundError:
            return None
    data = backtest_data_cache.get(token_address, [])
    for row in data:
        if row["timestamp"] >= timestamp:
            return row
    return None

async def backtest(context=None):
    global backtest_trades, current_buy_amount
    backtest_trades = []
    current_buy_amount = BUY_AMOUNT_MIN
    try:
        with open(BACKTEST_DATA, "r") as f:
            reader = csv.DictReader(f)
            tokens = sorted(set(row["token"] for row in reader), key=lambda x: x)[:100]
            for token in tokens:
                if len([t for t in backtest_trades if t["win"]]) >= MAX_TRADES_PER_DAY and datetime.now().date() == last_trade_day:
                    break
                market_cap, buy_price, liquidity = await check_token(token)
                if market_cap:
                    await execute_trade(token, buy=True, backtest=True)
                    await monitor_price(token, buy_price, market_cap, backtest=True)
        df = pd.DataFrame(backtest_trades)
        win_rate = len(df[df["win"]]) / len(df) * 100 if len(df) > 0 else 0
        avg_profit = df["profit"].mean() if len(df) > 0 else 0
        total_profit = df["profit"].sum() if len(df) > 0 else 0
        with open