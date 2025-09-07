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
from solders.instruction import Instruction, AccountMeta
from solders.pubkey import Pubkey

# Setup logging
logging.basicConfig(filename='logs/sniper_bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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
ENTRY_MC_MIN = 75000  # $75k
ENTRY_MC_MAX = 2000000  # $2M
ENTRY_LP_MIN_USD = 30000  # $30k
ENTRY_LP_TO_MCAP_MIN = 0.15  # 15%
ENTRY_POOL_AGE_MIN = 60  # 60 seconds
VOL1H_MIN = 50000  # $50k
ACCEL_MIN = 0.8  # 0.8
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
HEALTH_CHECK_INTERVAL = 300 # 5 minutes
DATA_POLL_INTERVAL = 10 # 10 seconds for faster memecoin sniping
PRIORITY_FEE = 0.002  # Increased to 0.002 SOL for better transaction success
MIN_SOL_BALANCE = 0.15 # 0.15 SOL minimum
MAX_TOKEN_AGE = 6 * 3600 # 6 hours in seconds
PORT = int(os.getenv("PORT", 8080)) # Render port, default 8080

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
active_positions = {} # token: buy_price
backtest_data_cache = {}
processed_tokens = set() # Track processed token addresses

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
        sol_balance = balance.value / 1_000_000_000 # Lamports to SOL
        if sol_balance < MIN_SOL_BALANCE:
            await send_notification(f"😿 Low balance! Only {sol_balance:.4f} SOL left, need {MIN_SOL_BALANCE} SOL! 💔")
            return False
        return True
    except Exception as e:
        logging.error(f"Wallet balance check failed: {str(e)}")
        await send_notification(f"😿 Wallet balance check failed! {str(e)} 💔")
        return False

async def check_rug(token_address):
    headers = {"x-api-key": SHYFT_API_KEY}
    response = session.get(f"{SHYFT_API}/{token_address}", headers=headers)
    if response.status_code == 200:
        data = response.json().get("result", {})
        if data.get("is_suspicious") or not data.get("liquidity_locked"):
            logging.info(f"Rug detected for {token_address}: Suspicious or unlocked liquidity")
            return True
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
        volume_1h = float(data.get("pair", {}).get("volume", {}).get("h1", 0))
        price_change_1h = float(data.get("pair", {}).get("priceChange", {}).get("h1", 0))
        acceleration = price_change_1h / 60 if price_change_1h > 0 else 0  # Simple acceleration calculation (change per minute)
    except (ValueError, TypeError) as e:
        logging.error(f"Data parsing error for {token_address}: {str(e)}")
        return None, None, None
    max_cap = BASE_MAX_MARKET_CAP / (2 if loss_streak >= LOSS_STREAK_THRESHOLD else 1)
    if not (ENTRY_MC_MIN <= market_cap <= ENTRY_MC_MAX) or liquidity < ENTRY_LP_MIN_USD or (liquidity / market_cap) < ENTRY_LP_TO_MCAP_MIN or abs(price_impact) > MAX_PRICE_IMPACT or volume_1h < VOL1H_MIN or acceleration < ACCEL_MIN:
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

async def execute_trade(token_address, buy=True, backtest=False):
    global loss_streak, trade_count, active_positions, gain, current_buy_amount
    if backtest:
        logging.info(f"Backtest: {'Buying' if buy else 'Selling'} {token_address} with {current_buy_amount} SOL")
        if buy:
            active_positions[token_address] = buy_price
        else:
            profit = (gain - 1) * current_buy_amount * 310 # Convert to USD at $310/SOL
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
                        data=bytes([0]), # Placeholder: Replace with actual Raydium swap instruction
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
        with open("logs/backtest_results.csv", "w", newline="") as f:
            df.to_csv(f, index=False)
        result = f"📊 Backtest Results\nWin Rate: {win_rate:.1f}%\nAvg Profit: {avg_profit:.1f}%\nTotal Profit: {total_profit:.1f}%"
        logging.info(result)
        await send_notification(result, context)
    except FileNotFoundError:
        await send_notification("😿 No backtest data found! Upload backtest_data.csv to proceed! 💔", context)
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncClient(SOLANA_RPC) as sol_client:
        balance_ok = await check_wallet_balance(sol_client)
        balance_status = "✅ Sufficient" if balance_ok else "❌ Low"
        dex_response = session.get(f"{DEXSCREENER_PAIRS_API}/So11111111111111111111111111111111111111112")
        dex_status = "✅ OK" if dex_response.status_code == 200 else f"❌ Failed (Status {dex_response.status_code})"
        solanafm_status = "✅ OK" if session.get(f"{SOLANAFM_API}?address={RAYDIUM_PROGRAM}").status_code == 200 else "❌ Failed"
        status_message = (
            f"🔍 KINGISBACK Sniper Bot Status Report\n"
            f"Wallet Balance: {balance_status}\n"
            f"DexScreener API: {dex_status}\n"
            f"SolanaFM API: {solanafm_status}\n"
            f"Active Positions: {len(active_positions)}\n"
            f"Trade Count Today: {trade_count}/{MAX_TRADES_PER_DAY}\n"
            f"Last Trade Day: {last_trade_day}"
        )
        await send_notification(status_message, context)
async def logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logic_message = (
        f"📜 KINGISBACK Sniper Bot Trading Logic\n"
        f"Market Cap Range: ${BASE_MIN_MARKET_CAP:,} - ${BASE_MAX_MARKET_CAP:,}\n"
        f"Min Liquidity: $50,000\n"
        f"Max Price Impact: {MAX_PRICE_IMPACT*100:.1f}%\n"
        f"Max Volatility (5m): 15%\n"
        f"Max Token Age: {MAX_TOKEN_AGE//3600} hours\n"
        f"Buy Amount: {BUY_AMOUNT_MIN:.6f} SOL (~$15)\n"
        f"Early Sell Profit: {EARLY_SELL_PROFIT}x\n"
        f"Stop Loss: {STOP_LOSS*100:.1f}%\n"
        f"Trailing Stop: {TRAILING_STOP*100:.1f}% below peak\n"
        f"Slippage: {SLIPPAGE*100:.1f}%\n"
        f"Max Trades/Day: {MAX_TRADES_PER_DAY}\n"
        f"Priority Fee: {PRIORITY_FEE:.6f} SOL\n"
        f"Min SOL Balance: {MIN_SOL_BALANCE:.2f} SOL"
    )
    await send_notification(logic_message, context)
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_message = (
        f"📚 KINGISBACK Sniper Bot Commands\n"
        f"?status - Check bot health, wallet balance, and API status\n"
        f"?logic - View trading logic and parameters\n"
        f"?backtest - Run backtest and get results\n"
        f"?help - Show this help message"
    )
    await send_notification(help_message, context)
async def backtest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_notification("🚀 Starting backtest! Results coming soon... 📊", context)
    await backtest(context)
async def start_telegram_bot():
    if not TELEGRAM_BOT_TOKEN:
        logging.error("Telegram bot token or chat ID missing")
        return
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("backtest", backtest_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("logic", logic))
    application.add_handler(CommandHandler("help", help_command))
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    logging.info("Telegram bot started")
async def health_check():
    while True:
        await send_notification("💖 KINGISBACK Sniper Bot is running and scanning for MOONSHOTS! 😘")
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
async def handle_callback(request):
    try:
        data = await request.json()
        logging.info(f"SolanaFM callback received: {data}")
        return web.Response(text="OK")
    except Exception as e:
        logging.error(f"SolanaFM callback error: {str(e)}")
        return web.Response(text="Error", status=500)
async def handle_health(request):
    return web.Response(text="KINGISBACK Sniper Bot is running")
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
    asyncio.create_task(start_telegram_bot())
    asyncio.create_task(health_check())
    asyncio.create_task(start_server())
    await send_notification("💃 KINGISBACK Sniper Bot v2.8 is LIVE! Scanning Solana for 1000x MOONSHOTS! 🌟😘")
    while True:
        if trade_count >= MAX_TRADES_PER_DAY and datetime.now().date() == last_trade_day:
            await asyncio.sleep(3600)
            trade_count = 0
            last_trade_day = datetime.now().date()
            processed_tokens.clear()
            logging.info("Reset trade count and processed tokens for new day")
            continue
        tokens = []
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
                    pair_response = session.get(f"{DEXSCREENER_PAIRS_API}/{token_address}")
                    if pair_response.status_code == 200:
                        try:
                            pair_data = pair_response.json()
                            if pair_data is None or not isinstance(pair_data, dict) or "pair" not in pair_data or not pair_data["pair"]:
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
                await asyncio.sleep(3 ** attempt)
        processed_tokens.clear() # Reset per scan to avoid skipping new tokens
        await asyncio.sleep(DATA_POLL_INTERVAL)
if __name__ == "__main__":
    asyncio.run(main())