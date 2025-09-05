import asyncio
import requests
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solana.transaction import Transaction
from aiohttp import web
import json
import os
import csv
from datetime import datetime, timedelta
import logging
import pandas as pd
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import dns.asyncresolver

# Setup logging
logging.basicConfig(filename='logs/sniper_bot.log', level=logging.INFO)

# Configuration
WALLET_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
DEXSCREENER_API_KEY = os.getenv("DEXSCREENER_API_KEY", "")  # Required API key
DEXSCREENER_TOKEN_API = f"https://api.dexscreener.com/token-profiles/latest/v1{'?api_key=' + DEXSCREENER_API_KEY if DEXSCREENER_API_KEY else ''}"
DEXSCREENER_PAIRS_API = f"https://api.dexscreener.com/latest/dex/pairs/solana{'?api_key=' + DEXSCREENER_API_KEY if DEXSCREENER_API_KEY else ''}"
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")  # Optional API key
JUPITER_API = f"https://api.jup.ag/price/v6{'?api_key=' + JUPITER_API_KEY if JUPITER_API_KEY else ''}"
BIRDEYE_API = "https://public-api.birdeye.so/public/price"
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")  # Optional API key
RUGCHECK_API = "https://api.rugcheck.xyz/v1/token"
SHYFT_API = "https://api.shyft.to/sol/v1/callback/register"
SHYFT_API_KEY = os.getenv("SHYFT_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BACKTEST_MODE = os.getenv("BACKTEST_MODE", "False") == "True"  # Convert env var to boolean
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

# Async DNS resolver for Jupiter/Birdeye
async def resolve_hostname(hostname):
    resolver = dns.asyncresolver.Resolver()
    resolver.nameservers = ["8.8.8.8", "8.8.4.4"]  # Google DNS
    for attempt in range(3):
        try:
            answers = await resolver.resolve(hostname, "A")
            return [str(answer) for answer in answers]
        except Exception as e:
            logging.error(f"DNS resolution failed for {hostname}, attempt {attempt+1}/3: {str(e)}")
            if attempt < 2:
                await asyncio.sleep(3 ** attempt)  # Exponential backoff: 3s, 9s
    return None

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

async def send_notification(message, is_win=True):
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
    balance = await sol_client.get_balance(keypair.pubkey())
    sol_balance = balance.value / 1_000_000_000  # Lamports to SOL
    if sol_balance < MIN_SOL_BALANCE:
        await send_notification(f"😿 Low balance, bae! Only {sol_balance:.4f} SOL left, need {MIN_SOL_BALANCE} SOL! 💔", is_win=False)
        return False
    return True

async def register_shyft_callback():
    headers = {"x-api-key": SHYFT_API_KEY}
    payload = {
        "network": "mainnet-beta",
        "addresses": [RAYDIUM_PROGRAM],
        "callback_url": os.getenv("CALLBACK_URL")
    }
    for attempt in range(3):
        try:
            response = session.post(SHYFT_API, json=payload, headers=headers)
            if response.status_code == 200:
                await send_notification("💃 Shyft callback locked in, bae! Rug-proof AF! 😘")
                return
            else:
                await send_notification(f"😿 Shyft callback failed, daddy! Status {response.status_code}, attempt {attempt+1}/3 💔", is_win=False)
                logging.error(f"Shyft callback failed: {response.status_code} - {response.text}")
        except Exception as e:
            await send_notification(f"😿 Shyft callback error, daddy! {str(e)}, attempt {attempt+1}/3 💔", is_win=False)
            logging.error(f"Shyft callback exception: {str(e)}")
        await asyncio.sleep(3 ** attempt)  # Exponential backoff: 3s, 9s
    await send_notification("😿 Shyft callback failed after retries, daddy! Check API key and CALLBACK_URL! 💔", is_win=False)

async def check_rug(token_address):
    try:
        rug_response = session.get(f"{RUGCHECK_API}/{token_address}")
        if rug_response.status_code == 200:
            rug_data = rug_response.json()
            if rug_data.get("risk_level") == "danger" or rug_data.get("top_holder_percentage", 0) > 75:
                return True
    except Exception as e:
        logging.error(f"Rugcheck API error for {token_address}: {str(e)}")
    try:
        shyft_response = session.get(f"https://api.shyft.to/sol/v1/events?network=mainnet-beta&address={token_address}", headers={"x-api-key": SHYFT_API_KEY})
        if shyft_response.status_code == 200:
            events = shyft_response.json().get("events", [])
            for event in events:
                if event.get("type") in ["LIQUIDITY_WITHDRAWAL", "TOKEN_BURN"] and event.get("amount") > 8000:
                    return True
                if event.get("type") == "TRANSFER" and event.get("from") == event.get("programId") and event.get("amount") > 800000:
                    return True
    except Exception as e:
        logging.error(f"Shyft rug check error for {token_address}: {str(e)}")
    return False

async def check_token(token_address):
    data = None
    for _ in range(3):
        try:
            response = session.get(f"{DEXSCREENER_PAIRS_API}/{token_address}")
            if response.status_code == 200:
                try:
                    data = response.json()
                    if data is None or not isinstance(data, dict) or "pair" not in data:
                        logging.error(f"DexScreener token check failed for {token_address}: Invalid JSON response - {response.text}")
                        continue
                    break
                except json.JSONDecodeError as e:
                    logging.error(f"DexScreener token check failed for {token_address}: JSON decode error - {str(e)}")
                    continue
            logging.error(f"DexScreener token check failed for {token_address}: Status {response.status_code} - {response.text}")
            jupiter_ips = await resolve_hostname("api.jup.ag")
            if not jupiter_ips:
                logging.error(f"DNS resolution failed for api.jup.ag")
            response = session.get(f"{JUPITER_API}?ids={token_address}", headers={"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {})
            if response.status_code == 200:
                try:
                    data = response.json()
                    if data is None or not isinstance(data, dict) or "data" not in data or token_address not in data.get("data", {}):
                        logging.error(f"Jupiter token check failed for {token_address}: Invalid JSON response - {response.text}")
                        continue
                    break
                except json.JSONDecodeError as e:
                    logging.error(f"Jupiter token check failed for {token_address}: JSON decode error - {str(e)}")
                    continue
            logging.error(f"Jupiter token check failed for {token_address}: Status {response.status_code} - {response.text}")
            birdeye_ips = await resolve_hostname("public-api.birdeye.so")
            if not birdeye_ips:
                logging.error(f"DNS resolution failed for public-api.birdeye.so")
            response = session.get(f"{BIRDEYE_API}?address={token_address}", headers={"x-api-key": BIRDEYE_API_KEY} if BIRDEYE_API_KEY else {})
            if response.status_code == 200:
                try:
                    data = response.json()
                    if data is None or not isinstance(data, dict) or "data" not in data or "value" not in data.get("data", {}):
                        logging.error(f"Birdeye token check failed for {token_address}: Invalid JSON response - {response.text}")
                        continue
                    break
                except json.JSONDecodeError as e:
                    logging.error(f"Birdeye token check failed for {token_address}: JSON decode error - {str(e)}")
                    continue
            logging.error(f"Birdeye token check failed for {token_address}: Status {response.status_code} - {response.text}")
        except Exception as e:
            logging.error(f"Token check error for {token_address}: {str(e)}")
        await asyncio.sleep(3)  # Increased delay for retries
    if data is None or not isinstance(data, dict):
        logging.error(f"Token check failed for {token_address}: No valid response data after retries")
        return None, None, None
    if "dexscreener.com" in response.url:
        market_cap = float(data.get("pair", {}).get("marketCap", 0))
        liquidity = float(data.get("pair", {}).get("liquidity", {}).get("usd", 0))
        price = float(data.get("pair", {}).get("priceUsd", 0))
        price_impact = float(data.get("pair", {}).get("priceChange", {}).get("m5", 0))
        created_at = data.get("pair", {}).get("createdAt", None)
    elif "jup.ag" in response.url:
        market_cap = float(data.get("data", {}).get(token_address, {}).get("marketCap", 0))
        liquidity = float(data.get("data", {}).get(token_address, {}).get("liquidity", 0))
        price = float(data.get("data", {}).get(token_address, {}).get("price", 0))
        price_impact = 0
        created_at = None
    else:  # Birdeye
        market_cap = float(data.get("data", {}).get("mc", 0))
        liquidity = float(data.get("data", {}).get("liquidity", 0))
        price = float(data.get("data", {}).get("value", 0))
        price_impact = float(data.get("data", {}).get("priceChange5m", 0)) / 100
        created_at = None
    max_cap = BASE_MAX_MARKET_CAP / (2 if loss_streak >= LOSS_STREAK_THRESHOLD else 1)
    if not (BASE_MIN_MARKET_CAP <= market_cap <= max_cap) or liquidity < 50000 or abs(price_impact) > MAX_PRICE_IMPACT:
        logging.info(f"Token {token_address} filtered out: market_cap={market_cap}, liquidity={liquidity}, price_impact={price_impact}")
        return None, None, None
    if created_at:
        created_time = datetime.fromtimestamp(created_at / 1000)
        if (datetime.now() - created_time).total_seconds() > MAX_TOKEN_AGE:
            logging.info(f"Token {token_address} filtered out: Too old")
            return None, None, None
    if await check_rug(token_address):
        logging.info(f"Token {token_address} filtered out: Rug detected")
        return None, None, None
    price_volatility = float(data.get("pair", {}).get("priceChange", {}).get("m5", 0)) if "dexscreener.com" in response.url else 0
    if abs(price_volatility) > 15:
        logging.info(f"Token {token_address} filtered out: High volatility")
        return None, None, None
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
                        data=bytes([0]),
                        accounts=[{"pubkey": keypair.pubkey(), "is_signer": True, "is_writable": True}]
                    )
                )
                # await sol_client.send_transaction(tx, keypair, opts={"priority_fee": PRIORITY_FEE})
                if buy:
                    await send_notification(f"🚀 OMG, bae, sniping {token_address} at ${market_cap} with {current_buy_amount} SOL (~$15)! MOON TIME! 😘")
                    active_positions[token_address] = buy_price
                else:
                    await send_notification(f"💸 Sold {token_address}! Banked big, daddy? 🤑" if gain > 1 else f"😢 Ugh, sold {token_address}, this hurts, bae! Let’s bounce back! 💔", is_win=gain > 1)
                    profit = (gain - 1) * current_buy_amount * 310
                    if profit > 0:
                        current_buy_amount = min(BUY_AMOUNT_MAX * 2, current_buy_amount + profit * PROFIT_REINVEST_RATIO / 310)
                    active_positions.pop(token_address, None)
                trade_count += 1
                return True
            except Exception as e:
                await send_notification(f"😿 Trade error, bae! {str(e)} Retrying... 💔", is_win=False)
                logging.error(f"Trade error for {token_address}: {str(e)}")
                await asyncio.sleep(1)
        await send_notification(f"😿 Trade failed after retries, bae! Check SOLANA_RPC or balance! 💔", is_win=False)
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
                jupiter_ips = await resolve_hostname("api.jup.ag")
                if not jupiter_ips:
                    logging.error(f"DNS resolution failed for api.jup.ag")
                response = session.get(f"{JUPITER_API}?ids={token_address}", headers={"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {})
                if response.status_code != 200:
                    birdeye_ips = await resolve_hostname("public-api.birdeye.so")
                    if not birdeye_ips:
                        logging.error(f"DNS resolution failed for public-api.birdeye.so")
                    response = session.get(f"{BIRDEYE_API}?address={token_address}", headers={"x-api-key": BIRDEYE_API_KEY} if BIRDEYE_API_KEY else {})
                    if response.status_code != 200:
                        logging.error(f"Price check failed for {token_address}: Status {response.status_code} - {response.text}")
                        break
                try:
                    data = response.json()
                    if data is None or not isinstance(data, dict):
                        logging.error(f"Price check failed for {token_address}: Invalid JSON response - {response.text}")
                        break
                except json.JSONDecodeError as e:
                    logging.error(f"Price check failed for {token_address}: JSON decode error - {str(e)}")
                    break
                if "jup.ag" in response.url:
                    if token_address not in data.get("data", {}):
                        logging.error(f"Price check failed for {token_address}: Token not found in Jupiter response")
                        break
                    current_price = float(data.get("data", {}).get(token_address, {}).get("price", 0))
                    market_cap = float(data.get("data", {}).get(token_address, {}).get("marketCap", 0))
                else:  # Birdeye
                    if "value" not in data.get("data", {}):
                        logging.error(f"Price check failed for {token_address}: Invalid Birdeye response - {response.text}")
                        break
                    current_price = float(data.get("data", {}).get("value", 0))
                    market_cap = float(data.get("data", {}).get("mc", 0))
            else:
                try:
                    data = response.json()
                    if data is None or not isinstance(data, dict) or "pair" not in data:
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
            await send_notification(f"😾 Rug alert on {token_address}! Sold at ${current_price} for {profit:.1f}%! Saved our bag, bae! 😿", is_win=profit > 0)
            loss_streak = loss_streak + 1 if current_price < buy_price else 0
            backtest_trades.append({"token": token_address, "profit": profit, "win": profit > 0})
            break
        if gain >= EARLY_SELL_PROFIT and (await check_rug(token_address) or current_price <= peak_price * TRAILING_STOP):
            await execute_trade(token_address, buy=False, backtest=backtest)
            profit = (current_price - buy_price) * current_buy_amount * 310
            await send_notification(f"😿 Early sell on {token_address} at ${current_price} for {profit:.1f}%! Dodged a dump, daddy! 💪", is_win=profit > 0)
            loss_streak = loss_streak + 1 if current_price < buy_price else 0
            backtest_trades.append({"token": token_address, "profit": profit, "win": profit > 0})
            break
        if current_price <= buy_price * STOP_LOSS:
            await execute_trade(token_address, buy=False, backtest=backtest)
            await send_notification(f"😡 Oof, stop loss hit for {token_address}! This SUCKS, bae! Let’s chase a MOONSHOT! 😢", is_win=False)
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

async def backtest():
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
        with open("logs/backtest_results.csv", "w", newline="") as f:
            df.to_csv(f, index=False)
        logging.info(f"Backtest: Win rate {win_rate:.1f}%, Avg profit {avg_profit:.1f}%, Total profit {total_profit:.1f}%")
        await send_notification(f"📊 Backtest slayed, bae! Win rate: {win_rate:.1f}%, Avg profit: {avg_profit:.1f}%, Total: {total_profit:.1f}% 🌟😍")
    except FileNotFoundError:
        await send_notification("😿 Oof, no backtest data, daddy! Upload backtest_data.csv! 💔", is_win=False)

async def health_check():
    while True:
        await send_notification("💖 Yo, bae, I’m still alive and hunting MOONSHOTS! 😘")
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)

async def handle_callback(request):
    data = await request.json()
    logging.info(f"Shyft callback received: {data}")
    return web.Response(text="OK")

async def handle_health(request):
    return web.Response(text="Bot is running")

async def start_server():
    app = web.Application()
    app.add_routes([web.get("/", handle_health), web.post("/callback", handle_callback)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"HTTP server running on port {PORT}")

async def main():
    global trade_count, last_trade_day, processed_tokens
    if BACKTEST_MODE:
        await backtest()
        return
    await register_shyft_callback()
    asyncio.create_task(health_check())
    asyncio.create_task(start_server())
    await send_notification("💃 Yo, bae, your crypto queen’s LIVE and rug-proof! Let’s stack BAGS! 🌟😘")
    while True:
        if trade_count >= MAX_TRADES_PER_DAY and datetime.now().date() == last_trade_day:
            await asyncio.sleep(3600)
            trade_count = 0
            last_trade_day = datetime.now().date()
            processed_tokens.clear()  # Reset processed tokens daily
            continue
        for attempt in range(3):
            try:
                response = session.get(DEXSCREENER_TOKEN_API)
                if response.status_code == 200:
                    try:
                        data = response.json()
                        if data is None or not isinstance(data, list):
                            logging.error(f"DexScreener Token API invalid response: {response.text}")
                            continue
                        break
                    except json.JSONDecodeError as e:
                        logging.error(f"DexScreener Token API JSON decode error: {str(e)}")
                        continue
                await send_notification(f"😿 DexScreener Token API failed, bae! Status {response.status_code}, attempt {attempt+1}/3 💔", is_win=False)
                logging.error(f"DexScreener Token API failed: {response.status_code} - {response.text}")
            except Exception as e:
                await send_notification(f"😿 DexScreener Token API error, bae! {str(e)}, attempt {attempt+1}/3 💔", is_win=False)
                logging.error(f"DexScreener Token API exception: {str(e)}")
            await asyncio.sleep(3 ** attempt)  # Exponential backoff: 3s, 9s
        else:
            await send_notification("😿 DexScreener Token API down after retries, bae! Falling back to pairs... 💔", is_win=False)
            for attempt in range(3):
                try:
                    response = session.get(DEXSCREENER_PAIRS_API)
                    if response.status_code == 200:
                        try:
                            data = response.json()
                            if data is None or not isinstance(data, dict) or "pairs" not in data:
                                logging.error(f"DexScreener Pairs API invalid response: {response.text}")
                                continue
                            break
                        except json.JSONDecodeError as e:
                            logging.error(f"DexScreener Pairs API JSON decode error: {str(e)}")
                            continue
                    await send_notification(f"😿 DexScreener Pairs API failed, bae! Status {response.status_code}, attempt {attempt+1}/3 💔", is_win=False)
                    logging.error(f"DexScreener Pairs API failed: {response.status_code} - {response.text}")
                except Exception as e:
                    await send_notification(f"😿 DexScreener Pairs API error, bae! {str(e)}, attempt {attempt+1}/3 💔", is_win=False)
                    logging.error(f"DexScreener Pairs API exception: {str(e)}")
                await asyncio.sleep(3 ** attempt)
            if response.status_code != 200:
                await send_notification(f"😿 DexScreener Pairs API down too, bae! Retrying in {DATA_POLL_INTERVAL}s... 💔", is_win=False)
                await asyncio.sleep(DATA_POLL_INTERVAL)
                continue
            data = {"pairs": data.get("pairs", [])}  # Adapt pairs response to tokens format
        tokens = [token for token in data if token.get("chainId") == "solana"] if isinstance(data, list) else data.get("pairs", [])
        for token in tokens:
            token_address = token.get("tokenAddress") if isinstance(data, list) else token.get("baseToken", {}).get("address")
            if not token_address or token_address in processed_tokens:
                continue
            # Validate token_address with pairs endpoint
            for attempt in range(3):
                try:
                    pair_response = session.get(f"{DEXSCREENER_PAIRS_API}/{token_address}")
                    if pair_response.status_code == 200:
                        try:
                            pair_data = pair_response.json()
                            if pair_data is None or not isinstance(pair_data, dict) or "pair" not in pair_data:
                                logging.error(f"Invalid pair data for {token_address}: {pair_response.text}")
                                break
                        except json.JSONDecodeError as e:
                            logging.error(f"Invalid pair JSON for {token_address}: {str(e)}")
                            break
                        market_cap, buy_price, liquidity = await check_token(token_address)
                        if market_cap:
                            logging.info(f"Found {token_address}: ${market_cap}, liquidity ${liquidity}")
                            processed_tokens.add(token_address)
                            success = await execute_trade(token_address, buy=True)
                            if success:
                                asyncio.create_task(monitor_price(token_address, buy_price, market_cap))
                        break
                    logging.error(f"Invalid token address {token_address}: Status {pair_response.status_code} - {pair_response.text}")
                except Exception as e:
                    logging.error(f"Token validation error for {token_address}: {str(e)}")
                await asyncio.sleep(3 ** attempt)  # Exponential backoff: 3s, 9s
        await asyncio.sleep(DATA_POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())