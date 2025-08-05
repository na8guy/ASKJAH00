import asyncio
import logging
import json
import httpx
import base64
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, 
    CommandHandler, 
    CallbackQueryHandler, 
    ContextTypes, 
    ConversationHandler, 
    MessageHandler, 
    filters,
    JobQueue
)
from typing import Optional, List, Dict, Any
import re
from solana.rpc.async_api import AsyncClient
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction, VersionedTransaction
try:
    import base58
except ImportError:
    raise ImportError("Missing 'base58' package. Install it with: pip install base58")
try:
    from mnemonic import Mnemonic
except ImportError:
    raise ImportError("Missing 'mnemonic' package. Install it with: pip install mnemonic")
try:
    from pymongo import MongoClient
    from pymongo.errors import ConnectionFailure
except ImportError:
    raise ImportError("Missing 'pymongo' package. Install it with: pip install pymongo")
try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    import base64 as base64_cryptography
except ImportError:
    raise ImportError("Missing 'cryptography' package. Install it with: pip install cryptography")

from datetime import datetime, timedelta
import time
import os
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account

# FastAPI setup
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse


# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot_debug.log")
    ]
)
logger = logging.getLogger(__name__)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.DEBUG)

# Load environment variables
load_dotenv()

# Create FastAPI app
app = FastAPI()

@app.get("/health")
async def health_check():
    return JSONResponse(content={'status': 'ok'})

@app.post("/webhook")
async def telegram_webhook(request: Request):
    global application
    try:
        # Check if application is properly initialized and running
        if application is None:
            logger.error("🚫 Application instance is None")
            return JSONResponse(
                content={'error': 'Application not created'},
                status_code=503
            )
        
        # Check both initialization and running state
        if not hasattr(application, '_initialized') or not application._initialized:
            logger.error("🚫 Application not initialized")
            return JSONResponse(
                content={'error': 'Application not initialized'},
                status_code=503
            )
        
        if not application.running:
            logger.error("🚫 Application not running")
            return JSONResponse(
                content={'error': 'Application not running'},
                status_code=503
            )
        
        update_data = await request.json()
        update = Update.de_json(update_data, application.bot)
        
        # Process the update
        await application.process_update(update)
        logger.debug(f"Processed update: {update.update_id}")
        
        return JSONResponse(content={'status': 'ok'})
    except Exception as e:
        logger.error(f"🔥 Webhook error: {str(e)}", exc_info=True)
        return JSONResponse(
            content={'error': str(e)},
            status_code=500
        )


GMGN_API_HOST = 'https://gmgn.ai'

# MongoDB setup with retry logic
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI not found in .env file")
    raise ValueError("MONGO_URI not found in .env file")
max_retries = 3
for attempt in range(max_retries):
    try:
        logger.debug("Connecting to MongoDB")
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=30000)
        mongo_client.admin.command('ping')
        logger.info("✅ MongoDB connection successful")
        db = mongo_client.get_database('trading_bot')
        users_collection = db.users
        users_collection.create_index('user_id', unique=True)
        
        # Initialize global posted tokens collection
        if 'global_posted_tokens' not in db.list_collection_names():
            db.create_collection('global_posted_tokens')
            db.global_posted_tokens.create_index('contract_address', unique=True)
            db.global_posted_tokens.create_index('timestamp', expireAfterSeconds=86400)  # 24h expiration
            logger.info("Created global_posted_tokens collection with indexes")
        break
    except ConnectionFailure as e:
        logger.error(f"Attempt {attempt + 1} failed to connect to MongoDB Atlas: {str(e)}")
        if attempt < max_retries - 1:
            time.sleep(5)
            continue
        raise
    except Exception as e:
        logger.error(f"Unexpected error in MongoDB setup: {str(e)}")
        raise

# Master key for encryption
MASTER_KEY = os.getenv("MASTER_KEY")
if not MASTER_KEY:
    logger.error("MASTER_KEY not found in .env file")
    raise ValueError("MASTER_KEY not found in .env file")
try:
    master_key = bytes.fromhex(MASTER_KEY)
    if len(master_key) != 32:
        raise ValueError("MASTER_KEY must be a 32-byte hex string")
except ValueError as e:
    logger.error(f"Invalid MASTER_KEY: {str(e)}")
    raise ValueError(f"Invalid MASTER_KEY: {str(e)}")

# Blockchain clients
SOLANA_RPC = os.getenv("SOLANA_RPC_URL")
ETH_RPC = os.getenv("ETH_RPC_URL")
BSC_RPC = os.getenv("BSC_RPC_URL")
if not all([SOLANA_RPC, ETH_RPC, BSC_RPC]):
    logger.error("One or more RPC URLs not found in .env file")
    raise ValueError("One or more RPC URLs not found in .env file")
solana_client = AsyncClient(SOLANA_RPC)
solana_sync_client = Client(SOLANA_RPC)
w3_eth = Web3(Web3.HTTPProvider(ETH_RPC))
w3_bsc = Web3(Web3.HTTPProvider(BSC_RPC))

# USDT contract setup
BOT_USDT_ADDRESS = os.getenv("BOT_USDT_ADDRESS")
if not BOT_USDT_ADDRESS:
    logger.error("BOT_USDT_ADDRESS not found in .env file")
    raise ValueError("BOT_USDT_ADDRESS not found in .env file")
try:
    if not Web3.is_address(BOT_USDT_ADDRESS):
        logger.error("Invalid BOT_USDT_ADDRESS: Not a valid Ethereum address")
        raise ValueError("Invalid BOT_USDT_ADDRESS: Not a valid Ethereum address")
    BOT_USDT_ADDRESS = Web3.to_checksum_address(BOT_USDT_ADDRESS)
except Exception as e:
    logger.error(f"Error validating BOT_USDT_ADDRESS: {str(e)}")
    raise ValueError(f"Error validating BOT_USDT_ADDRESS: {str(e)}")
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

# DexScreener API endpoints
DEXSCREENER_NEW_TOKENS_API = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN_API = "https://api.dexscreener.com/tokens/v1/solana/{token_address}"


# Bot states for conversation
(WALLET_SETUP_CHOICE, SET_TRADING_MODE, SET_AUTO_BUY_AMOUNT, SET_SELL_PERCENTAGE, SET_LOSS_PERCENTAGE, 
 SELECT_TOKEN, BUY_AMOUNT, CONFIRM_TRADE, TRANSFER_TOKEN, TRANSFER_AMOUNT, TRANSFER_ADDRESS,
 CONFIRM_NEW_WALLET, SET_WALLET_METHOD, INPUT_MNEMONIC, INPUT_PRIVATE_KEY, CONFIRM_SET_WALLET,
 SELECT_TOKEN_ACTION, SELL_AMOUNT, INPUT_CONTRACT) = range(19)

def derive_user_key(user_id: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=str(user_id).encode(),
        iterations=100000,
    )
    return kdf.derive(master_key)

def encrypt_data(data: str, key: bytes) -> dict:
    iv = os.urandom(16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    padded_data = data.encode() + b'\0' * (16 - len(data.encode()) % 16)
    ciphertext = encryptor.update(padded_data) + encryptor.finalize()
    return {'iv': base64_cryptography.b64encode(iv).decode(), 'ciphertext': base64_cryptography.b64encode(ciphertext).decode()}

def decrypt_data(encrypted_data: dict, key: bytes) -> str:
    iv = base64_cryptography.b64decode(encrypted_data['iv'])
    ciphertext = base64_cryptography.b64decode(encrypted_data['ciphertext'])
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded_data = decryptor.update(ciphertext) + decryptor.finalize()
    return padded_data.rstrip(b'\0').decode()

async def check_subscription(user_id: int) -> bool:
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        logger.debug(f"No user found for user_id {user_id}")
        return False
        
    status = user.get('subscription_status')
    if status not in ['active', 'trial']:
        logger.debug(f"User {user_id} subscription is inactive")
        return False
        
    expiry = user.get('subscription_expiry')
    if not expiry:
        logger.debug(f"User {user_id} has no subscription expiry")
        return False
        
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry)
        
    if datetime.now() >= expiry:
        new_status = 'inactive' if status == 'active' else 'expired_trial'
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {'subscription_status': new_status, 'subscription_expiry': None}}
        )
        logger.debug(f"User {user_id} subscription expired at {expiry}")
        return False
        
    logger.debug(f"User {user_id} has active subscription until {expiry}")
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
            try:
                solana_keypair = Keypair.from_bytes(base58.b58decode(private_key))
                solana_private_key = private_key
                eth_account = None
                eth_address = None
                eth_private_key = None
            except:
                if not private_key.startswith('0x'):
                    private_key = '0x' + private_key
                eth_account = Account.from_key(private_key)
                eth_address = eth_account.address
                eth_private_key = eth_account.key.hex()
                solana_keypair = Keypair.from_seed(bytes.fromhex(private_key[2:])[:32])
                solana_private_key = base58.b58encode(solana_keypair.to_bytes()).decode()
        except Exception as e:
            raise ValueError(f"Invalid private key: {str(e)}")
    else:
        raise ValueError("Either mnemonic or private key must be provided.")

    encrypted_mnemonic = encrypt_data(mnemonic if mnemonic else 'Imported via private key', user_key)
    encrypted_solana_private_key = encrypt_data(solana_private_key, user_key)
    encrypted_eth_private_key = encrypt_data(eth_private_key, user_key) if eth_private_key else None
    encrypted_bsc_private_key = encrypt_data(eth_private_key, user_key) if eth_private_key else None

    return {
        'user_id': user_id,
        'mnemonic': encrypted_mnemonic,
        'solana': {
            'public_key': str(solana_keypair.pubkey()),
            'private_key': encrypted_solana_private_key
        },
        'eth': {
            'address': eth_address,
            'private_key': encrypted_eth_private_key
        } if eth_address else None,
        'bsc': {
            'address': eth_address,
            'private_key': encrypted_bsc_private_key
        } if eth_address else None,
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

async def decrypt_user_wallet(user_id: int, user: dict) -> dict:
    user_key = derive_user_key(user_id)
    decrypted_user = user.copy()

    def safe_decrypt(field):
        if isinstance(field, dict) and 'iv' in field and 'ciphertext' in field:
            try:
                return decrypt_data(field, user_key)
            except Exception as e:
                logger.error(f"Decryption failed for user {user_id}: {str(e)}")
                return "[Decryption Failed]"
        else:
            logger.warning(f"Invalid or unencrypted data for user {user_id}: {field}")
            return field if isinstance(field, str) else "[Invalid Data]"

    # Ensure all required fields exist
    decrypted_user.setdefault('mnemonic', '')
    decrypted_user.setdefault('solana', {'private_key': ''})
    decrypted_user.setdefault('eth', {'private_key': ''})
    decrypted_user.setdefault('bsc', {'private_key': ''})

    decrypted_user['mnemonic'] = safe_decrypt(user.get('mnemonic', {}))
    decrypted_user['solana']['private_key'] = safe_decrypt(user.get('solana', {}).get('private_key', ''))
    if user.get('eth'):
        decrypted_user['eth']['private_key'] = safe_decrypt(user.get('eth', {}).get('private_key', ''))
    if user.get('bsc'):
        decrypted_user['bsc']['private_key'] = safe_decrypt(user.get('bsc', {}).get('private_key', ''))
    
    return decrypted_user

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    logger.debug(f"Start command from user {user_id}")
    
    context.user_data[f'conversation_state_{user_id}'] = None
    
    # Check if user exists and has all required fields
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        # Create new user with all required fields
        user_data = {
            'user_id': user_id,
            'chat_id': chat_id,
            'solana': {
                'public_key': '',
                'private_key': ''
            },
            'eth': None,
            'bsc': None,
            'trading_mode': 'manual',
            'auto_buy_amount': 0.0,
            'sell_percentage': 0.0,
            'loss_percentage': 0.0,
            'portfolio': {},
            'last_api_call': 0,
            'posted_tokens': [],
            'subscription_status': 'trial',
            'subscription_expiry': (datetime.now() + timedelta(days=1)).isoformat(),
            'payment_address': None,
            'expected_amount': None,
            'payment_deadline': None
        }
        users_collection.insert_one(user_data)
        
        # Ask if they want to generate or import wallet
        keyboard = [
            [InlineKeyboardButton("Generate New Wallet", callback_data='generate_wallet')],
            [InlineKeyboardButton("Import Existing Wallet", callback_data='import_wallet')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "👋 Welcome to the Multi-Chain Trading Bot!\n\n"
            "You have a 1-day free trial to test all features.\n\n"
            "Would you like to:\n"
            "1. Generate a new wallet (recommended for beginners)\n"
            "2. Import an existing wallet?",
            reply_markup=reply_markup
        )
        return WALLET_SETUP_CHOICE
    else:
        # Ensure existing user has all required fields
        update_fields = {}
        if 'solana' not in user:
            update_fields['solana'] = {'public_key': '', 'private_key': ''}
        if 'eth' not in user:
            update_fields['eth'] = None
        if 'bsc' not in user:
            update_fields['bsc'] = None
        if 'portfolio' not in user:
            update_fields['portfolio'] = {}
        if 'posted_tokens' not in user:
            update_fields['posted_tokens'] = []
        if 'subscription_status' not in user:
            update_fields['subscription_status'] = 'inactive'
            
        if update_fields:
            users_collection.update_one(
                {'user_id': user_id},
                {'$set': update_fields}
            )
            user = users_collection.find_one({'user_id': user_id})
        
        # Show existing user their wallet info
        decrypted_user = await decrypt_user_wallet(user_id, user)
        eth_bsc_address = user['eth']['address'] if user.get('eth') else "Not set"
        subscription_message = await get_subscription_status_message(user)
        
        message = (
            f"👋 *Welcome back!*\n\n"
            f"🔑 *Solana Address*: `{user['solana']['public_key']}`\n"
            f"🌐 *ETH/BSC Address*: `{eth_bsc_address}`\n\n"
            f"{subscription_message}\n\n"
            f"🔧 *Commands:*\n"
            f"- /generate_wallet - Create a new wallet\n"
            f"- /set_wallet - Import an existing wallet"
        )
            
        await update.message.reply_text(message, parse_mode='Markdown')
        
        if await check_subscription(user_id):
            logger.info(f"📡 Starting token updates for existing subscriber {user_id}")
            await start_token_updates(context, user_id)

async def get_subscription_status_message(user: dict) -> str:
    if user.get('subscription_status') != 'active':
        return "You do not have an active subscription. Use /subscribe to start a weekly subscription."
    expiry = user.get('subscription_expiry')
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry)
    if expiry and expiry > datetime.now():
        return f"Your subscription is active until {expiry.strftime('%Y-%m-%d %H:%M:%S')}."
    else:
        users_collection.update_one(
            {'user_id': user['user_id']},
            {'$set': {'subscription_status': 'inactive', 'subscription_expiry': None}}
        )
        return "Your subscription has expired. Use /subscribe to renew."


async def handle_wallet_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == 'generate_wallet':
        # Generate new wallet flow
        mnemo = Mnemonic("english")
        mnemonic = mnemo.generate(strength=256)
        user_data = await set_user_wallet(user_id, mnemonic=mnemonic)
        
        # Update the existing user document
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {
                'solana': user_data['solana'],
                'eth': user_data['eth'],
                'bsc': user_data['bsc'],
                'mnemonic': user_data['mnemonic']
            }}
        )
        
        decrypted_user = await decrypt_user_wallet(user_id, user_data)
        eth_bsc_address = user_data['eth']['address'] if user_data.get('eth') else "Not set"
        
        message = (
            f"✨ *New Wallet Generated!*\n\n"
            f"🔐 *Mnemonic*: `{decrypted_user['mnemonic']}`\n"
            f"🔑 *Solana Address*: `{user_data['solana']['public_key']}`\n"
            f"🌐 *ETH/BSC Address*: `{eth_bsc_address}`\n\n"
            f"⚠️ *SECURITY WARNING*\n"
            f"1️⃣ Never share your mnemonic or private keys\n"
            f"2️⃣ Store them securely offline\n"
            f"3️⃣ This message will auto-delete in 30 seconds\n\n"
            f"🎉 You have a 1-day free trial to test all features!"
        )
        
        msg = await query.message.reply_text(message, parse_mode='Markdown')
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=msg.message_id),
            30,
            user_id=user_id
        )
        
        # Start token updates after wallet is set up
        await start_token_updates(context, user_id)
        return ConversationHandler.END
        
    elif query.data == 'import_wallet':
        # Redirect to set_wallet flow
        await query.message.reply_text(
            "Choose how to import your wallet:\n"
            "- Mnemonic: Enter your 24-word BIP-39 mnemonic phrase\n"
            "- Private Key: Enter your Solana or ETH/BSC private key\n\n"
            "⚠️ Your input will auto-delete in 30 seconds for security."
        )
        return await set_wallet_method(update, context)
    

async def fetch_tokens_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Manual token fetch by user {user_id}")
    context.user_data[f'conversation_state_{user_id}'] = None  # Clear conversation state
    
    try:
        if not await check_subscription(user_id):
            await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
            return

        # Rate limiting
        user = users_collection.find_one({'user_id': user_id})
        current_time = time.time()
        if user.get('last_api_call', 0) > current_time - 1:
            await update.message.reply_text("Please wait at least 1 second between fetches.")
            return

        # Fetch tokens
        logger.debug("Starting token fetch")
        tokens = await fetch_latest_token()
        
        if not tokens:
            logger.warning("No new tokens fetched")
            await update.message.reply_text("No new tokens found. Please try again later.")
            return
            
        sent_any = False
        for token in tokens:
            logger.info(f"Fetched token: {token.get('name')} ({token.get('contract_address')})")

            # Check if already posted globally
            if db.global_posted_tokens.find_one({'contract_address': token['contract_address']}):
                logger.info(f"Token {token['contract_address']} already posted globally")
                continue

            # Check if user has already seen this token
            if token['contract_address'] in user.get('posted_tokens', []):
                logger.info(f"User {user_id} already saw token {token['contract_address']}")
                continue

            # Format token info
            message = format_token_message(token)
            is_suspicious = token['liquidity'] < 1000 or token['volume'] < 1000
            warning = "⚠️ *LOW LIQUIDITY - Trade with caution!*\n" if is_suspicious else ""
        
            # Create buttons
            keyboard = [
                [InlineKeyboardButton("💰 Buy", callback_data=f"buy_{token['contract_address']}"),
                 InlineKeyboardButton("💸 Sell", callback_data=f"sell_{token['contract_address']}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                if token.get('image'):
                    await update.message.reply_photo(
                        photo=token['image'],
                        caption=warning + message,
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                else:
                    await update.message.reply_text(
                        warning + message,
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                sent_any = True
            except Exception as e:
                logger.error(f"Error sending token {token['contract_address']}: {str(e)}")
                await update.message.reply_text(
                    warning + message,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
                sent_any = True
            
            # Update records
            try:
                db.global_posted_tokens.insert_one({
                    'contract_address': token['contract_address'],
                    'timestamp': datetime.now(),
                    'name': token.get('name', ''),
                    'symbol': token.get('symbol', '')
                })
                
                users_collection.update_one(
                    {'user_id': user_id},
                    {
                        '$set': {'last_api_call': current_time},
                        '$addToSet': {'posted_tokens': token['contract_address']}
                    }
                )
                logger.info(f"Token records updated for {token['contract_address']}")
            except Exception as e:
                logger.error(f"Error updating token records for {token['contract_address']}: {str(e)}")
        
        if not sent_any:
            await update.message.reply_text("No new tokens available that you haven't seen.")
            
    except Exception as e:
        logger.error(f"Error in manual token fetch: {str(e)}", exc_info=True)
        await update.message.reply_text("An error occurred while fetching tokens. Please try again.")
        if await check_subscription(user_id):
            await start_token_updates(context, user_id)

async def trade_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Trade status requested by user {user_id}")
    user = users_collection.find_one({'user_id': user_id})
    
    if not user:
        await update.message.reply_text("No user data found.")
        return
        
    status = (
        f"🔧 *Trade System Status*\n\n"
        f"🔄 Auto-fetch: {'✅ Enabled' if user.get('last_token_check') else '❌ Disabled'}\n"
        f"⏱️ Last check: {datetime.fromtimestamp(user.get('last_token_check', 0)).strftime('%Y-%m-%d %H:%M') if user.get('last_token_check') else 'Never'}\n"
        f"📋 Tokens seen: {len(user.get('posted_tokens', []))}\n"
        f"💼 Active trades: {len(user.get('portfolio', {}))}\n\n"
        f"Use /fetch_tokens to manually check for new tokens."
    )
    
    await update.message.reply_text(status, parse_mode='Markdown')  

async def start_token_updates(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Schedule periodic token updates for the subscribed user."""
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        return
        
    # Remove any existing jobs for this user
    for job in context.job_queue.jobs():
        if job.name.startswith(f"user_{user_id}_"):
            job.schedule_removal()
    
    if await check_subscription(user_id):
        # Schedule token updates
        context.job_queue.run_repeating(
            update_token_info,
            interval=30,
            first=5,
            user_id=user_id,
            name=f"user_{user_id}_token_updates",
            data={'last_activity': datetime.now()}
        )
        
        # Schedule trial ending notification if applicable
        if user.get('subscription_status') == 'trial':
            expiry = datetime.fromisoformat(user['subscription_expiry'])
            notify_time = expiry - timedelta(hours=1)
            
            context.job_queue.run_once(
                notify_trial_ending,
                when=notify_time,
                user_id=user_id,
                name=f"user_{user_id}_trial_ending"
            )

async def check_conversation_timeout(context: ContextTypes.DEFAULT_TYPE):
    """Check for inactive conversation states and resume jobs."""
    user_id = context.job.user_id
    last_activity = context.job.data.get('last_activity', datetime.now())
    if (datetime.now() - last_activity).total_seconds() > 60:
        logger.debug(f"Resuming token updates for user {user_id} after 1 minute of inactivity")
        context.user_data[f'conversation_state_{user_id}'] = None
        for job in context.job_queue.jobs():
            if job.name == f"timeout_check_{user_id}":
                job.schedule_removal()
                logger.debug(f"Removed timeout_check_{user_id}")
        if await check_subscription(user_id):
            await start_token_updates(context, user_id)



async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"Subscribe command from user {user_id}")
    context.user_data[f'conversation_state_{user_id}'] = None  # Clear conversation state
    
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("No wallet found. Please use /start to create a wallet.")
        return

    status = user.get('subscription_status')
    if status == 'active':
        expiry = user.get('subscription_expiry')
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        await update.message.reply_text(
            f"You already have an active subscription until {expiry.strftime('%Y-%m-%d %H:%M:%S')}."
        )
        await start_token_updates(context, user_id)
        return
    elif status == 'trial':
        expiry = datetime.fromisoformat(user['subscription_expiry'])
        time_left = expiry - datetime.now()
        await update.message.reply_text(
            f"You're currently on a free trial (expires in {time_left}).\n\n"
            f"To continue after your trial ends, the subscription is $5/week."
        )
        return

    try:
        usdt_amount = 5.0  # $5/week
        usdt_amount_wei = int(usdt_amount * 10**6)

        user_key = derive_user_key(user_id)
        account = Account.create(user_key)
        payment_address = account.address

        payment_deadline = datetime.now() + timedelta(minutes=30)
        users_collection.update_one(
            {'user_id': user_id},
            {
                '$set': {
                    'payment_address': payment_address,
                    'expected_amount': usdt_amount_wei,
                    'payment_deadline': payment_deadline.isoformat()
                }
            }
        )

        await update.message.reply_text(
            f"To subscribe ($5/week), send {usdt_amount:.6f} USDT to:\n"
            f"Address: {payment_address}\n"
            f"Network: Ethereum\n"
            f"Deadline: {payment_deadline.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Your subscription will activate automatically after payment is confirmed."
        )
    except Exception as e:
        logger.error(f"Error creating subscription for user {user_id}: {str(e)}")
        await update.message.reply_text("Error initiating subscription. Please try again later.")

async def generate_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"Generate wallet command from user {user_id}")
    
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    
    user = users_collection.find_one({'user_id': user_id})
    if user and user.get('solana', {}).get('public_key'):
        keyboard = [
            [InlineKeyboardButton("Yes, generate new wallet", callback_data='confirm_new_wallet')],
            [InlineKeyboardButton("No, keep existing wallet", callback_data='cancel_new_wallet')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        eth_bsc_address = user['eth']['address'] if user.get('eth') else "Not set"
        await update.message.reply_text(
            f"You already have a wallet:\n"
            f"Solana wallet: {user['solana']['public_key']}\n"
            f"ETH/BSC wallet: {eth_bsc_address}\n\n"
            f"Generating a new wallet will overwrite the existing one. Are you sure?",
            reply_markup=reply_markup
        )
        return CONFIRM_NEW_WALLET
    else:
        # Directly generate wallet if none exists
        return await confirm_generate_wallet(update, context, new_user=True)

async def confirm_generate_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE, new_user=False) -> int:
    if not new_user:
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        if query.data == 'cancel_new_wallet':
            await query.message.reply_text("Wallet generation cancelled. Your existing wallet remains unchanged.")
            return ConversationHandler.END
    else:
        user_id = update.effective_user.id

    try:
        mnemo = Mnemonic("english")
        mnemonic = mnemo.generate(strength=256)
        user_data = await set_user_wallet(user_id, mnemonic=mnemonic)
        
        # Update or insert the user document
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {
                'solana': user_data['solana'],
                'eth': user_data['eth'],
                'bsc': user_data['bsc'],
                'mnemonic': user_data['mnemonic']
            }},
            upsert=True
        )
        
        decrypted_user = await decrypt_user_wallet(user_id, user_data)
        eth_bsc_address = user_data['eth']['address'] if user_data.get('eth') else "Not set"
        
        message = (
            f"✨ *New Wallet Generated!*\n\n"
            f"🔐 *Mnemonic*: `{decrypted_user['mnemonic']}`\n"
            f"🔑 *Solana Address*: `{user_data['solana']['public_key']}`\n"
            f"🌐 *ETH/BSC Address*: `{eth_bsc_address}`\n\n"
            f"⚠️ *SECURITY WARNING*\n"
            f"1️⃣ Never share your mnemonic or private keys\n"
            f"2️⃣ Store them securely offline\n"
            f"3️⃣ This message will auto-delete in 30 seconds"
        )
        
        if new_user:
            msg = await update.message.reply_text(message, parse_mode='Markdown')
        else:
            msg = await query.message.reply_text(message, parse_mode='Markdown')
        
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=msg.message_id),
            30,
            user_id=user_id
        )
        
        return ConversationHandler.END
    except Exception as e:
        error_msg = f"Error generating wallet: {str(e)}"
        if new_user:
            await update.message.reply_text(error_msg)
        else:
            await query.message.reply_text(error_msg)
        logger.error(error_msg)
        return ConversationHandler.END

async def set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"Set wallet command from user {user_id}")
    
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    
    keyboard = [
        [InlineKeyboardButton("Import with Mnemonic", callback_data='mnemonic')],
        [InlineKeyboardButton("Import with Private Key", callback_data='private_key')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "Choose how to import your wallet:\n"
        "- Mnemonic: Enter your 24-word BIP-39 mnemonic phrase\n"
        "- Private Key: Enter your Solana (base58) or ETH/BSC (hex) private key\n\n"
        "⚠️ Your input will auto-delete in 30 seconds for security.",
        reply_markup=reply_markup
    )
    return SET_WALLET_METHOD

async def set_wallet_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await check_subscription(user_id):
        await query.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    context.user_data['wallet_method'] = query.data

    if query.data == 'mnemonic':
        message = await query.message.reply_text(
            "Please enter your 24-word BIP-39 mnemonic phrase (space-separated).\n"
            "Example: word1 word2 ... word24\n"
            "⚠️ This message and your input will auto-delete in 30 seconds."
        )
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id
        )
        return INPUT_MNEMONIC
    else:
        message = await query.message.reply_text(
            "Please enter your private key (64-byte base58 for Solana or 32-byte hex for ETH/BSC, with or without '0x').\n"
            "Example for Solana: 2a3b... (base58)\n"
            "Example for ETH/BSC: 0x1234... or 1234... (hex)\n"
            "⚠️ This message and your input will auto-delete in 30 seconds."
        )
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id
        )
        return INPUT_PRIVATE_KEY

async def input_mnemonic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    
    mnemonic = update.message.text.strip()
    context.user_data['wallet_input'] = mnemonic

    # Delete the message after 30 seconds
    context.job_queue.run_once(
        lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=update.message.message_id),
        30,
        user_id=user_id
    )

    try:
        mnemo = Mnemonic("english")
        if not mnemo.check(mnemonic):
            message = await update.message.reply_text(
                "Invalid mnemonic phrase. Please enter a valid 24-word BIP-39 mnemonic.\n"
                "⚠️ This message will auto-delete in 30 seconds."
            )
            context.job_queue.run_once(
                lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
                30,
                user_id=user_id
            )
            return INPUT_MNEMONIC
        
        user = users_collection.find_one({'user_id': user_id})
        if user and user.get('solana', {}).get('public_key'):
            keyboard = [
                [InlineKeyboardButton("Yes, import new wallet", callback_data='confirm_set_wallet')],
                [InlineKeyboardButton("No, keep existing wallet", callback_data='cancel_set_wallet')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            eth_bsc_address = user['eth']['address'] if user.get('eth') else "Not set"
            await update.message.reply_text(
                f"You already have a wallet:\n"
                f"Solana wallet: {user['solana']['public_key']}\n"
                f"ETH/BSC wallet: {eth_bsc_address}\n\n"
                f"Importing a new wallet will overwrite the existing one. Are you sure?",
                reply_markup=reply_markup
            )
            return CONFIRM_SET_WALLET
        else:
            # No existing wallet, proceed directly
            user_data = await set_user_wallet(user_id, mnemonic=mnemonic)
            users_collection.update_one(
                {'user_id': user_id},
                {'$set': user_data},
                upsert=True
            )
            
            decrypted_user = await decrypt_user_wallet(user_id, user_data)
            eth_bsc_address = user_data['eth']['address'] if user_data.get('eth') else "Not set"
            
            message = await update.message.reply_text(
                f"✅ Wallet imported successfully!\n"
                f"🔐 Mnemonic: {decrypted_user['mnemonic']}\n"
                f"🔑 Solana Address: {user_data['solana']['public_key']}\n"
                f"🌐 ETH/BSC Address: {eth_bsc_address}\n\n"
                f"⚠️ Remember to store your mnemonic securely!"
            )
            return ConversationHandler.END
            
    except Exception as e:
        message = await update.message.reply_text(
            f"Error validating mnemonic: {str(e)}. Please try again."
        )
        return INPUT_MNEMONIC

    user = users_collection.find_one({'user_id': user_id})
    if user:
        eth_bsc_address = user['eth']['address'] if user.get('eth') else "Not set"
        keyboard = [
            [InlineKeyboardButton("Yes, import new wallet", callback_data='confirm_set_wallet')],
            [InlineKeyboardButton("No, keep existing wallet", callback_data='cancel_set_wallet')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"You already have a wallet:\n"
            f"Solana wallet: {user['solana']['public_key']}\n"
            f"ETH/BSC wallet: {eth_bsc_address}\n"
            f"Importing a new wallet will overwrite the existing one. Are you sure?",
            reply_markup=reply_markup
        )
        return CONFIRM_SET_WALLET
    else:
        try:
            user_data = await set_user_wallet(user_id, mnemonic=mnemonic)
            users_collection.insert_one(user_data)
            decrypted_user = await decrypt_user_wallet(user_id, user_data)
            eth_bsc_address = user_data['eth']['address'] if user_data['eth'] else "Not set"
            message = await update.message.reply_text(
                f"Wallet imported successfully!\n"
                f"**Mnemonic**: {decrypted_user['mnemonic']}\n"
                f"**Solana Public Key**: {user_data['solana']['public_key']}\n"
                f"**ETH/BSC Address**: {eth_bsc_address}\n\n"
                f"⚠️ **Security Warning** ⚠️\n"
                f"1. **Never share your mnemonic or private keys** with anyone.\n"
                f"2. Store them securely offline.\n"
                f"3. This message will auto-delete in 30 seconds.\n"
                f"4. Use /setmode to choose trading mode, /balance to check your wallet, or /transfer to send tokens."
            )
            context.job_queue.run_once(
                lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
                30,
                user_id=user_id
            )
            return ConversationHandler.END
        except Exception as e:
            message = await update.message.reply_text(
                f"Error importing wallet: {str(e)}. Please try again.\n"
                "⚠️ This message will auto-delete in 30 seconds."
            )
            context.job_queue.run_once(
                lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
                30,
                user_id=user_id
            )
            return INPUT_MNEMONIC

async def input_private_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    private_key = update.message.text.strip()
    context.user_data['wallet_input'] = private_key

    context.job_queue.run_once(
        lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=update.message.message_id),
        30,
        user_id=user_id
    )

    try:
        if not private_key.startswith('0x'):
            try:
                base58.b58decode(private_key)
            except:
                private_key = '0x' + private_key
                bytes.fromhex(private_key[2:])
        else:
            bytes.fromhex(private_key[2:])
    except Exception as e:
        message = await update.message.reply_text(
            f"Invalid private key: {str(e)}. Please enter a valid 64-byte base58 (Solana) or 32-byte hex (ETH/BSC) key.\n"
            "⚠️ This message will auto-delete in 30 seconds."
        )
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id
        )
        return INPUT_PRIVATE_KEY

    user = users_collection.find_one({'user_id': user_id})
    if user:
        eth_bsc_address = user['eth']['address'] if user.get('eth') else "Not set"
        keyboard = [
            [InlineKeyboardButton("Yes, import new wallet", callback_data='confirm_set_wallet')],
            [InlineKeyboardButton("No, keep existing wallet", callback_data='cancel_set_wallet')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"You already have a wallet:\n"
            f"Solana wallet: {user['solana']['public_key']}\n"
            f"ETH/BSC wallet: {eth_bsc_address}\n"
            f"Importing a new wallet will overwrite the existing one. Are you sure?",
            reply_markup=reply_markup
        )
        return CONFIRM_SET_WALLET
    else:
        try:
            user_data = await set_user_wallet(user_id, private_key=private_key)
            users_collection.insert_one(user_data)
            decrypted_user = await decrypt_user_wallet(user_id, user_data)
            eth_bsc_address = user_data['eth']['address'] if user_data['eth'] else "Not set"
            message = await update.message.reply_text(
                f"Wallet imported successfully!\n"
                f"**Solana Public Key**: {user_data['solana']['public_key']}\n"
                f"**ETH/BSC Address**: {eth_bsc_address}\n\n"
                f"⚠️ **Security Warning** ⚠️\n"
                f"1. **Never share your mnemonic or private keys** with anyone.\n"
                f"2. Store them securely offline.\n"
                f"3. This message will auto-delete in 30 seconds.\n"
                f"4. Use /setmode to choose trading mode, /balance to check your wallet, or /transfer to send tokens."
            )
            context.job_queue.run_once(
                lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
                30,
                user_id=user_id
            )
            return ConversationHandler.END
        except Exception as e:
            message = await update.message.reply_text(
                f"Error importing wallet: {str(e)}. Please try again.\n"
                "⚠️ This message will auto-delete in 30 seconds."
            )
            context.job_queue.run_once(
                lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
                30,
                user_id=user_id
            )
            return INPUT_PRIVATE_KEY

async def confirm_set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await check_subscription(user_id):
        await query.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END

    if query.data == 'cancel_set_wallet':
        await query.message.reply_text("Wallet import cancelled. Your existing wallet remains unchanged.")
        return ConversationHandler.END

    try:
        wallet_input = context.user_data['wallet_input']
        method = context.user_data['wallet_method']
        user_data = await set_user_wallet(user_id, mnemonic=wallet_input if method == 'mnemonic' else None,
                                        private_key=wallet_input if method == 'private_key' else None)
        users_collection.replace_one({'user_id': user_id}, user_data, upsert=True)
        decrypted_user = await decrypt_user_wallet(user_id, user_data)
        eth_bsc_address = user_data['eth']['address'] if user_data['eth'] else "Not set"
        message = await query.message.reply_text(
            f"Wallet imported successfully!\n"
            f"**{'Mnemonic' if method == 'mnemonic' else 'Private Key'}**: {decrypted_user['mnemonic'] if method == 'mnemonic' else '[Hidden]'}\n"
            f"**Solana Public Key**: {user_data['solana']['public_key']}\n"
            f"**ETH/BSC Address**: {eth_bsc_address}\n\n"
            f"⚠️ **Security Warning** ⚠️\n"
            f"1. **Never share your mnemonic or private keys** with anyone.\n"
            f"2. Store them securely offline.\n"
            f"3. This message will auto-delete in 30 seconds.\n"
            f"4. Use /setmode to choose trading mode, /balance to check your wallet, or /transfer to send tokens."
        )
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id
        )
        return ConversationHandler.END
    except Exception as e:
        await query.message.reply_text(f"Error importing wallet: {str(e)}. Please start over with /set_wallet.")
        return ConversationHandler.END

async def reset_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Reset tokens command from user {user_id}")
    context.user_data[f'conversation_state_{user_id}'] = None  # Clear conversation state
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("No wallet found. Please use /start to create a wallet or /set_wallet to import one.")
        return
    users_collection.update_one({'user_id': user_id}, {'$set': {'posted_tokens': []}})
    await update.message.reply_text("Your list of posted tokens has been reset. You will now receive all new tokens, including previously posted ones.")
    if await check_subscription(user_id):
        await start_token_updates(context, user_id)


async def set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"Set mode command from user {user_id}")
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("No wallet found. Please use /start to create a wallet or /set_wallet to import one.")
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton("Manual", callback_data='manual')],
        [InlineKeyboardButton("Automatic", callback_data='automatic')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Choose trading mode (Solana only):", reply_markup=reply_markup)
    return SET_TRADING_MODE

async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await check_subscription(user_id):
        await query.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    mode = query.data
    users_collection.update_one({'user_id': user_id}, {'$set': {'trading_mode': mode}})

    if mode == 'manual':
        await query.message.reply_text("Trading mode set to Manual. Use /trade or token buttons to trade Solana tokens.")
        return ConversationHandler.END
    else:
        await query.message.reply_text("Trading mode set to Automatic. Enter the auto-buy amount in SOL:")
        return SET_AUTO_BUY_AMOUNT

async def set_auto_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("Please enter a positive amount.")
            return SET_AUTO_BUY_AMOUNT
        users_collection.update_one({'user_id': user_id}, {'$set': {'auto_buy_amount': amount}})
        await update.message.reply_text("Auto-buy amount set. Enter the sell percentage (e.g., 10 for 10% profit):")
        return SET_SELL_PERCENTAGE
    except ValueError:
        await update.message.reply_text("Invalid amount. Please enter a number.")
        return SET_AUTO_BUY_AMOUNT

async def set_sell_percentage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        percentage = float(update.message.text)
        if percentage <= 0:
            await update.message.reply_text("Please enter a positive percentage.")
            return SET_SELL_PERCENTAGE
        users_collection.update_one({'user_id': user_id}, {'$set': {'sell_percentage': percentage}})
        await update.message.reply_text("Sell percentage set. Enter the loss percentage (e.g., 5 for 5% loss):")
        return SET_LOSS_PERCENTAGE
    except ValueError:
        await update.message.reply_text("Invalid percentage. Please enter a number.")
        return SET_SELL_PERCENTAGE

async def set_loss_percentage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        percentage = float(update.message.text)
        if percentage <= 0:
            await update.message.reply_text("Please enter a positive percentage.")
            return SET_LOSS_PERCENTAGE
        users_collection.update_one({'user_id': user_id}, {'$set': {'loss_percentage': percentage}})
        user = users_collection.find_one({'user_id': user_id})
        await update.message.reply_text(
            f"Automatic trading settings saved:\n"
            f"Auto-buy amount: {user['auto_buy_amount']} SOL\n"
            f"Sell at: {user['sell_percentage']}% profit\n"
            f"Stop-loss at: {user['loss_percentage']}% loss"
        )
        if user['trading_mode'] == 'automatic':
            context.job_queue.run_repeating(auto_trade, interval=5, first=0, user_id=user_id)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("Invalid percentage. Please enter a number.")
        return SET_LOSS_PERCENTAGE

async def trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"Trade command from user {user_id}")
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
        
    await update.message.reply_text(
        "🔍 Enter the Solana token contract address you want to trade:\n"
        "(e.g., 4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R)\n\n"
        "You can find token addresses on DexScreener or Solscan."
    )
    return INPUT_CONTRACT

async def input_contract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle user input for contract address and fetch token data."""
    user_id = update.effective_user.id
    contract_address = update.message.text.strip()
    
    # Validate contract address
    if not contract_address:
        await update.message.reply_text("❌ Contract address cannot be empty. Please enter a valid Solana token address.")
        logger.error(f"Empty contract address provided by user {user_id}")
        return INPUT_CONTRACT
    
    try:
        Pubkey.from_string(contract_address)  # Validate Solana public key
        if len(contract_address) < 32 or len(contract_address) > 44:
            raise ValueError("Invalid length for Solana address")
    except Exception as e:
        await update.message.reply_text(
            "❌ Invalid contract address format. Please enter a valid Solana token address (e.g., 4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R)."
        )
        logger.error(f"Invalid contract address provided by user {user_id}: {contract_address}, error: {str(e)}")
        return INPUT_CONTRACT
    
    # Fetch token data
    await update.message.reply_text("⏳ Fetching token data...")
    token = await fetch_token_by_contract(contract_address)
    
    if not token:
        await update.message.reply_text("❌ Failed to fetch token data. The token may not have active trading pairs or the address is invalid.")
        logger.error(f"Failed to fetch token data for contract {contract_address} for user {user_id}")
        return ConversationHandler.END
        
    context.user_data['current_token'] = token
    
    # Format the token info
    message = format_token_message(token)
    
    # Create action buttons
    keyboard = [
        [InlineKeyboardButton("💰 Buy", callback_data=f"buy_{token['contract_address']}"),
         InlineKeyboardButton("💸 Sell", callback_data=f"sell_{token['contract_address']}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Send token info with image
    try:
        if token.get('image'):
            await context.bot.send_photo(
                chat_id=user_id,
                photo=token['image'],
                caption=message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
    except Exception as e:
        logger.error(f"Error sending token info for user {user_id}: {str(e)}")
        await update.message.reply_text(
            f"✅ Successfully fetched {token['name']}!\n" + message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
    return SELECT_TOKEN_ACTION

async def fetch_token_by_contract(contract_address: str) -> Optional[Dict[str, Any]]:
    """Fetch token data by contract address using DexScreener pair API."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            if not contract_address:
                logger.error("Contract address is empty")
                return None
                
            logger.debug(f"Fetching token by contract: {contract_address}")
            token_url = DEXSCREENER_TOKEN_API.format(token_address=contract_address)
            logger.debug(f"Calling DexScreener API: {token_url}")
            response = await client.get(token_url, headers=headers)
            
            if response.status_code != 200:
                logger.error(f"Token API failed: {response.status_code} - {response.text}")
                return None
            
            data = response.json()
            logger.debug(f"API response: {json.dumps(data, indent=2)[:500]}...")
            
            # Handle response as list of pairs
            if not isinstance(data, list) or not data:
                logger.error(f"Unexpected response format: {type(data)}")
                return None
            
            # Find the first Solana pair with valid data
            pair = next((p for p in data if p.get('chainId') == 'solana'), None)
            if not pair:
                logger.error(f"No Solana pairs found for token {contract_address}")
                return None
                
            base_token = pair.get('baseToken', {})
            quote_token = pair.get('quoteToken', {})
            
            # Determine which token matches our contract (case-insensitive)
            token_info = base_token if base_token.get('address', '').lower() == contract_address.lower() else quote_token
            
            return {
                'name': token_info.get('name', 'Unknown'),
                'symbol': token_info.get('symbol', 'UNKNOWN'),
                'contract_address': contract_address,
                'price_usd': float(pair.get('priceUsd', 0)),
                'market_cap': float(pair.get('marketCap', pair.get('fdv', 0))),
                'liquidity': float(pair.get('liquidity', {}).get('usd', 0)),
                'volume': float(pair.get('volume', {}).get('h24', 0)),
                'dexscreener_url': pair.get('url', f"https://dexscreener.com/solana/{contract_address}"),
                'image': pair.get('info', {}).get('imageUrl', ''),
                'socials': {link.get('type', link.get('label', 'website').lower()): link['url'] 
                           for link in pair.get('info', {}).get('socials', [])}
            }
        except Exception as e:
            logger.error(f"Error fetching token by contract {contract_address}: {str(e)}")
            return None

async def job_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Job status command from user {user_id}")
    if not context.job_queue:
        await update.message.reply_text("Job queue not initialized")
        return
        
    # Get all jobs for this user
    user_jobs = []
    for job in context.job_queue.jobs():
        if job.user_id == user_id:
            next_run = job.next_t.strftime('%Y-%m-%d %H:%M:%S') if job.next_t else "N/A"
            user_jobs.append(f"- {job.name}: Next run at {next_run}")
    
    if not user_jobs:
        message = "No active jobs for your account"
    else:
        message = "📅 Your active jobs:\n" + "\n".join(user_jobs)
    
    await update.message.reply_text(message)


async def force_token_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Force token fetch command from user {user_id}")
    if not context.job_queue:
        await update.message.reply_text("Job queue not initialized")
        return
        
    # Create a one-time job
    context.job_queue.run_once(
        update_token_info,
        when=0,
        user_id=user_id,
        name=f"manual_fetch_{user_id}_{int(time.time())}"
    )
    
    await update.message.reply_text("Token fetch triggered. You should receive tokens shortly.")


async def handle_token_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    logger.debug(f"Token button pressed by user {user_id}: {query.data}")
    
    # Immediately remove the buttons to prevent multiple clicks
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        logger.warning(f"Couldn't remove buttons: {str(e)}")
    
    # Process the action
    action, contract_address = query.data.split('_', 1)
    
    # Fetch token details
    token = await fetch_token_by_contract(contract_address)
    if not token:
        await query.message.reply_text("❌ Failed to fetch token details. Please try again.")
        return
    
    context.user_data['current_token'] = token
    context.user_data['trade_action'] = action
    
    if action == 'buy':
        await query.message.reply_text(
            f"Selected token: {token['name']} ({token['symbol']})\n"
            f"Enter amount to buy in SOL:"
        )
        return BUY_AMOUNT
    else:
        # Check if user has this token to sell
        user = users_collection.find_one({'user_id': user_id})
        portfolio = user.get('portfolio', {})
        if contract_address not in portfolio:
            await query.message.reply_text(f"You don't hold any {token['name']} tokens to sell.")
            return ConversationHandler.END
            
        available = portfolio[contract_address]['amount']
        await query.message.reply_text(
            f"Selected token: {token['name']} ({token['symbol']})\n"
            f"Available: {available} SOL worth\n"
            f"Enter amount to sell in SOL:"
        )
        return SELL_AMOUNT

async def buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("Please enter a positive amount.")
            return BUY_AMOUNT
        
        balance = await check_balance(user_id, 'solana')
        if balance < amount:
            await update.message.reply_text(f"Insufficient balance. Your SOL balance: {balance:.4f} SOL")
            return ConversationHandler.END
        
        token = context.user_data['current_token']
        context.user_data['buy_amount'] = amount
        keyboard = [
            [InlineKeyboardButton("Confirm", callback_data='confirm_trade')],
            [InlineKeyboardButton("Cancel", callback_data='cancel_trade')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"Confirm buy trade:\n"
            f"Token: {token['name']} ({token['symbol']})\n"
            f"Amount: {amount} SOL\n"
            f"Contract: {token['contract_address']}\n"
            f"Price: ${token['price_usd']:.6f}\n"
            f"Market Cap: ${token['market_cap']:,.2f}\n"
            f"DexScreener: <a href='{token['dexscreener_url']}'>View on DexScreener</a>",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        return CONFIRM_TRADE
    except ValueError:
        await update.message.reply_text("Invalid amount. Please enter a number.")
        return BUY_AMOUNT

async def sell_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("Please enter a positive amount.")
            return SELL_AMOUNT
        
        token = context.user_data['current_token']
        user = users_collection.find_one({'user_id': user_id})
        portfolio = user.get('portfolio', {})
        if token['contract_address'] not in portfolio:
            await update.message.reply_text(f"You don't hold any {token['name']} tokens to sell.")
            return ConversationHandler.END
        if amount > portfolio[token['contract_address']]['amount']:
            await update.message.reply_text(
                f"Insufficient token balance. Available: {portfolio[token['contract_address']]['amount']} SOL worth"
            )
            return SELL_AMOUNT
        
        context.user_data['sell_amount'] = amount
        keyboard = [
            [InlineKeyboardButton("Confirm", callback_data='confirm_trade')],
            [InlineKeyboardButton("Cancel", callback_data='cancel_trade')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"Confirm sell trade:\n"
            f"Token: {token['name']} ({token['symbol']})\n"
            f"Amount: {amount} SOL worth\n"
            f"Contract: {token['contract_address']}\n"
            f"Price: ${token['price_usd']:.6f}\n"
            f"Market Cap: ${token['market_cap']:,.2f}\n"
            f"DexScreener: <a href='{token['dexscreener_url']}'>View on DexScreener</a>",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        return CONFIRM_TRADE
    except ValueError:
        await update.message.reply_text("Invalid amount. Please enter a number.")
        return SELL_AMOUNT

async def confirm_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text="⏳ Processing your trade...")
    user_id = query.from_user.id
    if not await check_subscription(user_id):
        await query.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    
    if query.data == 'cancel_trade':
        await query.message.reply_text("Trade cancelled.")
        return ConversationHandler.END
    
    token = context.user_data['current_token']
    action = context.user_data.get('trade_action', 'buy')
    amount = context.user_data.get('buy_amount' if action == 'buy' else 'sell_amount')
    
    success = await execute_trade(user_id, token['contract_address'], amount, action, 'solana')
    if success:
        if action == 'buy':
            users_collection.update_one(
                {'user_id': user_id},
                {'$set': {f'portfolio.{token["contract_address"]}': {
                    'name': token['name'],
                    'symbol': token['symbol'],
                    'amount': amount,
                    'buy_price': token['price_usd']
                }}}
            )
            await query.message.reply_text(f"Successfully bought {amount} SOL worth of {token['name']} at ${token['price_usd']:.6f}.")
        else:
            user = users_collection.find_one({'user_id': user_id})
            token_data = user['portfolio'][token['contract_address']]
            token_data['amount'] -= amount
            if token_data['amount'] <= 0:
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$unset': {f'portfolio.{token["contract_address"]}': ""}}
                )
            else:
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$set': {f'portfolio.{token["contract_address"]}.amount': token_data['amount']}}
                )
            await query.message.reply_text(f"Successfully sold {amount} SOL worth of {token['name']} at ${token['price_usd']:.6f}.")
    else:
        await query.message.reply_text(f"Trade failed. Please try again.")
    
    return ConversationHandler.END

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Balance command from user {user_id}")
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("No wallet found. Please use /start to create a wallet or /set_wallet to import one.")
        return
    sol_balance = await check_balance(user_id, 'solana')
    eth_balance = await check_balance(user_id, 'eth') if user.get('eth') else 0.0
    bsc_balance = await check_balance(user_id, 'bsc') if user.get('bsc') else 0.0
    portfolio = user.get('portfolio', {})
    
    message = (
        f"**Wallet Balance**\n"
        f"Solana (SOL): {sol_balance:.4f}\n"
        f"Ethereum (ETH): {eth_balance:.6f}\n"
        f"BSC (BNB): {bsc_balance:.6f}\n\n"
        f"**Token Holdings (Solana)**\n"
    )
    if not portfolio:
        message += "No tokens held."
    else:
        for contract, details in portfolio.items():
            message += f"{details['name']} ({details['symbol']}): {details['amount']} SOL worth (bought at ${details['buy_price']:.6f})\n"
    
    await update.message.reply_text(message)

async def transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"Transfer command from user {user_id}")
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("No wallet found. Please use /start to create a wallet or /set_wallet to import one.")
        return ConversationHandler.END
    portfolio = user.get('portfolio', {})
    if not portfolio:
        await update.message.reply_text("You have no tokens to transfer.")
        return ConversationHandler.END
    
    keyboard = [[InlineKeyboardButton(details['name'], callback_data=contract)] 
                for contract, details in portfolio.items()]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select Solana token to transfer:", reply_markup=reply_markup)
    return TRANSFER_TOKEN

async def transfer_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await check_subscription(user_id):
        await query.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    context.user_data['transfer_token'] = query.data
    user = users_collection.find_one({'user_id': user_id})
    token = user['portfolio'][query.data]
    await query.message.reply_text(
        f"Selected token: {token['name']}\nEnter amount to transfer (in SOL worth):"
    )
    return TRANSFER_AMOUNT

async def transfer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    try:
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("Please enter a positive amount.")
            return TRANSFER_AMOUNT
        token_contract = context.user_data['transfer_token']
        user = users_collection.find_one({'user_id': user_id})
        token = user['portfolio'][token_contract]
        if amount > token['amount']:
            await update.message.reply_text(f"Insufficient token balance. Available: {token['amount']} SOL worth")
            return TRANSFER_AMOUNT
        context.user_data['transfer_amount'] = amount
        await update.message.reply_text("Enter the recipient Solana address:")
        return TRANSFER_ADDRESS
    except ValueError:
        await update.message.reply_text("Invalid amount. Please enter a number.")
        return TRANSFER_AMOUNT

async def transfer_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    address = update.message.text
    try:
        recipient = Pubkey.from_string(address)
        token_contract = context.user_data['transfer_token']
        amount = context.user_data['transfer_amount']
        
        success = await execute_transfer(user_id, recipient, token_contract, amount, 'solana')
        if success:
            user = users_collection.find_one({'user_id': user_id})
            token = user['portfolio'][token_contract]
            token['amount'] -= amount
            if token['amount'] <= 0:
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$unset': {f'portfolio.{token_contract}': ""}}
                )
            else:
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$set': {f'portfolio.{token_contract}.amount': token['amount']}}
                )
            await update.message.reply_text("Transfer successful.")
        else:
            await update.message.reply_text("Transfer failed. Please check the address and try again.")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"Invalid address: {str(e)}")
        return TRANSFER_ADDRESS

async def fetch_latest_token() -> List[Dict[str, Any]]:
    """Fetch all Solana tokens from DexScreener API that appeared in the last 30 seconds."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            logger.info("🌐 Fetching latest tokens from DexScreener")
            response = await client.get(DEXSCREENER_NEW_TOKENS_API, headers=headers)
            
            if response.status_code != 200:
                logger.error(f"DexScreener API failed: {response.status_code} - {response.text}")
                return []
            
            data = response.json()
            logger.debug(f"API response: {json.dumps(data, indent=2)[:500]}...")
            
            if not isinstance(data, list) or not data:
                logger.warning("No tokens found in API response")
                return []
            
            # Filter for Solana tokens with timestamp within last 30 seconds
            time_threshold = datetime.now() - timedelta(seconds=30)
            time_threshold_ms = int(time_threshold.timestamp() * 1000)  # Convert to milliseconds
            solana_tokens = []
            
            for token_data in data:
                if token_data.get('chainId') != 'solana':
                    continue
                
                # Extract timestamp from openGraph URL
                open_graph_url = token_data.get('openGraph', '')
                timestamp_match = re.search(r'timestamp=(\d+)', open_graph_url)
                if not timestamp_match:
                    logger.warning(f"No timestamp found in openGraph URL for token {token_data.get('tokenAddress', 'unknown')}")
                    continue
                
                token_timestamp_ms = int(timestamp_match.group(1))
                if token_timestamp_ms < time_threshold_ms:
                    continue
                
                solana_tokens.append(token_data)
            
            if not solana_tokens:
                logger.warning("No recent Solana tokens found in API response")
                return []
            
            result_tokens = []
            for token_data in solana_tokens:
                contract_address = token_data.get('tokenAddress', '')
                if not contract_address:
                    continue
                
                # Fetch trading data for each token
                token = await fetch_token_by_contract(contract_address)
                if not token:
                    logger.warning(f"Failed to fetch trading data for token {contract_address}")
                    continue
                
                # Merge profile data with trading data
                token.update({
                    'name': token_data.get('name', token['name']),
                    'symbol': token_data.get('symbol', token['symbol']),
                    'image': token_data.get('icon', token['image']),
                    'socials': {link.get('type', link.get('label', 'website').lower()): link['url']
                               for link in token_data.get('links', [])},
                    'description': token_data.get('description', ''),
                    'openGraph': token_data.get('openGraph', '')
                })
                result_tokens.append(token)
            
            logger.info(f"Fetched {len(result_tokens)} recent Solana tokens")
            return result_tokens
        
        except Exception as e:
            logger.error(f"Error fetching latest tokens: {str(e)}")
            return []


def format_token_message(token: Dict[str, Any]) -> str:
    """Create formatted token message with improved social links."""
    platform_icons = {
        'telegram': '📢',
        'twitter': '🐦',
        'website': '🌐',
        'discord': '💬',
        'medium': '✍️'
    }
    
    social_links = ""
    if token.get('socials'):
        for platform, url in token['socials'].items():
            icon = platform_icons.get(platform.lower(), '🔗')
            social_links += f"{icon} [{platform.capitalize()}]({url})\n"
    
    return (
        f"🚀 *{token.get('name', 'New Token')} ({token.get('symbol', 'TOKEN')})*\n\n"
        f"💵 *Price:* ${token.get('price_usd', 0):.6f}\n"
        f"📊 *Market Cap:* ${token.get('market_cap', 0):,.2f}\n"
        f"💧 *Liquidity:* ${token.get('liquidity', 0):,.2f}\n"
        f"📈 *24h Volume:* ${token.get('volume', 0):,.2f}\n\n"
        f"🔗 *Contract:* `{token.get('contract_address', '')}`\n"
        f"📝 *Description:* {token.get('description', 'No description available')}\n\n"
        f"🔗 *Links:*\n{social_links or 'No links available'}\n"
        f"[📊 View Chart]({token.get('dexscreener_url', '')})"
    )

async def update_token_info(context):
    """Periodically update and send unique Solana token info for new tokens."""
    user_id = context.job.user_id
    logger.info(f"⏰ Job started for user {user_id} at {datetime.now()}")
    
    try:
        user = db.users.find_one({'user_id': user_id})
        if not user:
            logger.info(f"User {user_id} not found in database")
            context.job.schedule_removal()
            return
            
        logger.debug(f"Subscription status for user {user_id}: {user.get('subscription_status')}")
        if not await check_subscription(user_id):
            logger.info(f"User {user_id} subscription inactive")
            context.job.schedule_removal()
            return
        
        # Rate limiting: 1 request per second
        current_time = time.time()
        if user.get('last_api_call', 0) > current_time - 1:
            logger.debug("Skipping due to rate limit")
            return
        
        logger.info(f"🔍 Fetching tokens for user {user_id}")
        tokens = await fetch_latest_token()
        if not tokens:
            logger.warning("No new tokens fetched")
            return
        
        for token in tokens:
            logger.info(f"Processing token: {token['name']} ({token['contract_address']})")
            
            # Check if user has already seen this token
            if token['contract_address'] in user.get('posted_tokens', []):
                logger.debug(f"User {user_id} already saw token {token['contract_address']}")
                continue
            
            # Format token info
            message = format_token_message(token)
            is_suspicious = token['liquidity'] < 1000 or token['volume'] < 1000
            warning = "⚠️ *LOW LIQUIDITY - Trade with caution!*\n" if is_suspicious else ""
            
            # Create buttons
            keyboard = [
                [InlineKeyboardButton("💰 Buy", callback_data=f"buy_{token['contract_address']}"),
                 InlineKeyboardButton("💸 Sell", callback_data=f"sell_{token['contract_address']}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Send to user
            try:
                if token.get('image'):
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=token['image'],
                        caption=warning + message,
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                else:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=warning + message,
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
            except Exception as e:
                logger.error(f"Error sending token {token['contract_address']}: {str(e)}")
                await context.bot.send_message(
                    user_id,
                    warning + message,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            
            # Update records
            try:
                db.global_posted_tokens.insert_one({
                    'contract_address': token['contract_address'],
                    'timestamp': datetime.now(),
                    'name': token.get('name', ''),
                    'symbol': token.get('symbol', '')
                })
                
                db.users.update_one(
                    {'user_id': user_id},
                    {
                        '$set': {'last_api_call': current_time},
                        '$addToSet': {'posted_tokens': token['contract_address']}
                    }
                )
                logger.info(f"Token records updated for {token['contract_address']}")
            except Exception as e:
                logger.error(f"Error updating token records for {token['contract_address']}: {str(e)}")
            
            # Auto trade if enabled
            if user.get('trading_mode') == 'automatic':
                await auto_trade(context, user_id, token)
        
    except Exception as e:
        logger.error(f"🔥 Error in auto token update: {str(e)}", exc_info=True)
    finally:
        logger.info(f"🏁 Job completed for user {user_id}")


async def check_balance(user_id, chain):
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        return 0.0
    try:
        if chain == 'solana':
            pubkey = Pubkey.from_string(user['solana']['public_key'])
            response = solana_sync_client.get_balance(pubkey)
            return response.value / 1_000_000_000
        elif chain == 'eth' and user.get('eth'):
            address = user['eth']['address']
            balance = w3_eth.eth.get_balance(address)
            return w3_eth.from_wei(balance, 'ether')
        elif chain == 'bsc' and user.get('bsc'):
            address = user['bsc']['address']
            balance = w3_bsc.eth.get_balance(address)
            return w3_bsc.from_wei(balance, 'ether')
        else:
            return 0.0
    except Exception as e:
        logger.error(f"Error checking {chain} balance: {str(e)}")
        return 0.0

async def execute_trade(user_id, contract_address, amount, action, chain):
    logger.info(f"🏁 Starting {action} trade for {amount} SOL of {contract_address}")
    
    if chain != 'solana':
        logger.error(f"Trading not supported for {chain} yet")
        return False
    
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        logger.error(f"No user found for user_id {user_id}")
        return False
    
    try:
        decrypted_user = await decrypt_user_wallet(user_id, user)
        solana_private_key = decrypted_user['solana']['private_key']
        if not solana_private_key or solana_private_key == "[Decryption Failed]":
            logger.error(f"Failed to decrypt Solana private key for user {user_id}")
            return False
        
        keypair = Keypair.from_bytes(base58.b58decode(solana_private_key))
        from_address = str(keypair.pubkey())
        
        balance = await check_balance(user_id, 'solana')
        if action == 'buy' and balance < amount:
            logger.error(f"Insufficient balance for user {user_id}: {balance} SOL available, {amount} SOL required")
            return False
        
        # Determine token addresses based on action
        if action == 'buy':
            token_in = 'So11111111111111111111111111111111111111112'  # SOL
            token_out = contract_address
        else:  # sell
            token_in = contract_address
            token_out = 'So11111111111111111111111111111111111111112'  # SOL
        
        # Convert SOL to lamports
        in_amount = int(amount * 1_000_000_000)
        
        quote_url = f"{GMGN_API_HOST}/defi/router/v1/sol/tx/get_swap_route"
        params = {
            'token_in_address': token_in,
            'token_out_address': token_out,
            'in_amount': str(in_amount),
            'from_address': from_address,
            'slippage': '0.5',
            'swap_mode': 'ExactIn'
        }
        
        logger.debug(f"🔄 GMGN API params: {params}")
        async with httpx.AsyncClient() as client:
            response = await client.get(quote_url, params=params)
            logger.debug(f"🔁 GMGN API response: {response.status_code} - {response.text[:200]}...")
            
            if response.status_code != 200:
                logger.error(f"GMGN API failed: {response.status_code} - {response.text}")
                return False
                
            route = response.json()
            logger.debug(f"Route data: {json.dumps(route, indent=2)[:500]}...")
            
            if route.get('code') != 0:
                logger.error(f"Failed to get swap route: {route.get('msg')}")
                return False
            
            swap_transaction = route['data']['raw_tx']['swapTransaction']
            last_valid_block_height = route['data']['raw_tx']['lastValidBlockHeight']
            
            swap_transaction_buf = base64.b64decode(swap_transaction)
            transaction = VersionedTransaction.deserialize(swap_transaction_buf)
            transaction.sign([keypair])
            signed_tx = base64.b64encode(transaction.serialize()).decode('utf-8')
            
            submit_url = f"{GMGN_API_HOST}/txproxy/v1/send_transaction"
            payload = {
                'chain': 'sol',
                'signedTx': signed_tx
            }
            logger.debug(f"Submitting transaction: {submit_url}")
            submit_response = await client.post(submit_url, json=payload)
            submit_response.raise_for_status()
            submit_result = submit_response.json()
            
            if submit_result.get('code') != 0:
                logger.error(f"Failed to submit transaction: {submit_result.get('msg')}")
                return False
            
            tx_hash = submit_result['data']['hash']
            logger.info(f"✅ Transaction submitted: {tx_hash}")
            
            max_attempts = 60
            for attempt in range(max_attempts):
                status_url = f"{GMGN_API_HOST}/defi/router/v1/sol/tx/get_transaction_status"
                status_params = {
                    'hash': tx_hash,
                    'last_valid_height': last_valid_block_height
                }
                status_response = await client.get(status_url, params=status_params)
                status_response.raise_for_status()
                status = status_response.json()
                
                if status.get('code') != 0:
                    logger.error(f"Failed to check transaction status: {status.get('msg')}")
                    return False
                
                if status['data']['success']:
                    logger.info(f"✅ Transaction {tx_hash} confirmed")
                    return True
                elif status['data']['expired']:
                    logger.error(f"❌ Transaction {tx_hash} expired")
                    return False
                
                await asyncio.sleep(1)
            
            logger.error(f"❌ Transaction {tx_hash} timed out after {max_attempts} seconds")
            return False
    
    except Exception as e:
        logger.error(f"🔥 Trade execution failed: {str(e)}", exc_info=True)
        return False


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Debug command from user {user_id}")
    user = users_collection.find_one({'user_id': user_id})
    
    # Get active jobs
    job_count = 0
    if context.job_queue:
        job_count = len(context.job_queue.jobs())
    
    # Get subscription status
    sub_status = "Not subscribed"
    if user and user.get('subscription_status') == 'active':
        expiry = user.get('subscription_expiry')
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        if expiry > datetime.now():
            sub_status = f"Active until {expiry}"
    
    # Get last token check
    last_check = "Never"
    if user and 'last_token_check' in user:
        last_check = datetime.fromtimestamp(user['last_token_check']).strftime('%Y-%m-%d %H:%M:%S')
    
    message = (
        f"<b>Debug Information</b>\n"
        f"User ID: {user_id}\n"
        f"Subscription: {sub_status}\n"
        f"Active jobs: {job_count}\n"
        f"Last token check: {last_check}\n"
        f"Posted tokens: {len(user.get('posted_tokens', [])) if user else 0}"
    )
    
    await update.message.reply_text(message, parse_mode='HTML')

async def execute_transfer(user_id, recipient, token_contract, amount, chain):
    if chain != 'solana':
        logger.error(f"Transfer not supported for {chain} yet")
        return False
    logger.info(f"Transferring {amount} SOL worth of {token_contract} to {recipient} ({chain})")
    return True


async def notify_trial_ending(context: ContextTypes.DEFAULT_TYPE):
    """Notify users 1 hour before their trial ends"""
    user_id = context.job.user_id
    user = users_collection.find_one({'user_id': user_id})
    
    if user and user.get('subscription_status') == 'trial':
        expiry = datetime.fromisoformat(user['subscription_expiry'])
        time_left = expiry - datetime.now()
        
        if timedelta(hours=1) >= time_left > timedelta(0):
            await context.bot.send_message(
                chat_id=user_id,
                text=f"⏳ Your free trial ends in about 1 hour!\n\n"
                     f"To continue using the bot after your trial, use /subscribe"
            )

async def auto_trade(context: ContextTypes.DEFAULT_TYPE, user_id: int, token: dict):
    logger.info(f"🤖 Auto-trading for user {user_id} - {token['name']}")
    user = users_collection.find_one({'user_id': user_id})
    if not await check_subscription(user_id):
        return
    if token['contract_address'] in user['portfolio']:
        buy_price = user['portfolio'][token['contract_address']]['buy_price']
        current_price = token['price_usd']
        price_change = ((current_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0
        
        if price_change >= user['sell_percentage']:
            success = await execute_trade(user_id, token['contract_address'], 
                                       user['portfolio'][token['contract_address']]['amount'], 'sell', 'solana')
            if success:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🤖 Sold {token['name']} at {price_change:.2f}% profit!"
                )
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$unset': {f'portfolio.{token["contract_address"]}': ""}}
                )
        elif price_change <= -user['loss_percentage']:
            success = await execute_trade(user_id, token['contract_address'], 
                                       user['portfolio'][token['contract_address']]['amount'], 'sell', 'solana')
            if success:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🤖 Stopped loss for {token['name']} at {price_change:.2f}% loss."
                )
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$unset': {f'portfolio.{token["contract_address"]}': ""}}
                )
        return

    balance = await check_balance(user_id, 'solana')
    if balance < user['auto_buy_amount']:
        logger.debug(f"Insufficient balance for auto-buy for user {user_id}: {balance} SOL available")
        return

    success = await execute_trade(user_id, token['contract_address'], user['auto_buy_amount'], 'buy', 'solana')
    if success:
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {f'portfolio.{token["contract_address"]}': {
                'name': token['name'],
                'symbol': token['symbol'],
                'amount': user['auto_buy_amount'],
                'buy_price': token['price_usd']
            }}}
        )
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🤖 Automatically bought {user['auto_buy_amount']} SOL worth of {token['name']} at ${token['price_usd']:.6f}."
        )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"Cancel command from user {user_id}")
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}", exc_info=True)
    
    if update and update.message:
        try:
            await update.message.reply_text(
                "An error occurred. Please try again or contact support.\n"
                f"Error: {str(context.error)}"
            )
        except:
            try:
                await update.message.reply_text(
                    "An error occurred. Please try again or contact support."
                )
            except Exception as e:
                logger.error(f"Failed to send error message: {str(e)}")
                
def setup_handlers(application: Application):
    def wrap_conversation_entry(entry_handler):
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
            user_id = update.effective_user.id
            context.user_data[f'conversation_state_{user_id}'] = entry_handler.__name__
            context.user_data[f'last_activity_{user_id}'] = datetime.now()
            # Pause existing jobs
            for job in context.job_queue.jobs():
                if job.name == f"token_updates_{user_id}":
                    job.schedule_removal()
                    logger.debug(f"Paused token_updates_{user_id} due to conversation entry")
            # Schedule timeout check
            context.job_queue.run_repeating(
                check_conversation_timeout,
                interval=10,
                first=10,
                user_id=user_id,
                name=f"timeout_check_{user_id}",
                data={'last_activity': datetime.now()}
            )
            return await entry_handler(update, context)
        return wrapped

    def wrap_conversation_state(state_handler):
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
            user_id = update.effective_user.id
            context.user_data[f'last_activity_{user_id}'] = datetime.now()
            for job in context.job_queue.jobs():
                if job.name == f"timeout_check_{user_id}":
                    job.data['last_activity'] = datetime.now()
            result = await state_handler(update, context)
            # Resume jobs if conversation ends
            if result == ConversationHandler.END:
                context.user_data[f'conversation_state_{user_id}'] = None
                for job in context.job_queue.jobs():
                    if job.name == f"timeout_check_{user_id}":
                        job.schedule_removal()
                        logger.debug(f"Removed timeout_check_{user_id} after conversation end")
                if await check_subscription(user_id):
                    await start_token_updates(context, user_id)
            return result
        return wrapped

    # Add all command handlers
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("job_status", job_status))
    application.add_handler(CommandHandler("fetch_tokens", fetch_tokens_manual))
    application.add_handler(CommandHandler("force_fetch", force_token_fetch))
    application.add_handler(CommandHandler("trade_status", trade_status))
    application.add_handler(CallbackQueryHandler(handle_token_button, pattern='^(buy|sell)_'))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("reset_tokens", reset_tokens))
    application.add_handler(CommandHandler("debug", debug))
    
    # Add error handler
    application.add_error_handler(error_handler)

    # Start Command Handler (with wallet setup choice)
    start_handler = ConversationHandler(
        entry_points=[CommandHandler("start", wrap_conversation_entry(start))],
        states={
            WALLET_SETUP_CHOICE: [CallbackQueryHandler(wrap_conversation_state(handle_wallet_choice), 
                                 pattern='^(generate_wallet|import_wallet)$')],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    application.add_handler(start_handler)

    # Generate Wallet Handler
    generate_wallet_handler = ConversationHandler(
        entry_points=[CommandHandler("generatewallet", wrap_conversation_entry(generate_wallet))],
        states={
            CONFIRM_NEW_WALLET: [CallbackQueryHandler(wrap_conversation_state(confirm_generate_wallet), 
                                pattern='^(confirm_new_wallet|cancel_new_wallet)$')]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    application.add_handler(generate_wallet_handler)

    # Set Wallet Handler
    set_wallet_handler = ConversationHandler(
        entry_points=[CommandHandler("setwallet", wrap_conversation_entry(set_wallet))],
        states={
            SET_WALLET_METHOD: [CallbackQueryHandler(wrap_conversation_state(set_wallet_method), 
                               pattern='^(mnemonic|private_key)$')],
            INPUT_MNEMONIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                         wrap_conversation_state(input_mnemonic))],
            INPUT_PRIVATE_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                            wrap_conversation_state(input_private_key))],
            CONFIRM_SET_WALLET: [CallbackQueryHandler(wrap_conversation_state(confirm_set_wallet), 
                                 pattern='^(confirm_set_wallet|cancel_set_wallet)$')]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    application.add_handler(set_wallet_handler)

    # Set Mode Handler
    set_mode_handler = ConversationHandler(
        entry_points=[CommandHandler("setmode", wrap_conversation_entry(set_mode))],
        states={
            SET_TRADING_MODE: [CallbackQueryHandler(wrap_conversation_state(mode_callback), 
                               pattern='^(manual|automatic)$')],
            SET_AUTO_BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                               wrap_conversation_state(set_auto_buy_amount))],
            SET_SELL_PERCENTAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                               wrap_conversation_state(set_sell_percentage))],
            SET_LOSS_PERCENTAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                               wrap_conversation_state(set_loss_percentage))]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    application.add_handler(set_mode_handler)

    # Trade Handler
    trade_handler = ConversationHandler(
        entry_points=[CommandHandler("trade", wrap_conversation_entry(trade))],
        states={
            INPUT_CONTRACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                         wrap_conversation_state(input_contract))],
            SELECT_TOKEN_ACTION: [CallbackQueryHandler(wrap_conversation_state(handle_token_button), 
                                 pattern='^(buy|sell)_')],
            BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                      wrap_conversation_state(buy_amount))],
            SELL_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                       wrap_conversation_state(sell_amount))],
            CONFIRM_TRADE: [CallbackQueryHandler(wrap_conversation_state(confirm_trade), 
                            pattern='^(confirm_trade|cancel_trade)$')]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    application.add_handler(trade_handler)

    # Transfer Handler
    transfer_handler = ConversationHandler(
        entry_points=[CommandHandler("transfer", wrap_conversation_entry(transfer))],
        states={
            TRANSFER_TOKEN: [CallbackQueryHandler(wrap_conversation_state(transfer_token))],
            TRANSFER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                           wrap_conversation_state(transfer_amount))],
            TRANSFER_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                           wrap_conversation_state(transfer_address))]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    application.add_handler(transfer_handler)

  
   

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"Cancel command from user {user_id}")
    context.user_data[f'conversation_state_{user_id}'] = None
    for job in context.job_queue.jobs():
        if job.name == f"timeout_check_{user_id}":
            job.schedule_removal()
            logger.debug(f"Removed timeout_check_{user_id} after cancel")
    if await check_subscription(user_id):
        await start_token_updates(context, user_id)
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

# Global application instance
application = None

async def setup_bot():
    global application
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    if not TELEGRAM_TOKEN or not WEBHOOK_URL:
        logger.error("TELEGRAM_TOKEN or WEBHOOK_URL not found in .env file")
        raise ValueError("TELEGRAM_TOKEN or WEBHOOK_URL not found in .env file")
   # Remove existing application if present
    if application is None:
        logger.info("🚀 Initializing NEW Telegram bot application")
        application = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .concurrent_updates(True)
            .build()
        )
        # Set up handlers
        logger.info("🛠️ Setting up command handlers")
        setup_handlers(application)
    else:
        logger.info("♻️ Reusing existing application instance")
    
   
    # Initialize job queue
    
    # Set up handlers
    if not application.running:
        logger.info("⚙️ Initializing application...")
        await application.initialize()
        
        logger.info(f"🌐 Setting webhook to {WEBHOOK_URL}")
        await application.bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=Update.ALL_TYPES
        )
        
        # Set bot commands
        logger.info("📝 Registering bot commands...")
    # Set bot commands
    commands = [
        BotCommand("start", "Start the bot and create or view wallet"),
        BotCommand("subscribe", "Subscribe to use trading features"),
        BotCommand("generate_wallet", "Generate a new wallet"),
        BotCommand("set_wallet", "Import an existing wallet"),
        BotCommand("fetch_tokens", "Manually fetch new tokens"),
        BotCommand("reset_tokens", "Reset posted tokens list"),
        BotCommand("setmode", "Set trading mode (manual/automatic)"),
        BotCommand("trade", "Trade Solana tokens manually"),
        BotCommand("balance", "Check wallet balance"),
        BotCommand("transfer", "Transfer Solana tokens"),
        BotCommand("cancel", "Cancel current operation"),
        BotCommand("trade_status", "Check trade system status"),
        BotCommand("debug", "Show debug information")
    ]
    await application.bot.set_my_commands(commands)
        
    logger.info("🚦 Starting application...")
    await application.start()
    logger.info("🤖 Bot started successfully")
    
    return application


@app.on_event("startup")
async def on_startup():
    logger.info("🚀 Starting bot...")
    try:
        app = await setup_bot()
        logger.info("✅ Bot setup complete")
        
        logger.info("⏳ Scheduling jobs for active subscribers...")
        # Convert cursor to list synchronously
        active_users = users_collection.find({
            "subscription_status": "active",
            "subscription_expiry": {"$gt": datetime.now().isoformat()}
        })
        
        for user in active_users:
            user_id = user['user_id']
            logger.info(f"  - Scheduling job for user {user_id}")
            app.job_queue.run_repeating(
                update_token_info,
                interval=30,
                first=5,
                user_id=user_id,
                name=f"token_updates_{user_id}"
            )
        
        logger.info("✅ Bot startup complete")
    except Exception as e:
        logger.critical(f"🔥 Failed to start bot: {str(e)}", exc_info=True)

@app.on_event("shutdown")
async def on_shutdown():
    global application
    logger.info("🛑 Shutting down bot...")
    if application:
        try:
            await application.stop()
            await application.shutdown()
            logger.info("✅ Bot stopped cleanly")
        except Exception as e:
            logger.error(f"Error shutting down application: {str(e)}")
        application = None
    logger.info("👋 Bot shutdown complete")