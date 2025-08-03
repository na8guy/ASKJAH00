import asyncio
import logging
import os
import time
import base64
import base58  # Make sure to install: pip install base58
import httpx
import threading
from datetime import datetime, timedelta
from typing import Dict

# Telegram imports
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from datetime import datetime, timedelta
from solders.pubkey import Pubkey
# Solana imports
from solana.rpc.async_api import AsyncClient
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

# MongoDB imports
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

# Cryptography imports
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Web3 imports
from web3 import Web3
from eth_account import Account

# Flask imports
from flask import Flask, request, jsonify

# Other utilities
from dotenv import load_dotenv
from mnemonic import Mnemonic

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.INFO)

# Load environment variables
load_dotenv()

# ======================
# CONFIGURATION
# ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
MONGO_URI = os.getenv("MONGO_URI")
MASTER_KEY = os.getenv("MASTER_KEY")
SOLANA_RPC = os.getenv("SOLANA_RPC_URL")
ETH_RPC = os.getenv("ETH_RPC_URL")
BSC_RPC = os.getenv("BSC_RPC_URL")
BOT_USDT_ADDRESS = os.getenv("BOT_USDT_ADDRESS")
PORT = int(os.getenv("PORT", 5000))

# Validate environment variables
if not TELEGRAM_TOKEN:
    logger.error("Missing TELEGRAM_TOKEN in environment variables")
    raise ValueError("TELEGRAM_TOKEN is required")
if not WEBHOOK_URL:
    logger.error("Missing WEBHOOK_URL in environment variables")
    raise ValueError("WEBHOOK_URL is required")
if not MONGO_URI:
    logger.error("Missing MONGO_URI in environment variables")
    raise ValueError("MONGO_URI is required")
if not MASTER_KEY:
    logger.error("Missing MASTER_KEY in environment variables")
    raise ValueError("MASTER_KEY is required")
if not SOLANA_RPC:
    logger.error("Missing SOLANA_RPC_URL in environment variables")
    raise ValueError("SOLANA_RPC_URL is required")
if not ETH_RPC:
    logger.error("Missing ETH_RPC_URL in environment variables")
    raise ValueError("ETH_RPC_URL is required")
if not BSC_RPC:
    logger.error("Missing BSC_RPC_URL in environment variables")
    raise ValueError("BSC_RPC_URL is required")
if not BOT_USDT_ADDRESS:
    logger.error("Missing BOT_USDT_ADDRESS in environment variables")
    raise ValueError("BOT_USDT_ADDRESS is required")

# ======================
# DATABASE SETUP
# ======================
try:
    logger.info("Connecting to MongoDB...")
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
    mongo_client.admin.command('ping')
    logger.info("MongoDB connection successful")
    db = mongo_client.get_database('trading_bot')
    users_collection = db.users
    users_collection.create_index('user_id', unique=True)
    logger.info("Created user_id index")
except ConnectionFailure as e:
    logger.error(f"Failed to connect to MongoDB: {str(e)}")
    raise
except Exception as e:
    logger.error(f"Database error: {str(e)}")
    raise

# ======================
# ENCRYPTION SETUP
# ======================
try:
    master_key_bytes = bytes.fromhex(MASTER_KEY)
    if len(master_key_bytes) != 32:
        raise ValueError("MASTER_KEY must be a 32-byte hex string")
except ValueError as e:
    logger.error(f"Invalid MASTER_KEY: {str(e)}")
    raise

# ======================
# BLOCKCHAIN CLIENTS
# ======================
solana_client = AsyncClient(SOLANA_RPC)
solana_sync_client = Client(SOLANA_RPC)
w3_eth = Web3(Web3.HTTPProvider(ETH_RPC))
w3_bsc = Web3(Web3.HTTPProvider(BSC_RPC))

# USDT contract setup
USDT_CONTRACT_ADDRESS = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
USDT_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"}
        ],
        "name": "Transfer",
        "type": "event"
    }
]
usdt_contract = w3_eth.eth.contract(address=USDT_CONTRACT_ADDRESS, abi=USDT_ABI)

# ======================
# CONSTANTS AND STATE
# ======================
GMGN_API_HOST = 'https://gmgn.ai'
DEXSCREENER_PROFILE_API = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN_API = "https://api.dexscreener.com/tokens/v1/solana/{token_address}"

# Bot states for conversation
(SET_TRADING_MODE, SET_AUTO_BUY_AMOUNT, SET_SELL_PERCENTAGE, SET_LOSS_PERCENTAGE, 
 SELECT_TOKEN, BUY_AMOUNT, CONFIRM_TRADE, TRANSFER_TOKEN, TRANSFER_AMOUNT, TRANSFER_ADDRESS,
 CONFIRM_NEW_WALLET, SET_WALLET_METHOD, INPUT_MNEMONIC, INPUT_PRIVATE_KEY, CONFIRM_SET_WALLET,
 SELECT_TOKEN_ACTION, SELL_AMOUNT) = range(17)

# Global for Telegram application
telegram_application = None

# Flask app setup
app = Flask(__name__)

# ======================
# HELPER FUNCTIONS
# ======================
def derive_user_key(user_id: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=str(user_id).encode(),
        iterations=100000,
    )
    return kdf.derive(master_key_bytes)

def encrypt_data(data: str, key: bytes) -> dict:
    iv = os.urandom(16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    padded_data = data.encode() + b'\0' * (16 - len(data.encode()) % 16)
    ciphertext = encryptor.update(padded_data) + encryptor.finalize()
    return {
        'iv': base64.b64encode(iv).decode(),
        'ciphertext': base64.b64encode(ciphertext).decode()
    }

def decrypt_data(encrypted_data: dict, key: bytes) -> str:
    iv = base64.b64decode(encrypted_data['iv'])
    ciphertext = base64.b64decode(encrypted_data['ciphertext'])
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded_data = decryptor.update(ciphertext) + decryptor.finalize()
    return padded_data.rstrip(b'\0').decode()

async def check_subscription(user_id: int) -> bool:
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        logger.debug(f"User {user_id} not found")
        return False
    if user.get('subscription_status') != 'active':
        logger.debug(f"User {user_id} has no active subscription")
        return False
    
    expiry = user.get('subscription_expiry')
    if not expiry:
        logger.debug(f"User {user_id} has no subscription expiry")
        return False
    
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry)
    
    if datetime.now() >= expiry:
        logger.debug(f"User {user_id} subscription expired")
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {'subscription_status': 'inactive', 'subscription_expiry': None}}
        )
        return False
    
    logger.debug(f"User {user_id} has active subscription")
    return True

async def set_user_wallet(user_id: int, mnemonic: str = None, private_key: str = None) -> dict:
    user_key = derive_user_key(user_id)
    
    if mnemonic:
        mnemo = Mnemonic("english")
        if not mnemo.check(mnemonic):
            raise ValueError("Invalid mnemonic phrase.")
        eth_account = Account.from_mnemonic(mnemonic)
        eth_address = eth_account.address
        eth_private_key = eth_account.key.hex()
        seed = mnemo.to_seed(mnemonic)
        solana_keypair = Keypair.from_seed(seed[:32])
        solana_private_key = base58.b58encode(solana_keypair.to_bytes()).decode()
    elif private_key:
        try:
            if private_key.startswith('0x'):
                # Ethereum private key
                eth_account = Account.from_key(private_key)
                eth_address = eth_account.address
                eth_private_key = eth_account.key.hex()
                # Derive Solana key from Ethereum private key
                solana_keypair = Keypair.from_seed(bytes.fromhex(private_key[2:])[:32])
                solana_private_key = base58.b58encode(solana_keypair.to_bytes()).decode()
            else:
                # Solana private key
                solana_keypair = Keypair.from_bytes(base58.b58decode(private_key))
                solana_private_key = private_key
                eth_address = None
                eth_private_key = None
        except Exception as e:
            raise ValueError(f"Invalid private key: {str(e)}")
    else:
        raise ValueError("Either mnemonic or private key must be provided.")

    encrypted_mnemonic = encrypt_data(mnemonic if mnemonic else 'Imported via private key', user_key)
    encrypted_solana_private_key = encrypt_data(solana_private_key, user_key)
    
    wallet_data = {
        'user_id': user_id,
        'mnemonic': encrypted_mnemonic,
        'solana': {
            'public_key': str(solana_keypair.pubkey()),
            'private_key': encrypted_solana_private_key
        },
        'trading_mode': 'manual',
        'auto_buy_amount': 0.0,
        'sell_percentage': 0.0,
        'loss_percentage': 0.0,
        'portfolio': {},
        'last_api_call': 0,
        'posted_tokens': [],
        'subscription_status': 'inactive',
        'subscription_expiry': None,
        'payment_address': None,
        'expected_amount': None,
        'payment_deadline': None
    }
    
    if eth_address:
        encrypted_eth_private_key = encrypt_data(eth_private_key, user_key)
        wallet_data['eth'] = {
            'address': eth_address,
            'private_key': encrypted_eth_private_key
        }
        wallet_data['bsc'] = {
            'address': eth_address,
            'private_key': encrypted_eth_private_key
        }
    
    return wallet_data

async def decrypt_user_wallet(user_id: int, user: dict) -> dict:
    user_key = derive_user_key(user_id)
    decrypted_user = user.copy()

    def safe_decrypt(field):
        if isinstance(field, dict) and 'iv' in field and 'ciphertext' in field:
            try:
                return decrypt_data(field, user_key)
            except Exception as e:
                logger.error(f"Decryption failed: {str(e)}")
                return "[Decryption Failed]"
        return field if isinstance(field, str) else "[Invalid Data]"

    decrypted_user['mnemonic'] = safe_decrypt(user.get('mnemonic', {}))
    decrypted_user['solana']['private_key'] = safe_decrypt(user.get('solana', {}).get('private_key', {}))
    
    if 'eth' in user:
        decrypted_user['eth']['private_key'] = safe_decrypt(user.get('eth', {}).get('private_key', {}))
    if 'bsc' in user:
        decrypted_user['bsc']['private_key'] = safe_decrypt(user.get('bsc', {}).get('private_key', {}))
    
    return decrypted_user

async def fetch_latest_token() -> dict:
    """Fetches the latest Solana token from DexScreener API"""
    try:
        async with httpx.AsyncClient() as client:
            # Get latest token profiles
            response = await client.get(
                DEXSCREENER_PROFILE_API,
                params={"sort": "createdAt", "order": "desc", "limit": 1, "chainId": "solana"}
            )
            response.raise_for_status()
            data = response.json()
            
            # Validate response
            if not data or 'data' not in data or not data['data']:
                logger.warning("No tokens found in profile response")
                return None
                
            token_profile = data['data'][0]
            token_address = token_profile.get('tokenAddress')
            if not token_address:
                logger.warning("No token address in profile")
                return None
            
            # Get detailed token information
            token_url = DEXSCREENER_TOKEN_API.format(token_address=token_address)
            token_response = await client.get(token_url)
            token_response.raise_for_status()
            token_data = token_response.json()
            
            if not token_data or 'data' not in token_data or not token_data['data']:
                logger.warning("No token data found")
                return None
                
            token_info = token_data['data'][0]
            
            # Extract social links
            social_links = {}
            for link in token_profile.get('links', []):
                if 'type' in link and 'url' in link:
                    social_links[link['type']] = link['url']
            
            return {
                'contract_address': token_address,
                'name': token_info.get('name', token_profile.get('description', 'Unknown')).split()[0],
                'symbol': token_info.get('symbol', 'UNKNOWN'),
                'price_usd': float(token_info.get('price', 0.0)),
                'market_cap': float(token_info.get('marketCap', 0.0)),
                'liquidity': float(token_info.get('liquidity', {}).get('usd', 0.0)),
                'volume': float(token_info.get('volume', {}).get('h24', 0.0)),
                'image': token_profile.get('icon', ''),
                'website': next((link['url'] for link in token_profile.get('links', []) 
                               if link.get('label') == 'Website'), ''),
                'socials': social_links,
                'dexscreener_url': f"https://dexscreener.com/solana/{token_address}"
            }
    except Exception as e:
        logger.error(f"Error fetching token: {str(e)}")
        return None

async def get_subscription_status_message(user: dict) -> str:
    if user.get('subscription_status') != 'active':
        return "❌ No active subscription. Use /subscribe to start."
    
    expiry = user.get('subscription_expiry')
    if not expiry:
        return "❌ Subscription status unknown."
    
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry)
    
    if datetime.now() >= expiry:
        return "❌ Subscription expired. Use /subscribe to renew."
    
    return f"✅ Subscription active until {expiry.strftime('%Y-%m-%d %H:%M:%S')}."

async def check_balance(user_id: int, chain: str) -> float:
    """Check the native token balance for a specific blockchain chain"""
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        return 0.0
    
    try:
        if chain == 'solana':
            public_key = user['solana']['public_key']
            balance = await solana_client.get_balance(Pubkey.from_string(public_key))
            return balance.value / 10**9  # Convert lamports to SOL
        
        elif chain == 'eth':
            address = user['eth']['address']
            balance_wei = w3_eth.eth.get_balance(Web3.to_checksum_address(address))
            return w3_eth.from_wei(balance_wei, 'ether')
        
        elif chain == 'bsc':
            address = user['bsc']['address']
            balance_wei = w3_bsc.eth.get_balance(Web3.to_checksum_address(address))
            return w3_bsc.from_wei(balance_wei, 'ether')
        
        else:
            return 0.0
    except Exception as e:
        logger.error(f"Error getting {chain} balance for user {user_id}: {str(e)}")
        return 0.0

# ======================
# TELEGRAM COMMAND HANDLERS
# ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Start command from user {user_id}")
    
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        # Create new wallet
        mnemo = Mnemonic("english")
        mnemonic = mnemo.generate(strength=256)
        try:
            user_data = await set_user_wallet(user_id, mnemonic=mnemonic)
            users_collection.insert_one(user_data)
            decrypted_user = await decrypt_user_wallet(user_id, user_data)
            
            # Send welcome message
            await update.message.reply_text(
                "🎉 Welcome to the Crypto Trading Bot!\n\n"
                "Your new wallet has been created.\n"
                "Use /generate_wallet to create a new wallet or /set_wallet to import an existing one.\n\n"
                "⚠️ Your recovery phrase will be sent in a separate message."
            )
            
            # Send sensitive info separately
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🔒 Your Recovery Phrase (save securely):\n\n{decrypted_user['mnemonic']}\n\n"
                     f"Solana Public Key: {user_data['solana']['public_key']}\n"
                     f"ETH/BSC Address: {user_data.get('eth', {}).get('address', 'N/A')}"
            )
            
            logger.info(f"Created new wallet for user {user_id}")
        except Exception as e:
            logger.error(f"Error creating wallet: {str(e)}")
            await update.message.reply_text(f"❌ Error creating wallet: {str(e)}")
    else:
        # Existing user
        try:
            decrypted_user = await decrypt_user_wallet(user_id, user)
            subscription_msg = await get_subscription_status_message(user)
            
            await update.message.reply_text(
                f"👋 Welcome back!\n\n"
                f"Solana Address: {user['solana']['public_key']}\n"
                f"ETH/BSC Address: {user.get('eth', {}).get('address', 'N/A')}\n"
                f"{subscription_msg}\n\n"
                f"Use /balance to check your funds or /trade to start trading."
            )
            logger.info(f"Returning user {user_id}")
        except Exception as e:
            logger.error(f"Error accessing wallet: {str(e)}")
            await update.message.reply_text(f"❌ Error accessing wallet: {str(e)}")

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Subscribe command from user {user_id}")
    
    if await check_subscription(user_id):
        user = users_collection.find_one({'user_id': user_id})
        expiry = user.get('subscription_expiry')
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        await update.message.reply_text(
            f"✅ You already have an active subscription until {expiry.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return
    
    # Create payment address
    user_key = derive_user_key(user_id)
    account = Account.create(user_key)
    payment_address = account.address
    payment_deadline = datetime.now() + timedelta(minutes=30)
    
    # Update user record
    users_collection.update_one(
        {'user_id': user_id},
        {'$set': {
            'payment_address': payment_address,
            'expected_amount': int(5 * 10**6),  # 5 USDT (6 decimals)
            'payment_deadline': payment_deadline.isoformat()
        }},
        upsert=True
    )
    
    await update.message.reply_text(
        "💳 To activate your subscription (5 USDT/week):\n\n"
        f"1. Send exactly 5.0 USDT to:\n`{payment_address}`\n"
        f"2. Network: Ethereum (ERC-20)\n"
        f"3. Complete before: {payment_deadline.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "Your subscription will activate automatically once payment is confirmed.",
        parse_mode='Markdown'
    )
    logger.info(f"Subscription initiated for user {user_id}")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Balance command from user {user_id}")
    
    if not await check_subscription(user_id):
        await update.message.reply_text("❌ You need an active subscription. Use /subscribe.")
        return
    
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("❌ No wallet found. Use /start first.")
        return
    
    try:
        sol_balance = await check_balance(user_id, 'solana')
        eth_balance = await check_balance(user_id, 'eth') if 'eth' in user else 0.0
        bsc_balance = await check_balance(user_id, 'bsc') if 'bsc' in user else 0.0
        
        message = (
            "💰 Your Balances:\n\n"
            f"• Solana (SOL): {sol_balance:.6f}\n"
            f"• Ethereum (ETH): {eth_balance:.6f}\n"
            f"• Binance Smart Chain (BNB): {bsc_balance:.6f}\n\n"
        )
        
        if 'portfolio' in user and user['portfolio']:
            message += "📊 Token Holdings:\n"
            for contract, token in user['portfolio'].items():
                message += f"• {token['name']} ({token['symbol']}): {token['amount']:.6f} SOL worth\n"
        else:
            message += "You don't hold any tokens yet."
        
        await update.message.reply_text(message)
    except Exception as e:
        logger.error(f"Balance error: {str(e)}")
        await update.message.reply_text(f"❌ Error fetching balances: {str(e)}")

# ======================
# PERIODIC TASKS
# ======================
async def periodic_token_check(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running periodic token check")
    
    try:
        token = await fetch_latest_token()
        if not token:
            logger.info("No new token found")
            return
        
        users = users_collection.find({'subscription_status': 'active'})
        for user in users:
            user_id = user['user_id']

            
            
            # Skip if token already posted
            if token['contract_address'] in user.get('posted_tokens', []):
                continue
            
            # Rate limiting
            if time.time() - user.get('last_api_call', 0) < 1:
                continue
            
            # Prepare token info
            is_risky = token['liquidity'] < 1000 or token['volume'] < 1000
            warning = "⚠️ *Low liquidity/volume - Trade with caution!*" if is_risky else ""
            
            # Format social links
            social_links = "\n".join(
            [f"{k.capitalize()}: {v}" for k, v in token.get('socials', {}).items()]
        ) or "N/A"
        
        # Create message
            message = (
            f"🚀 *New Token Alert* 🚀\n\n"
            f"*Name:* {token['name']} ({token['symbol']})\n"
            f"*Contract:* `{token['contract_address']}`\n"
            f"*Price:* ${token['price_usd']:.6f}\n"
            f"*Market Cap:* ${token['market_cap']:,.2f}\n"
            f"*Liquidity:* ${token['liquidity']:,.2f}\n"
            f"*24h Volume:* ${token['volume']:,.2f}\n"
            f"*Website:* {token['website'] or 'N/A'}\n"
            f"*Socials:*\n{social_links}\n\n"
            f"[View on DexScreener]({token['dexscreener_url']})"
        )
            
            # Create buttons
            keyboard = [
                [InlineKeyboardButton("Buy", callback_data=f"buy_{token['contract_address']}"),
                 InlineKeyboardButton("Sell", callback_data=f"sell_{token['contract_address']}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Send message
            await context.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            
            # Update user record
            users_collection.update_one(
                {'user_id': user_id},
                {
                    '$set': {'last_api_call': time.time()},
                    '$addToSet': {'posted_tokens': token['contract_address']}
                }
            )
            
            logger.info(f"Sent token alert to user {user_id}")
    except Exception as e:
        logger.error(f"Error in periodic token check: {str(e)}")

async def subscription_check(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running subscription check")
    
    try:
        users = users_collection.find({'subscription_status': 'active'})
        now = datetime.now()
        
        for user in users:
            user_id = user['user_id']
            expiry = user.get('subscription_expiry')
            
            if not expiry:
                continue
                
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry)
                
            if now >= expiry:
                # Update status
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$set': {'subscription_status': 'inactive', 'subscription_expiry': None}}
                )
                
                # Notify user
                await context.bot.send_message(
                    chat_id=user_id,
                    text="❌ Your subscription has expired. Use /subscribe to renew your access."
                )
                logger.info(f"Subscription expired for user {user_id}")
    except Exception as e:
        logger.error(f"Error in subscription check: {str(e)}")

# ======================
# FLASK ROUTES
# ======================
@app.route('/')
def index():
    return "Crypto Trading Bot is running!", 200

@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    global telegram_application
    
    if not telegram_application:
        logger.error("Telegram application not initialized")
        return "Service Unavailable", 503
        
    try:
        update = Update.de_json(request.get_json(), telegram_application.bot)
        telegram_application.process_update(update)
        return "OK", 200
    except Exception as e:
        logger.error(f"Error processing update: {str(e)}")
        return "Internal Server Error", 500

@app.route('/payment_webhook', methods=['POST'])
def payment_webhook():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
            
        # Extract transaction details
        tx_hash = data.get('hash')
        sender = data.get('from')
        value = int(data.get('value', 0))
        contract_address = data.get('contract_address')
        
        # Validate USDT transaction
        if contract_address.lower() != USDT_CONTRACT_ADDRESS.lower():
            return jsonify({"error": "Not a USDT transaction"}), 400
            
        # Find user by payment address
        user = users_collection.find_one({'payment_address': sender})
        if not user:
            return jsonify({"error": "User not found"}), 404
            
        # Validate expected amount
        expected_amount = user.get('expected_amount', 0)
        if value < expected_amount:
            return jsonify({"error": "Insufficient payment"}), 400
            
        # Activate subscription
        expiry = datetime.now() + timedelta(days=7)
        users_collection.update_one(
            {'user_id': user['user_id']},
            {'$set': {
                'subscription_status': 'active',
                'subscription_expiry': expiry.isoformat()
            }}
        )
        
        logger.info(f"Subscription activated for user {user['user_id']}")
        return jsonify({"status": "success"}), 200
        
    except Exception as e:
        logger.error(f"Payment webhook error: {str(e)}")
        return jsonify({"error": str(e)}), 500


def main():
    global telegram_application
    
    logger.info("Starting application setup")
    
    # Create Telegram application
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Register handlers
    telegram_app.add_handler(CommandHandler('start', start))
    telegram_app.add_handler(CommandHandler('subscribe', subscribe))
    telegram_app.add_handler(CommandHandler('balance', balance))
    # Add more handlers as needed...
    
    # Set bot commands
    commands = [
        BotCommand('start', 'Start the bot'),
        BotCommand('subscribe', 'Subscribe to premium features'),
        BotCommand('balance', 'Check your balances'),
    ]
    telegram_app.bot.set_my_commands(commands)
    
    # Set up periodic tasks
    telegram_app.job_queue.run_repeating(periodic_token_check, interval=60, first=10)
    telegram_app.job_queue.run_repeating(subscription_check, interval=3600, first=15)
    
    # Set webhook
    async def set_webhook_task():
        await telegram_app.bot.set_webhook(
            url=f"{WEBHOOK_URL}/telegram_webhook",
            drop_pending_updates=True
        )
        logger.info(f"Webhook set to: {WEBHOOK_URL}/telegram_webhook")
    
    loop = asyncio.get_event_loop()
    loop.run_until_complete(set_webhook_task())
    
    # Assign to global
    telegram_application = telegram_app
    
    # Start Flask in the main thread (remove threading)
    logger.info(f"Starting Flask server on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

if __name__ == '__main__':
    main()

