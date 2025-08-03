import asyncio
import logging
import requests
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
    filters
)
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
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)
logging.getLogger('telegram.ext.ConversationHandler').setLevel(logging.CRITICAL)
logging.getLogger('httpx').setLevel(logging.WARNING)

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
        update_data = await request.json()
        update = Update.de_json(update_data, application.bot)
        
        # Process the update
        async with application:
            await application.process_update(update)
            
        return JSONResponse(content={'status': 'ok'})
    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        return JSONResponse(content={'error': str(e)}, status_code=500)

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
        logger.debug("MongoDB connection successful")
        db = mongo_client.get_database('trading_bot')
        users_collection = db.users
        users_collection.create_index('user_id', unique=True)
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

if 'global_posted_tokens' not in db.list_collection_names():
    db.create_collection('global_posted_tokens')
    db.global_posted_tokens.create_index('contract_address', unique=True)
    db.global_posted_tokens.create_index('timestamp', expireAfterSeconds=86400)  # 24h expiration

# DexScreener API endpoints
DEXSCREENER_PROFILE_API = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN_API = "https://api.dexscreener.com/tokens/v1/solana/{token_address}"

# Bot states for conversation
(SET_TRADING_MODE, SET_AUTO_BUY_AMOUNT, SET_SELL_PERCENTAGE, SET_LOSS_PERCENTAGE, 
 SELECT_TOKEN, BUY_AMOUNT, CONFIRM_TRADE, TRANSFER_TOKEN, TRANSFER_AMOUNT, TRANSFER_ADDRESS,
 CONFIRM_NEW_WALLET, SET_WALLET_METHOD, INPUT_MNEMONIC, INPUT_PRIVATE_KEY, CONFIRM_SET_WALLET,
 SELECT_TOKEN_ACTION, SELL_AMOUNT) = range(17)

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
    if user.get('subscription_status') != 'active':
        logger.debug(f"User {user_id} subscription is inactive")
        return False
    expiry = user.get('subscription_expiry')
    if not expiry:
        logger.debug(f"User {user_id} has no subscription expiry")
        return False
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry)
    if datetime.now() >= expiry:
        logger.debug(f"User {user_id} subscription expired at {expiry}")
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {'subscription_status': 'inactive', 'subscription_expiry': None}}
        )
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

    decrypted_user['mnemonic'] = safe_decrypt(user.get('mnemonic', {}))
    decrypted_user['solana']['private_key'] = safe_decrypt(user.get('solana', {}).get('private_key', {}))
    if user.get('eth'):
        decrypted_user['eth']['private_key'] = safe_decrypt(user.get('eth', {}).get('private_key', {}))
    if user.get('bsc'):
        decrypted_user['bsc']['private_key'] = safe_decrypt(user.get('bsc', {}).get('private_key', {}))
    return decrypted_user

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        mnemo = Mnemonic("english")
        mnemonic = mnemo.generate(strength=256)
        user_data = await set_user_wallet(user_id, mnemonic=mnemonic)
        users_collection.insert_one(user_data)
        try:
            decrypted_user = await decrypt_user_wallet(user_id, user_data)
            eth_bsc_address = user_data['eth']['address'] if user_data['eth'] else "Not set"
            message = await update.message.reply_text(
                f"Welcome to the Multi-Chain Trading Bot!\n\n"
                f"A new wallet has been created for you.\n"
                f"**Mnemonic (for Solana, ETH, BSC)**: {decrypted_user['mnemonic']}\n"
                f"**Solana Public Key**: {user_data['solana']['public_key']}\n"
                f"**ETH/BSC Address**: {eth_bsc_address}\n\n"
                f"⚠️ **Security Warning** ⚠️\n"
                f"1. **Never share your mnemonic or private keys** with anyone.\n"
                f"2. Store them securely offline (e.g., on paper or a hardware wallet).\n"
                f"3. This message will auto-delete in 30 seconds for security.\n"
                f"4. Use this wallet only for trading with small amounts.\n\n"
                f"To access trading features, subscribe using /subscribe.\n"
                f"To import an existing wallet, use /set_wallet."
            )
            context.job_queue.run_once(
                lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
                30,
                user_id=user_id
            )
        except Exception as e:
            await update.message.reply_text(
                f"Error accessing wallet data: {str(e)}. Please try /generate_wallet to create a new wallet or /set_wallet to import one."
            )
            logger.error(f"Error in start for user {user_id}: {str(e)}")
            return
    else:
        try:
            decrypted_user = await decrypt_user_wallet(user_id, user)
            eth_bsc_address = user['eth']['address'] if user.get('eth') else "Not set"
            subscription_message = await get_subscription_status_message(user)
            await update.message.reply_text(
                f"Welcome back!\n"
                f"Solana wallet: {user['solana']['public_key']}\n"
                f"ETH/BSC wallet: {eth_bsc_address}\n"
                f"{subscription_message}\n"
                f"To generate a new wallet, use /generate_wallet.\n"
                f"To import an existing wallet, use /set_wallet."
            )
        except Exception as e:
            await update.message.reply_text(
                f"Error accessing wallet data: {str(e)}. Please try /generate_wallet to create a new wallet or /set_wallet to import one."
            )
            logger.error(f"Error in start for user {user_id}: {str(e)}")
            return


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

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("No wallet found. Please use /start to create a wallet.")
        return

    if await check_subscription(user_id):
        expiry = user.get('subscription_expiry')
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        await update.message.reply_text(
            f"You already have an active subscription until {expiry.strftime('%Y-%m-%d %H:%M:%S')}."
        )
        return

    try:
        usdt_amount = 5.0
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
            f"Deadline: {payment_deadline.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Your subscription will activate automatically after payment is confirmed."
        )
        await start_token_updates(context, user_id)
    except Exception as e:
        logger.error(f"Error creating subscription for user {user_id}: {str(e)}")
        await update.message.reply_text("Error initiating subscription. Please try again later.")
    return



   

async def generate_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("No wallet found. Please use /start to create a wallet.")
        return ConversationHandler.END
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    eth_bsc_address = user['eth']['address'] if user.get('eth') else "Not set"
    keyboard = [
        [InlineKeyboardButton("Yes, generate new wallet", callback_data='confirm_new_wallet')],
        [InlineKeyboardButton("No, keep existing wallet", callback_data='cancel_new_wallet')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"You already have a wallet:\n"
        f"Solana wallet: {user['solana']['public_key']}\n"
        f"ETH/BSC wallet: {eth_bsc_address}\n"
        f"Generating a new wallet will overwrite the existing one. Are you sure?",
        reply_markup=reply_markup
    )
    return CONFIRM_NEW_WALLET

async def confirm_generate_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await check_subscription(user_id):
        await query.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END

    if query.data == 'cancel_new_wallet':
        await query.message.reply_text("Wallet generation cancelled. Your existing wallet remains unchanged.")
        return ConversationHandler.END

    mnemo = Mnemonic("english")
    mnemonic = mnemo.generate(strength=256)
    user_data = await set_user_wallet(user_id, mnemonic=mnemonic)
    users_collection.replace_one({'user_id': user_id}, user_data, upsert=True)
    decrypted_user = await decrypt_user_wallet(user_id, user_data)
    eth_bsc_address = user_data['eth']['address'] if user_data['eth'] else "Not set"
    message = await query.message.reply_text(
        f"New wallet generated!\n"
        f"**Mnemonic (for Solana, ETH, BSC)**: {decrypted_user['mnemonic']}\n"
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
    if context.job_queue is None:
        await query.message.reply_text(
            "Error: JobQueue is not available. Install 'python-telegram-bot[job-queue]'."
        )
        logger.error("JobQueue is not initialized.")
        return ConversationHandler.END
    context.job_queue.run_repeating(update_token_info, interval=5, first=0, user_id=user_id)
    return ConversationHandler.END

async def set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
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
        "- Mnemonic: Enter your 24-word BIP-39 mnemonic phrase.\n"
        "- Private Key: Enter your Solana (64-byte base58) or ETH/BSC (32-byte hex) private key.\n"
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
    except Exception as e:
        message = await update.message.reply_text(
            f"Error validating mnemonic: {str(e)}. Please try again.\n"
            "⚠️ This message will auto-delete in 30 seconds."
        )
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id
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
            if context.job_queue is None:
                await update.message.reply_text(
                    "Error: JobQueue is not available. Install 'python-telegram-bot[job-queue]'."
                )
                logger.error("JobQueue is not initialized.")
                return ConversationHandler.END
            context.job_queue.run_repeating(update_token_info, interval=5, first=0, user_id=user_id)
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
            if context.job_queue is None:
                await update.message.reply_text(
                    "Error: JobQueue is not available. Install 'python-telegram-bot[job-queue]'."
                )
                logger.error("JobQueue is not initialized.")
                return ConversationHandler.END
            context.job_queue.run_repeating(update_token_info, interval=5, first=0, user_id=user_id)
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
        if context.job_queue is None:
            await query.message.reply_text(
                "Error: JobQueue is not available. Install 'python-telegram-bot[job-queue]'."
            )
            logger.error("JobQueue is not initialized.")
            return ConversationHandler.END
        context.job_queue.run_repeating(update_token_info, interval=5, first=0, user_id=user_id)
        return ConversationHandler.END
    except Exception as e:
        await query.message.reply_text(f"Error importing wallet: {str(e)}. Please start over with /set_wallet.")
        return ConversationHandler.END

async def reset_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("No wallet found. Please use /start to create a wallet or /set_wallet to import one.")
        return
    users_collection.update_one({'user_id': user_id}, {'$set': {'posted_tokens': []}})
    await update.message.reply_text("Your list of posted tokens has been reset. You will now receive all new tokens, including previously posted ones.")

async def set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
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
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
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
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
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
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
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
        if context.job_queue is None:
            await update.message.reply_text(
                "Error: JobQueue is not available. Install 'python-telegram-bot[job-queue]'."
            )
            logger.error("JobQueue is not initialized.")
            return ConversationHandler.END
        if user['trading_mode'] == 'automatic':
            context.job_queue.run_repeating(auto_trade, interval=5, first=0, user_id=user_id)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("Invalid percentage. Please enter a number.")
        return SET_LOSS_PERCENTAGE

async def trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("No wallet found. Please use /start to create a wallet or /set_wallet to import one.")
        return ConversationHandler.END
    if user['trading_mode'] != 'manual':
        await update.message.reply_text("Please set trading mode to Manual using /setmode.")
        return ConversationHandler.END
    
    token = await fetch_latest_token()
    if not token:
        await update.message.reply_text("Failed to fetch token data. Try again later.")
        return ConversationHandler.END
    
    if token['contract_address'] in user.get('posted_tokens', []):
        await update.message.reply_text("No new unique tokens available at this time. Try again later or use /reset_tokens to clear posted tokens.")
        return ConversationHandler.END
    
    context.user_data['current_token'] = token
    keyboard = [
        [InlineKeyboardButton("Buy", callback_data=f"buy_{token['contract_address']}")],
        [InlineKeyboardButton("Sell", callback_data=f"sell_{token['contract_address']}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"<b>Latest Solana Token</b>\n"
        f"Name: {token['name']} ({token['symbol']})\n"
        f"Contract: {token['contract_address']}\n"
        f"Price: ${token['price_usd']:.6f}\n"
        f"Market Cap: ${token['market_cap']:,.2f}\n"
        f"DexScreener: <a href='{token['dexscreener_url']}'>View on DexScreener</a>\n"
        f"Select an action:",
        parse_mode='HTML',
        reply_markup=reply_markup
    )
    users_collection.update_one(
        {'user_id': user_id},
        {'$addToSet': {'posted_tokens': token['contract_address']}}
    )
    return SELECT_TOKEN_ACTION

async def select_token_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    logger.debug(f"select_token_action called for user {user_id} with data: {query.data}")
    
    if not await check_subscription(user_id):
        logger.debug(f"User {user_id} blocked due to inactive subscription")
        await query.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        logger.debug(f"No user found for user_id {user_id}")
        await query.message.reply_text("No wallet found. Please use /start to create a wallet or /set_wallet to import one.")
        return ConversationHandler.END
    if user['trading_mode'] != 'manual':
        logger.debug(f"User {user_id} not in manual trading mode")
        await query.message.reply_text("Please set trading mode to Manual using /setmode.")
        return ConversationHandler.END

    try:
        action, contract_address = query.data.split('_', 1)
        logger.debug(f"Parsed action: {action}, contract_address: {contract_address}")
    except ValueError:
        logger.error(f"Invalid callback data for user {user_id}: {query.data}")
        await query.message.reply_text("Error processing action. Please try again.")
        return ConversationHandler.END

    token = context.user_data.get('current_token', {})
    if not token or token['contract_address'] != contract_address:
        logger.debug(f"Token mismatch or missing for user {user_id}, fetching new data for {contract_address}")
        async with httpx.AsyncClient() as client:
            token_url = DEXSCREENER_TOKEN_API.format(token_address=contract_address)
            try:
                token_response = await client.get(token_url)
                token_response.raise_for_status()
                token_data = token_response.json()
                if not token_data or not isinstance(token_data, list) or not token_data[0]:
                    logger.error(f"Invalid token data for {contract_address}: {token_data}")
                    await query.message.reply_text("Failed to fetch token data. Try again later.")
                    return ConversationHandler.END
                pair_data = token_data[0]
                token = {
                    'name': pair_data.get('name', 'Unknown'),
                    'symbol': pair_data.get('symbol', 'Unknown'),
                    'contract_address': contract_address,
                    'price_usd': float(pair_data.get('priceUsd', '0.0')),
                    'market_cap': float(pair_data.get('marketCap', 0.0)),
                    'dexscreener_url': f"https://dexscreener.com/solana/{contract_address}"
                }
                context.user_data['current_token'] = token
                logger.debug(f"Updated token data for user {user_id}: {token}")
            except Exception as e:
                logger.error(f"Error fetching token data for {contract_address}: {str(e)}")
                await query.message.reply_text("Failed to fetch token data. Try again later.")
                return ConversationHandler.END

    context.user_data['trade_action'] = action
    if action == 'buy':
        logger.debug(f"User {user_id} selected buy for {token['name']}")
        await query.message.reply_text(
            f"Selected token: {token['name']} ({token['symbol']})\nEnter amount to buy in SOL:"
        )
        return BUY_AMOUNT
    else:
        portfolio = user.get('portfolio', {})
        if contract_address not in portfolio:
            logger.debug(f"User {user_id} has no {token['name']} to sell")
            await query.message.reply_text(f"You don't hold any {token['name']} tokens to sell.")
            return ConversationHandler.END
        logger.debug(f"User {user_id} selected sell for {token['name']}, available: {portfolio[contract_address]['amount']} SOL")
        await query.message.reply_text(
            f"Selected token: {token['name']} ({token['symbol']})\n"
            f"Available: {portfolio[contract_address]['amount']} SOL worth\n"
            f"Enter amount to sell in SOL:"
        )
        return SELL_AMOUNT

async def buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
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
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
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

async def fetch_latest_token():
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(DEXSCREENER_PROFILE_API, params={'chainId': 'solana'})
            response.raise_for_status()
            data = response.json()
            
            if not isinstance(data, list):
                logger.error(f"Expected a list from DexScreener profile API, got {type(data)}: {data}")
                return None
            
            solana_tokens = [token for token in data if isinstance(token, dict) and token.get('chainId') == 'solana']
            if not solana_tokens:
                logger.warning("No Solana tokens found in profile API response")
                return None
            
            token = solana_tokens[0]
            token_address = token.get('tokenAddress', '')
            if not token_address:
                logger.warning("No tokenAddress found in token data")
                return None
            
            token_url = DEXSCREENER_TOKEN_API.format(token_address=token_address)
            token_response = await client.get(token_url)
            token_response.raise_for_status()
            token_data = token_response.json()

            if not isinstance(token_data, list):
                logger.error(f"Expected a list from token API, got {type(token_data)}: {token_data}")
                return None
            
            if not token_data:
                logger.warning("No token pairs found in token API response")
                return None
            pair_data = token_data[0]

            if not isinstance(pair_data, dict):
                logger.error(f"Expected a dict for pair data, got {type(pair_data)}: {pair_data}")
                return None

            name = pair_data.get('name', token.get('description', 'Unknown').split()[0] if token.get('description') else 'Unknown')
            symbol = pair_data.get('symbol', token.get('url', '').split('/')[-1].upper() if token.get('url') else 'Unknown')
            website = next((link['url'] for link in token.get('links', []) if link.get('label') == 'Website'), 'N/A')
            social_links = {link['type']: link['url'] for link in token.get('links', []) if link.get('type')}
            dexscreener_url = f"https://dexscreener.com/solana/{token_address}"

            price_usd = float(pair_data.get('priceUsd', '0.0'))
            market_cap = float(pair_data.get('marketCap', 0.0))
            liquidity = float(pair_data.get('liquidity', {}).get('usd', 0.0))
            volume = float(pair_data.get('volume', {}).get('h24', 0.0))

            return {
                'name': name,
                'symbol': symbol,
                'contract_address': token_address,
                'price_usd': price_usd,
                'market_cap': market_cap,
                'image': token.get('icon', 'N/A'),
                'website': website,
                'socials': social_links,
                'liquidity': liquidity,
                'volume': volume,
                'dexscreener_url': dexscreener_url
            }
        except Exception as e:
            logger.error(f"Error fetching token: {str(e)}")
            return None


async def start_token_updates(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Start token updates for a subscribed user"""
    # Check if the user already has a job running
    current_jobs = context.job_queue.get_jobs_by_name(f"token_updates_{user_id}")
    if current_jobs:
        logger.debug(f"Token updates already running for user {user_id}")
        return
    
    logger.debug(f"Starting token updates for user {user_id}")
    
    # Schedule the token update job
    context.job_queue.run_repeating(
        update_token_info,
        interval=30,  # Check every 30 seconds
        first=5,       # First run in 5 seconds
        user_id=user_id,
        name=f"token_updates_{user_id}"  # Unique name for this job
    )

async def update_token_info(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.user_id
    logger.debug(f"Running token update check for user {user_id}")
    
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        logger.debug(f"No user found for user_id {user_id}, cancelling job")
        context.job.schedule_removal()
        return
        
    if not await check_subscription(user_id):
        logger.debug(f"User {user_id} subscription not active, cancelling job")
        context.job.schedule_removal()
        return
    
    # Add rate limiting to prevent too frequent checks
    last_check = user.get('last_token_check', 0)
    if time.time() - last_check < 30:
        return
    
    token = await fetch_latest_token()
    if not token:
        return
    
    # Check global posted tokens collection
    global_posted = db.global_posted_tokens
    if global_posted.find_one({'contract_address': token['contract_address']}):
        logger.debug(f"Token {token['contract_address']} already posted globally")
        return
    
    # Check if this user has already seen this token
    if token['contract_address'] in user.get('posted_tokens', []):
        logger.debug(f"User {user_id} already saw token {token['contract_address']}")
        return
    
    # Add token to global posted tokens
    global_posted.insert_one({
        'contract_address': token['contract_address'],
        'timestamp': datetime.now()
    })
    
    # Update user's last check time and add to their posted tokens
    users_collection.update_one(
        {'user_id': user_id},
        {
            '$set': {'last_token_check': time.time()},
            '$addToSet': {'posted_tokens': token['contract_address']}
        }
    )
    
    is_suspicious = token['liquidity'] < 1000 or token['volume'] < 1000
    warning = "⚠️ Low liquidity or volume detected. Trade with caution." if is_suspicious else ""
    
    social_links = "\n".join([f"{k.capitalize()}: {v}" for k, v in token['socials'].items()])
    message = (
        f"<b>ASKJAH GEM Solana Token</b>\n"
        f"Name: {token['name']} ({token['symbol']})\n\n"
        f"Contract: {token['contract_address']}\n\n"
        f"Price: ${token['price_usd']:.6f}\n"
        f"Market Cap: ${token['market_cap']:,.2f}\n\n"
        f"Liquidity: ${token['liquidity']:,.2f}\n\n"
        f"24h Volume: ${token['volume']:,.2f}\n\n"
        f"Website: {token['website']}\n\n"
        f"Socials:\n{social_links or 'N/A'}\n\n"
        f"Image: <a href='{token['image']}'>View Image</a>\n\n"
        f"DexScreener: <a href='{token['dexscreener_url']}'>View on DexScreener</a>\n"
        f"{warning}"
    )
    
    keyboard = [
        [InlineKeyboardButton("Buy", callback_data=f"buy_{token['contract_address']}")],
        [InlineKeyboardButton("Sell", callback_data=f"sell_{token['contract_address']}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=message,
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        users_collection.update_one(
            {'user_id': user_id},
            {
                '$set': {'last_api_call': time.time()},
                '$addToSet': {'posted_tokens': token['contract_address']}
            }
        )
        
        if user['trading_mode'] == 'automatic':
            await auto_trade(context, user_id=user_id, token=token)
    except Exception as e:
        logger.error(f"Error sending token info: {str(e)}")
        context.job.schedule_removal()

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
    logger.debug(f"Executing {action} trade for user {user_id}: {amount} SOL of {contract_address} on {chain}")
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
        
        input_token = 'So11111111111111111111111111111111111111112'  # SOL
        output_token = contract_address
        in_amount = str(int(amount * 1_000_000_000))  # Convert SOL to lamports
        slippage = 0.5  # 0.5% slippage
        swap_mode = 'ExactIn'
        
        if action == 'sell':
            input_token, output_token = output_token, input_token
        
        async with httpx.AsyncClient() as client:
            quote_url = f"{GMGN_API_HOST}/defi/router/v1/sol/tx/get_swap_route"
            params = {
                'token_in_address': input_token,
                'token_out_address': output_token,
                'in_amount': in_amount,
                'from_address': from_address,
                'slippage': slippage,
                'swap_mode': swap_mode
            }
            logger.debug(f"Fetching swap route for user {user_id}: {params}")
            response = await client.get(quote_url, params=params)
            response.raise_for_status()
            route = response.json()
            
            if route.get('code') != 0:
                logger.error(f"Failed to get swap route for user {user_id}: {route.get('msg')}")
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
            logger.debug(f"Submitting transaction for user {user_id}: {submit_url}")
            submit_response = await client.post(submit_url, json=payload)
            submit_response.raise_for_status()
            submit_result = submit_response.json()
            
            if submit_result.get('code') != 0:
                logger.error(f"Failed to submit transaction for user {user_id}: {submit_result.get('msg')}")
                return False
            
            tx_hash = submit_result['data']['hash']
            logger.debug(f"Transaction hash for user {user_id}: {tx_hash}")
            
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
                    logger.error(f"Failed to check transaction status for user {user_id}: {status.get('msg')}")
                    return False
                
                if status['data']['success']:
                    logger.info(f"Transaction {tx_hash} successful for user {user_id}")
                    return True
                elif status['data']['expired']:
                    logger.error(f"Transaction {tx_hash} expired for user {user_id}")
                    return False
                
                await asyncio.sleep(1)
            
            logger.error(f"Transaction {tx_hash} timed out after {max_attempts} seconds for user {user_id}")
            return False
    
    except Exception as e:
        logger.error(f"Error executing {action} trade for user {user_id}: {str(e)}", exc_info=True)
        return False

async def execute_transfer(user_id, recipient, token_contract, amount, chain):
    if chain != 'solana':
        logger.error(f"Transfer not supported for {chain} yet")
        return False
    logger.info(f"Transferring {amount} SOL worth of {token_contract} to {recipient} ({chain})")
    return True

async def auto_trade(context: ContextTypes.DEFAULT_TYPE, user_id: int, token: dict):
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
                    text=f"Sold {token['name']} at {price_change:.2f}% profit!"
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
                    text=f"Stopped loss for {token['name']} at {price_change:.2f}% loss."
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
            text=f"Automatically bought {user['auto_buy_amount']} SOL worth of {token['name']} at ${token['price_usd']:.6f}."
        )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    if update:
        await update.message.reply_text("An error occurred. Please try again or contact support.")

def setup_handlers(application: Application):
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("generate_wallet", generate_wallet)],
        states={
            CONFIRM_NEW_WALLET: [CallbackQueryHandler(confirm_generate_wallet, pattern='^(confirm_new_wallet|cancel_new_wallet)$')]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("set_wallet", set_wallet)],
        states={
            SET_WALLET_METHOD: [CallbackQueryHandler(set_wallet_method, pattern='^(mnemonic|private_key)$')],
            INPUT_MNEMONIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_mnemonic)],
            INPUT_PRIVATE_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_private_key)],
            CONFIRM_SET_WALLET: [CallbackQueryHandler(confirm_set_wallet, pattern='^(confirm_set_wallet|cancel_set_wallet)$')]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    ))
    application.add_handler(CommandHandler("reset_tokens", reset_tokens))
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("setmode", set_mode)],
        states={
            SET_TRADING_MODE: [CallbackQueryHandler(mode_callback, pattern='^(manual|automatic)$')],
            SET_AUTO_BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_auto_buy_amount)],
            SET_SELL_PERCENTAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_sell_percentage)],
            SET_LOSS_PERCENTAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_loss_percentage)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("trade", trade)],
        states={
            SELECT_TOKEN_ACTION: [CallbackQueryHandler(select_token_action, pattern='^(buy|sell)_')],
            BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_amount)],
            SELL_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_amount)],
            CONFIRM_TRADE: [CallbackQueryHandler(confirm_trade, pattern='^(confirm_trade|cancel_trade)$')]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    ))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("transfer", transfer)],
        states={
            TRANSFER_TOKEN: [CallbackQueryHandler(transfer_token)],
            TRANSFER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, transfer_amount)],
            TRANSFER_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, transfer_address)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    ))
    application.add_error_handler(error_handler)

# Global application instance
application = None

async def setup_bot():
    global application
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    if not TELEGRAM_TOKEN or not WEBHOOK_URL:
        logger.error("TELEGRAM_TOKEN or WEBHOOK_URL not found in .env file")
        raise ValueError("TELEGRAM_TOKEN or WEBHOOK_URL not found in .env file")
    
    # Create application instance
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )
    
    # Set up handlers
    setup_handlers(application)
    
    # Initialize application
    await application.initialize()
    
    # Set webhook
    await application.bot.set_webhook(
        url=WEBHOOK_URL,
        allowed_updates=Update.ALL_TYPES
    )
    logger.info(f"Webhook set to {WEBHOOK_URL}")
    
    # Set bot commands
    commands = [
        BotCommand("start", "Start the bot and create or view wallet"),
        BotCommand("subscribe", "Subscribe to use trading features"),
        BotCommand("generate_wallet", "Generate a new wallet"),
        BotCommand("set_wallet", "Import an existing wallet"),
        BotCommand("reset_tokens", "Reset posted tokens list"),
        BotCommand("setmode", "Set trading mode (manual/automatic)"),
        BotCommand("trade", "Trade Solana tokens manually"),
        BotCommand("balance", "Check wallet balance"),
        BotCommand("transfer", "Transfer Solana tokens"),
        BotCommand("cancel", "Cancel current operation")
    ]
    await application.bot.set_my_commands(commands)
    
    # Start the application
    await application.start()
    logger.info("Bot started successfully")
    
    return application

@app.on_event("startup")
async def on_startup():
    logger.info("Starting up...")
    await setup_bot()

@app.on_event("shutdown")
async def on_shutdown():
    global application
    logger.info("Shutting down...")
    if application:
        await application.stop()
        await application.shutdown()
    logger.info("Bot stopped")

if __name__ == '__main__':
    asyncio.run(main())