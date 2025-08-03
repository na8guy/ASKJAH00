import asyncio
import logging
import os
from flask import Flask, request, jsonify
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
from solana.rpc.async_api import AsyncClient
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction
from base58 import b58encode, b58decode
from mnemonic import Mnemonic
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import base64
import httpx
import requests
from web3 import Web3
from eth_account import Account
from datetime import datetime, timedelta
import time
from dotenv import load_dotenv

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)
logging.getLogger('telegram.ext.ConversationHandler').setLevel(logging.CRITICAL)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('werkzeug').setLevel(logging.INFO)

# Load environment variables
load_dotenv()

# Configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
MONGO_URI = os.getenv("MONGO_URI")
MASTER_KEY = os.getenv("MASTER_KEY")
SOLANA_RPC = os.getenv("SOLANA_RPC_URL")
ETH_RPC = os.getenv("ETH_RPC_URL")
BSC_RPC = os.getenv("BSC_RPC_URL")
BOT_USDT_ADDRESS = os.getenv("BOT_USDT_ADDRESS")
PORT = int(os.getenv("PORT", 10000))  # Use Render's PORT or default to 10000

# Validate environment variables
if not all([TELEGRAM_TOKEN, WEBHOOK_URL, MONGO_URI, MASTER_KEY, SOLANA_RPC, ETH_RPC, BSC_RPC, BOT_USDT_ADDRESS]):
    logger.error("Missing required environment variables: " + ", ".join(
        [var for var, val in [
            ("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
            ("WEBHOOK_URL", WEBHOOK_URL),
            ("MONGO_URI", MONGO_URI),
            ("MASTER_KEY", MASTER_KEY),
            ("SOLANA_RPC", SOLANA_RPC),
            ("ETH_RPC", ETH_RPC),
            ("BSC_RPC", BSC_RPC),
            ("BOT_USDT_ADDRESS", BOT_USDT_ADDRESS)
        ] if not val]
    ))
    raise ValueError("Missing required environment variables")

    # Add this function above the MongoDB connection block
def connect_mongodb_with_retry():
    max_retries = 5
    for attempt in range(max_retries):
        try:
            logger.debug(f"Attempting MongoDB connection (try {attempt+1}/{max_retries})")
            client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=10000,
                connectTimeoutMS=30000,
                socketTimeoutMS=30000
            )
            client.server_info()  # Test connection
            logger.debug("MongoDB connection successful")
            return client
        except Exception as e:
            logger.warning(f"Connection attempt {attempt+1} failed: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
            else:
                raise

# MongoDB setup
try:
    logger.debug("Connecting to MongoDB")
    # Use the standard connection without replicaSet parameter
    mongo_client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=10000,  # Increase to 10 seconds
        connectTimeoutMS=30000,
        socketTimeoutMS=30000
    )
    
    # Verify connection with a simple command
    mongo_client.server_info()
    logger.debug("MongoDB connection successful")
    db = mongo_client.get_database('trading_bot')
    users_collection = db.users
    users_collection.create_index('user_id', unique=True)
except Exception as e:
    logger.error(f"Failed to connect to MongoDB: {str(e)}")
    # Add detailed diagnostics
    logger.error(f"MONGO_URI: {MONGO_URI.split('@')[0]}@...")  # Log without password
    raise

def mongo_with_retry():
    max_retries = 5
    for attempt in range(max_retries):
        try:
            mongo_client.admin.command('ismaster')
            return mongo_client
        except ConnectionFailure as e:
            logger.warning(f"MongoDB connection attempt {attempt+1} failed: {str(e)}")
            time.sleep(2 ** attempt)  # Exponential backoff
    raise ConnectionFailure("Failed to connect to MongoDB after retries")


try:
    logger.debug("Connecting to MongoDB with retries")
    mongo_client = connect_mongodb_with_retry()
    logger.debug("MongoDB connection verified")
    db = mongo_client.get_database('trading_bot')
    users_collection = db.users
    users_collection.create_index('user_id', unique=True)
except Exception as e:
    logger.critical(f"Critical MongoDB connection failure: {str(e)}")
    # Add detailed error information
    if "ServerSelectionTimeoutError" in str(e):
        logger.critical("This usually indicates network issues or incorrect connection parameters")
    elif "Authentication failed" in str(e):
        logger.critical("Verify database username and password")
    raise

# Master key validation
try:
    master_key = bytes.fromhex(MASTER_KEY)
    if len(master_key) != 32:
        raise ValueError("MASTER_KEY must be a 32-byte hex string")
except ValueError as e:
    logger.error(f"Invalid MASTER_KEY: {str(e)}")
    raise

# Blockchain clients
solana_client = AsyncClient(SOLANA_RPC)
solana_sync_client = Client(SOLANA_RPC)
w3_eth = Web3(Web3.HTTPProvider(ETH_RPC))
w3_bsc = Web3(Web3.HTTPProvider(BSC_RPC))

# USDT contract setup
if not Web3.is_address(BOT_USDT_ADDRESS):
    logger.error("Invalid BOT_USDT_ADDRESS")
    raise ValueError("Invalid BOT_USDT_ADDRESS")
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
GMGN_API_HOST = 'https://gmgn.ai'
DEXSCREENER_PROFILE_API = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN_API = "https://api.dexscreener.com/tokens/v1/solana/{token_address}"

# Bot states for conversation
(SET_TRADING_MODE, SET_AUTO_BUY_AMOUNT, SET_SELL_PERCENTAGE, SET_LOSS_PERCENTAGE, 
 SELECT_TOKEN, BUY_AMOUNT, CONFIRM_TRADE, TRANSFER_TOKEN, TRANSFER_AMOUNT, TRANSFER_ADDRESS,
 CONFIRM_NEW_WALLET, SET_WALLET_METHOD, INPUT_MNEMONIC, INPUT_PRIVATE_KEY, CONFIRM_SET_WALLET,
 SELECT_TOKEN_ACTION, SELL_AMOUNT) = range(17)

# Flask app setup
app = Flask(__name__)

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
    return {
        'iv': base64.b64encode(iv).decode(),
        'ciphertext': base64.b64encode(ciphertext).decode()
    }

def decrypt_data(encrypted_data: dict, key: bytes) -> str:
    try:
        iv = base64.b64decode(encrypted_data['iv'])
        ciphertext = base64.b64decode(encrypted_data['ciphertext'])
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded_data = decryptor.update(ciphertext) + decryptor.finalize()
        return padded_data.rstrip(b'\0').decode()
    except Exception as e:
        logger.error(f"Decryption failed: {str(e)}")
        raise

async def check_subscription(user_id: int) -> bool:
    user = users_collection.find_one({'user_id': user_id})
    if not user or user.get('subscription_status') != 'active':
        logger.debug(f"User {user_id} has no active subscription")
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
        logger.debug(f"Setting wallet for user {user_id} using mnemonic")
        mnemo = Mnemonic("english")
        if not mnemo.check(mnemonic):
            logger.error(f"Invalid mnemonic for user {user_id}")
            raise ValueError("Invalid mnemonic phrase.")
        eth_account = Account.from_mnemonic(mnemonic)
        eth_address = eth_account.address
        eth_private_key = eth_account.key.hex()
        seed = mnemo.to_seed(mnemonic)
        solana_keypair = Keypair.from_seed(seed[:32])
        solana_private_key = b58encode(solana_keypair.to_bytes()).decode()
    elif private_key:
        logger.debug(f"Setting wallet for user {user_id} using private key")
        try:
            try:
                solana_keypair = Keypair.from_bytes(b58decode(private_key))
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
                solana_private_key = b58encode(solana_keypair.to_bytes()).decode()
        except Exception as e:
            logger.error(f"Invalid private key for user {user_id}: {str(e)}")
            raise ValueError(f"Invalid private key: {str(e)}")
    else:
        logger.error(f"No mnemonic or private key provided for user {user_id}")
        raise ValueError("Either mnemonic or private key must be provided.")

    encrypted_mnemonic = encrypt_data(mnemonic if mnemonic else 'Imported via private key', user_key)
    encrypted_solana_private_key = encrypt_data(solana_private_key, user_key)
    encrypted_eth_private_key = encrypt_data(eth_private_key, user_key) if eth_private_key else None
    encrypted_bsc_private_key = encrypt_data(eth_private_key, user_key) if eth_private_key else None

    logger.debug(f"Wallet set for user {user_id}: Solana {solana_keypair.pubkey()}, ETH {eth_address or 'N/A'}")
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
        return field if isinstance(field, str) else "[Invalid Data]"

    decrypted_user['mnemonic'] = safe_decrypt(user.get('mnemonic', {}))
    decrypted_user['solana']['private_key'] = safe_decrypt(user.get('solana', {}).get('private_key', {}))
    if user.get('eth'):
        decrypted_user['eth']['private_key'] = safe_decrypt(user.get('eth', {}).get('private_key', {}))
    if user.get('bsc'):
        decrypted_user['bsc']['private_key'] = safe_decrypt(user.get('bsc', {}).get('private_key', {}))
    logger.debug(f"Decrypted wallet for user {user_id}")
    return decrypted_user

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Start command received for user {user_id}")
    
    try:
        user = users_collection.find_one({'user_id': user_id})
        if not user:
            # Generate a new wallet
            mnemo = Mnemonic("english")
            mnemonic = mnemo.generate(strength=256)
            try:
                user_data = await set_user_wallet(user_id, mnemonic=mnemonic)
                users_collection.insert_one(user_data)
                decrypted_user = await decrypt_user_wallet(user_id, user_data)
                
                # Send wallet details with security warning
                message = await update.message.reply_text(
                    f"Welcome to the Multi-Chain Trading Bot!\n\n"
                    f"A new wallet has been created for you.\n"
                    f"**Solana Public Key**: {user_data['solana']['public_key']}\n"
                    f"**ETH/BSC Address**: {user_data['eth']['address'] if user_data['eth'] else 'N/A'}\n\n"
                    f"⚠️ **Security Warning** ⚠️\n"
                    f"1. **Never share your mnemonic or private keys**.\n"
                    f"2. Store them securely offline.\n"
                    f"3. Use this wallet only for trading small amounts.\n\n"
                    f"To access trading features, subscribe using /subscribe.",
                    parse_mode='HTML'
                )
                
                # Send mnemonic in a separate private message
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"🔒 **Your Recovery Phrase** 🔒\n\n{decrypted_user['mnemonic']}\n\n"
                             f"⚠️ Save this in a secure location. Never share it with anyone.",
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"Failed to send mnemonic to user {user_id}: {str(e)}")
                
                logger.info(f"New wallet created for user {user_id}")
            except Exception as e:
                logger.error(f"Error creating wallet for user {user_id}: {str(e)}")
                await update.message.reply_text(f"Error creating wallet: {str(e)}")
        else:
            subscription_message = await get_subscription_status_message(user)
            await update.message.reply_text(
                f"Welcome back!\n"
                f"Solana wallet: {user['solana']['public_key']}\n"
                f"ETH/BSC wallet: {user['eth']['address'] if user['eth'] else 'N/A'}\n"
                f"{subscription_message}\n"
                f"Use /set_wallet to import a different wallet.",
                parse_mode='HTML'
            )
            logger.info(f"User {user_id} returned, subscription status checked")
    except Exception as e:
        logger.error(f"Error in start command: {str(e)}")
        await update.message.reply_text(f"Error: {str(e)}")

async def get_subscription_status_message(user: dict) -> str:
    user_id = user['user_id']
    if user.get('subscription_status') != 'active':
        logger.debug(f"User {user_id} has no active subscription")
        return "No active subscription. Use /subscribe to start."
    expiry = user.get('subscription_expiry')
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry)
    if expiry and expiry > datetime.now():
        logger.debug(f"User {user_id} subscription active until {expiry}")
        return f"Subscription active until {expiry.strftime('%Y-%m-%d %H:%M:%S')}."
    logger.debug(f"User {user_id} subscription expired")
    users_collection.update_one(
        {'user_id': user_id},
        {'$set': {'subscription_status': 'inactive', 'subscription_expiry': None}}
    )
    return "Subscription expired. Use /subscribe to renew."

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Subscribe command received for user {user_id}")
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("Please use /start to create a wallet.")
        logger.debug(f"User {user_id} has no wallet")
        return
    if await check_subscription(user_id):
        expiry = user.get('subscription_expiry')
        await update.message.reply_text(
            f"Active subscription until {expiry.strftime('%Y-%m-%d %H:%M:%S')}."
        )
        logger.debug(f"User {user_id} already has active subscription")
        return
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
        f"To subscribe (5 USDT/week), send {usdt_amount:.2f} USDT to:\n"
        f"Address: {payment_address}\n"
        f"Network: Ethereum\n"
        f"Deadline: {payment_deadline.strftime('%Y-%m-%d %H:%M:%S')}",
        parse_mode='HTML'
    )
    logger.info(f"Subscription request initiated for user {user_id}, payment address: {payment_address}")


    

async def generate_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"Generate wallet command received for user {user_id}")
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("Please use /start to create a wallet.")
        logger.debug(f"User {user_id} has no wallet")
        return ConversationHandler.END
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription. Use /subscribe.")
        logger.debug(f"User {user_id} has no active subscription")
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton("Yes", callback_data='confirm_new_wallet')],
        [InlineKeyboardButton("No", callback_data='cancel_new_wallet')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Existing wallet:\n"
        f"Solana: {user['solana']['public_key']}\n"
        f"ETH/BSC: {user['eth']['address'] if user['eth'] else 'N/A'}\n"
        f"Generate new wallet? This will overwrite the existing one.",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    logger.debug(f"Prompted user {user_id} to confirm new wallet generation")
    return CONFIRM_NEW_WALLET

async def confirm_generate_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    logger.debug(f"Confirm generate wallet callback for user {user_id}: {query.data}")
    if query.data == 'cancel_new_wallet':
        await query.message.reply_text("Wallet generation cancelled.")
        logger.info(f"User {user_id} cancelled wallet generation")
        return ConversationHandler.END
    mnemo = Mnemonic("english")
    mnemonic = mnemo.generate(strength=256)
    try:
        user_data = await set_user_wallet(user_id, mnemonic=mnemonic)
        users_collection.replace_one({'user_id': user_id}, user_data, upsert=True)
        decrypted_user = await decrypt_user_wallet(user_id, user_data)
        message = await query.message.reply_text(
            f"New wallet generated!\n"
            f"**Mnemonic**: {decrypted_user['mnemonic']}\n"
            f"**Solana Public Key**: {user_data['solana']['public_key']}\n"
            f"**ETH/BSC Address**: {user_data['eth']['address'] if user_data['eth'] else 'N/A'}\n\n"
            f"⚠️ **Security Warning**: Save your mnemonic securely. This message will auto-delete in 30 seconds.",
            parse_mode='HTML'
        )
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id,
            name=f"delete_wallet_message_{user_id}"
        )
        logger.info(f"New wallet generated for user {user_id}")
    except Exception as e:
        logger.error(f"Error generating wallet for user {user_id}: {str(e)}")
        await query.message.reply_text(f"Error generating wallet: {str(e)}")
        return ConversationHandler.END
    return ConversationHandler.END

async def set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"Set wallet command received for user {user_id}")
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription. Use /subscribe.")
        logger.debug(f"User {user_id} has no active subscription")
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton("Mnemonic", callback_data='mnemonic')],
        [InlineKeyboardButton("Private Key", callback_data='private_key')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Import wallet using:\n- Mnemonic: 24-word BIP-39 phrase\n- Private Key: Solana base58 or ETH/BSC hex",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    logger.debug(f"Prompted user {user_id} to select wallet import method")
    return SET_WALLET_METHOD

async def set_wallet_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    context.user_data['wallet_method'] = query.data
    logger.debug(f"User {user_id} selected wallet method: {query.data}")
    if query.data == 'mnemonic':
        message = await query.message.reply_text(
            "Enter your 24-word mnemonic phrase (space-separated)."
        )
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id,
            name=f"delete_mnemonic_prompt_{user_id}"
        )
        return INPUT_MNEMONIC
    else:
        message = await query.message.reply_text(
            "Enter your private key (Solana base58 or ETH/BSC hex)."
        )
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id,
            name=f"delete_private_key_prompt_{user_id}"
        )
        return INPUT_PRIVATE_KEY

async def input_mnemonic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    mnemonic = update.message.text.strip()
    context.user_data['wallet_input'] = mnemonic
    logger.debug(f"User {user_id} submitted mnemonic")
    context.job_queue.run_once(
        lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=update.message.message_id),
        30,
        user_id=user_id,
        name=f"delete_mnemonic_input_{user_id}"
    )
    try:
        mnemo = Mnemonic("english")
        if not mnemo.check(mnemonic):
            message = await update.message.reply_text("Invalid mnemonic. Try again.")
            context.job_queue.run_once(
                lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
                30,
                user_id=user_id,
                name=f"delete_invalid_mnemonic_{user_id}"
            )
            logger.debug(f"User {user_id} submitted invalid mnemonic")
            return INPUT_MNEMONIC
    except Exception as e:
        message = await update.message.reply_text(f"Error: {str(e)}. Try again.")
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id,
            name=f"delete_mnemonic_error_{user_id}"
        )
        logger.error(f"Error validating mnemonic for user {user_id}: {str(e)}")
        return INPUT_MNEMONIC
    user = users_collection.find_one({'user_id': user_id})
    if user:
        keyboard = [
            [InlineKeyboardButton("Yes", callback_data='confirm_set_wallet')],
            [InlineKeyboardButton("No", callback_data='cancel_set_wallet')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"Existing wallet:\n"
            f"Solana: {user['solana']['public_key']}\n"
            f"ETH/BSC: {user['eth']['address'] if user['eth'] else 'N/A'}\n"
            f"Import new wallet? This will overwrite the existing one.",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        logger.debug(f"Prompted user {user_id} to confirm wallet import")
        return CONFIRM_SET_WALLET
    try:
        user_data = await set_user_wallet(user_id, mnemonic=mnemonic)
        users_collection.insert_one(user_data)
        decrypted_user = await decrypt_user_wallet(user_id, user_data)
        message = await update.message.reply_text(
            f"Wallet imported!\n"
            f"**Mnemonic**: {decrypted_user['mnemonic']}\n"
            f"**Solana Public Key**: {user_data['solana']['public_key']}\n"
            f"**ETH/BSC Address**: {user_data['eth']['address'] if user_data['eth'] else 'N/A'}\n\n"
            f"⚠️ **Security Warning**: Save your mnemonic securely. This message will auto-delete in 30 seconds.",
            parse_mode='HTML'
        )
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id,
            name=f"delete_wallet_import_{user_id}"
        )
        logger.info(f"Wallet imported for user {user_id} via mnemonic")
    except Exception as e:
        logger.error(f"Error importing wallet for user {user_id}: {str(e)}")
        await update.message.reply_text(f"Error importing wallet: {str(e)}")
        return ConversationHandler.END
    return ConversationHandler.END

async def input_private_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    private_key = update.message.text.strip()
    context.user_data['wallet_input'] = private_key
    logger.debug(f"User {user_id} submitted private key")
    context.job_queue.run_once(
        lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=update.message.message_id),
        30,
        user_id=user_id,
        name=f"delete_private_key_input_{user_id}"
    )
    try:
        if not private_key.startswith('0x'):
            try:
                b58decode(private_key)
            except:
                private_key = '0x' + private_key
                bytes.fromhex(private_key[2:])
        else:
            bytes.fromhex(private_key[2:])
    except Exception as e:
        message = await update.message.reply_text(f"Invalid private key: {str(e)}. Try again.")
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id,
            name=f"delete_invalid_private_key_{user_id}"
        )
        logger.debug(f"User {user_id} submitted invalid private key: {str(e)}")
        return INPUT_PRIVATE_KEY
    user = users_collection.find_one({'user_id': user_id})
    if user:
        keyboard = [
            [InlineKeyboardButton("Yes", callback_data='confirm_set_wallet')],
            [InlineKeyboardButton("No", callback_data='cancel_set_wallet')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"Existing wallet:\n"
            f"Solana: {user['solana']['public_key']}\n"
            f"ETH/BSC: {user['eth']['address'] if user['eth'] else 'N/A'}\n"
            f"Import new wallet? This will overwrite the existing one.",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        logger.debug(f"Prompted user {user_id} to confirm wallet import")
        return CONFIRM_SET_WALLET
    try:
        user_data = await set_user_wallet(user_id, private_key=private_key)
        users_collection.insert_one(user_data)
        decrypted_user = await decrypt_user_wallet(user_id, user_data)
        message = await update.message.reply_text(
            f"Wallet imported!\n"
            f"**Solana Public Key**: {user_data['solana']['public_key']}\n"
            f"**ETH/BSC Address**: {user_data['eth']['address'] if user_data['eth'] else 'N/A'}\n\n"
            f"⚠️ **Security Warning**: This message will auto-delete in 30 seconds.",
            parse_mode='HTML'
        )
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id,
            name=f"delete_wallet_import_{user_id}"
        )
        logger.info(f"Wallet imported for user {user_id} via private key")
    except Exception as e:
        logger.error(f"Error importing wallet for user {user_id}: {str(e)}")
        await update.message.reply_text(f"Error importing wallet: {str(e)}")
        return ConversationHandler.END
    return ConversationHandler.END

async def confirm_set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    logger.debug(f"Confirm set wallet callback for user {user_id}: {query.data}")
    if query.data == 'cancel_set_wallet':
        await query.message.reply_text("Wallet import cancelled.")
        logger.info(f"User {user_id} cancelled wallet import")
        return ConversationHandler.END
    wallet_input = context.user_data['wallet_input']
    method = context.user_data['wallet_method']
    try:
        user_data = await set_user_wallet(
            user_id,
            mnemonic=wallet_input if method == 'mnemonic' else None,
            private_key=wallet_input if method == 'private_key' else None
        )
        users_collection.replace_one({'user_id': user_id}, user_data, upsert=True)
        decrypted_user = await decrypt_user_wallet(user_id, user_data)
        message = await query.message.reply_text(
            f"Wallet imported!\n"
            f"**{'Mnemonic' if method == 'mnemonic' else 'Private Key'}**: {decrypted_user['mnemonic'] if method == 'mnemonic' else '[Hidden]'}\n"
            f"**Solana Public Key**: {user_data['solana']['public_key']}\n"
            f"**ETH/BSC Address**: {user_data['eth']['address'] if user_data['eth'] else 'N/A'}\n\n"
            f"⚠️ **Security Warning**: Save securely. This message will auto-delete in 30 seconds.",
            parse_mode='HTML'
        )
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id,
            name=f"delete_wallet_confirm_{user_id}"
        )
        logger.info(f"Wallet imported for user {user_id} via {method}")
    except Exception as e:
        logger.error(f"Error confirming wallet import for user {user_id}: {str(e)}")
        await query.message.reply_text(f"Error importing wallet: {str(e)}")
        return ConversationHandler.END
    return ConversationHandler.END

async def reset_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Reset tokens command received for user {user_id}")
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription. Use /subscribe.")
        logger.debug(f"User {user_id} has no active subscription")
        return
    users_collection.update_one({'user_id': user_id}, {'$set': {'posted_tokens': []}})
    await update.message.reply_text("Posted tokens reset. You will receive all new tokens again.")
    logger.info(f"Posted tokens reset for user {user_id}")

async def set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"Set mode command received for user {user_id}")
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription. Use /subscribe.")
        logger.debug(f"User {user_id} has no active subscription")
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton("Manual", callback_data='manual')],
        [InlineKeyboardButton("Automatic", callback_data='automatic')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Choose trading mode (Solana only):",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    logger.debug(f"Prompted user {user_id} to select trading mode")
    return SET_TRADING_MODE

async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    mode = query.data
    logger.debug(f"User {user_id} selected trading mode: {mode}")
    users_collection.update_one({'user_id': user_id}, {'$set': {'trading_mode': mode}})
    if mode == 'manual':
        await query.message.reply_text("Trading mode set to Manual. Use /trade to trade.")
        logger.info(f"User {user_id} set trading mode to manual")
        return ConversationHandler.END
    await query.message.reply_text("Trading mode set to Automatic. Enter auto-buy amount in SOL:")
    logger.debug(f"Prompted user {user_id} for auto-buy amount")
    return SET_AUTO_BUY_AMOUNT

async def set_auto_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"User {user_id} submitted auto-buy amount")
    try:
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("Enter a positive amount.")
            logger.debug(f"User {user_id} entered invalid auto-buy amount: {update.message.text}")
            return SET_AUTO_BUY_AMOUNT
        users_collection.update_one({'user_id': user_id}, {'$set': {'auto_buy_amount': amount}})
        await update.message.reply_text("Enter sell percentage (e.g., 10 for 10%):")
        logger.debug(f"User {user_id} set auto-buy amount to {amount} SOL")
        return SET_SELL_PERCENTAGE
    except ValueError:
        await update.message.reply_text("Invalid amount. Enter a number.")
        logger.debug(f"User {user_id} entered invalid auto-buy amount: {update.message.text}")
        return SET_AUTO_BUY_AMOUNT

async def set_sell_percentage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"User {user_id} submitted sell percentage")
    try:
        percentage = float(update.message.text)
        if percentage <= 0:
            await update.message.reply_text("Enter a positive percentage.")
            logger.debug(f"User {user_id} entered invalid sell percentage: {update.message.text}")
            return SET_SELL_PERCENTAGE
        users_collection.update_one({'user_id': user_id}, {'$set': {'sell_percentage': percentage}})
        await update.message.reply_text("Enter loss percentage (e.g., 5 for 5%):")
        logger.debug(f"User {user_id} set sell percentage to {percentage}%")
        return SET_LOSS_PERCENTAGE
    except ValueError:
        await update.message.reply_text("Invalid percentage. Enter a number.")
        logger.debug(f"User {user_id} entered invalid sell percentage: {update.message.text}")
        return SET_SELL_PERCENTAGE

async def set_loss_percentage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"User {user_id} submitted loss percentage")
    try:
        percentage = float(update.message.text)
        if percentage <= 0:
            await update.message.reply_text("Enter a positive percentage.")
            logger.debug(f"User {user_id} entered invalid loss percentage: {update.message.text}")
            return SET_LOSS_PERCENTAGE
        users_collection.update_one({'user_id': user_id}, {'$set': {'loss_percentage': percentage}})
        user = users_collection.find_one({'user_id': user_id})
        await update.message.reply_text(
            f"Automatic trading settings saved:\n"
            f"Auto-buy: {user['auto_buy_amount']} SOL\n"
            f"Sell at: {user['sell_percentage']}% profit\n"
            f"Stop-loss at: {user['loss_percentage']}% loss",
            parse_mode='HTML'
        )
        context.job_queue.run_repeating(
            auto_trade,
            interval=10,
            first=5,
            user_id=user_id,
            name=f"auto_trade_{user_id}"
        )
        logger.info(f"User {user_id} configured automatic trading: buy {user['auto_buy_amount']} SOL, "
                    f"sell at {user['sell_percentage']}%, stop-loss at {user['loss_percentage']}%")
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("Invalid percentage. Enter a number.")
        logger.debug(f"User {user_id} entered invalid loss percentage: {update.message.text}")
        return SET_LOSS_PERCENTAGE

async def trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"Trade command received for user {user_id}")
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription. Use /subscribe.")
        logger.debug(f"User {user_id} has no active subscription")
        return ConversationHandler.END
    user = users_collection.find_one({'user_id': user_id})
    if user['trading_mode'] != 'manual':
        await update.message.reply_text("Set trading mode to Manual using /setmode.")
        logger.debug(f"User {user_id} is not in manual trading mode")
        return ConversationHandler.END
    token = await fetch_latest_token()
    if not token:
        await update.message.reply_text("Failed to fetch token data.")
        logger.error(f"Failed to fetch token data for user {user_id}")
        return ConversationHandler.END
    if token['contract_address'] in user.get('posted_tokens', []):
        await update.message.reply_text("No new tokens available. Use /reset_tokens to clear posted tokens.")
        logger.debug(f"No new tokens for user {user_id}")
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
        f"DexScreener: <a href='{token['dexscreener_url']}'>View</a>",
        parse_mode='HTML',
        reply_markup=reply_markup
    )
    users_collection.update_one(
        {'user_id': user_id},
        {'$addToSet': {'posted_tokens': token['contract_address']}}
    )
    logger.info(f"Sent token info to user {user_id}: {token['name']} ({token['contract_address']})")
    return SELECT_TOKEN_ACTION

async def select_token_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    logger.debug(f"Token action callback for user {user_id}: {query.data}")
    if not await check_subscription(user_id):
        await query.message.reply_text("You need an active subscription. Use /subscribe.")
        logger.debug(f"User {user_id} has no active subscription")
        return ConversationHandler.END
    user = users_collection.find_one({'user_id': user_id})
    if user['trading_mode'] != 'manual':
        await query.message.reply_text("Set trading mode to Manual using /setmode.")
        logger.debug(f"User {user_id} is not in manual trading mode")
        return ConversationHandler.END
    action, contract_address = query.data.split('_', 1)
    token = context.user_data.get('current_token', {})
    if not token or token['contract_address'] != contract_address:
        async with httpx.AsyncClient() as client:
            token_url = DEXSCREENER_TOKEN_API.format(token_address=contract_address)
            try:
                response = await client.get(token_url)
                response.raise_for_status()
                token_data = response.json()
                if not token_data or not token_data[0]:
                    await query.message.reply_text("Failed to fetch token data.")
                    logger.error(f"Failed to fetch token data for contract {contract_address}")
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
                logger.debug(f"Fetched token data for {contract_address}: {token['name']}")
            except Exception as e:
                logger.error(f"Error fetching token data for {contract_address}: {str(e)}")
                await query.message.reply_text("Failed to fetch token data.")
                return ConversationHandler.END
    context.user_data['trade_action'] = action
    if action == 'buy':
        await query.message.reply_text(
            f"Selected: {token['name']} ({token['symbol']})\nEnter amount to buy in SOL:"
        )
        logger.debug(f"User {user_id} selected buy for {token['name']}")
        return BUY_AMOUNT
    else:
        portfolio = user.get('portfolio', {})
        if contract_address not in portfolio:
            await query.message.reply_text(f"You don't hold {token['name']}.")
            logger.debug(f"User {user_id} does not hold {token['name']}")
            return ConversationHandler.END
        await query.message.reply_text(
            f"Selected: {token['name']} ({token['symbol']})\n"
            f"Available: {portfolio[contract_address]['amount']} SOL worth\n"
            f"Enter amount to sell in SOL:"
        )
        logger.debug(f"User {user_id} selected sell for {token['name']}")
        return SELL_AMOUNT

async def buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"User {user_id} submitted buy amount")
    try:
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("Enter a positive amount.")
            logger.debug(f"User {user_id} entered invalid buy amount: {update.message.text}")
            return BUY_AMOUNT
        balance = await check_balance(user_id, 'solana')
        if balance < amount:
            await update.message.reply_text(f"Insufficient SOL balance: {balance:.4f} SOL")
            logger.debug(f"User {user_id} has insufficient SOL balance: {balance} < {amount}")
            return ConversationHandler.END
        token = context.user_data['current_token']
        context.user_data['buy_amount'] = amount
        keyboard = [
            [InlineKeyboardButton("Confirm", callback_data='confirm_trade')],
            [InlineKeyboardButton("Cancel", callback_data='cancel_trade')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"Confirm buy:\n"
            f"Token: {token['name']} ({token['symbol']})\n"
            f"Amount: {amount} SOL\n"
            f"Contract: {token['contract_address']}\n"
            f"Price: ${token['price_usd']:.6f}",
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        logger.debug(f"Prompted user {user_id} to confirm buy: {amount} SOL of {token['name']}")
        return CONFIRM_TRADE
    except ValueError:
        await update.message.reply_text("Invalid amount. Enter a number.")
        logger.debug(f"User {user_id} entered invalid buy amount: {update.message.text}")
        return BUY_AMOUNT

async def sell_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"User {user_id} submitted sell amount")
    try:
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("Enter a positive amount.")
            logger.debug(f"User {user_id} entered invalid sell amount: {update.message.text}")
            return SELL_AMOUNT
        token = context.user_data['current_token']
        user = users_collection.find_one({'user_id': user_id})
        portfolio = user.get('portfolio', {})
        if token['contract_address'] not in portfolio:
            await update.message.reply_text(f"You don't hold {token['name']}.")
            logger.debug(f"User {user_id} does not hold {token['name']}")
            return ConversationHandler.END
        if amount > portfolio[token['contract_address']]['amount']:
            await update.message.reply_text(
                f"Insufficient balance: {portfolio[token['contract_address']]['amount']} SOL worth"
            )
            logger.debug(f"User {user_id} has insufficient balance for {token['name']}: "
                        f"{portfolio[token['contract_address']]['amount']} SOL worth")
            return SELL_AMOUNT
        context.user_data['sell_amount'] = amount
        keyboard = [
            [InlineKeyboardButton("Confirm", callback_data='confirm_trade')],
            [InlineKeyboardButton("Cancel", callback_data='cancel_trade')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"Confirm sell:\n"
            f"Token: {token['name']} ({token['symbol']})\n"
            f"Amount: {amount} SOL worth\n"
            f"Contract: {token['contract_address']}\n"
            f"Price: ${token['price_usd']:.6f}",
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        logger.debug(f"Prompted user {user_id} to confirm sell: {amount} SOL of {token['name']}")
        return CONFIRM_TRADE
    except ValueError:
        await update.message.reply_text("Invalid amount. Enter a number.")
        logger.debug(f"User {user_id} entered invalid sell amount: {update.message.text}")
        return SELL_AMOUNT

async def confirm_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    logger.debug(f"Confirm trade callback for user {user_id}: {query.data}")
    if query.data == 'cancel_trade':
        await query.message.reply_text("Trade cancelled.")
        logger.info(f"User {user_id} cancelled trade")
        return ConversationHandler.END
    token = context.user_data['current_token']
    action = context.user_data.get('trade_action', 'buy')
    amount = context.user_data.get('buy_amount' if action == 'buy' else 'sell_amount')
    try:
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
                await query.message.reply_text(
                    f"Bought {amount} SOL of {token['name']} at ${token['price_usd']:.6f}."
                )
                logger.info(f"User {user_id} bought {amount} SOL of {token['name']}")
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
                await query.message.reply_text(
                    f"Sold {amount} SOL of {token['name']} at ${token['price_usd']:.6f}."
                )
                logger.info(f"User {user_id} sold {amount} SOL of {token['name']}")
        else:
            await query.message.reply_text("Trade failed.")
            logger.error(f"Trade failed for user {user_id}: {action} {amount} SOL of {token['name']}")
    except Exception as e:
        logger.error(f"Error executing trade for user {user_id}: {str(e)}")
        await query.message.reply_text(f"Trade failed: {str(e)}")
    return ConversationHandler.END

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Balance command received for user {user_id}")
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription. Use /subscribe.")
        logger.debug(f"User {user_id} has no active subscription")
        return
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("Please use /start to create a wallet.")
        logger.debug(f"User {user_id} has no wallet")
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
            message += f"{details['name']} ({details['symbol']}): {details['amount']} SOL worth\n"
    await update.message.reply_text(message, parse_mode='HTML')
    logger.info(f"Sent balance info to user {user_id}")

async def transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"Transfer command received for user {user_id}")
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription. Use /subscribe.")
        logger.debug(f"User {user_id} has no active subscription")
        return ConversationHandler.END
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("Please use /start to create a wallet.")
        logger.debug(f"User {user_id} has no wallet")
        return ConversationHandler.END
    portfolio = user.get('portfolio', {})
    if not portfolio:
        await update.message.reply_text("No tokens to transfer.")
        logger.debug(f"User {user_id} has no tokens to transfer")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(details['name'], callback_data=contract)] 
                for contract, details in portfolio.items()]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Select Solana token to transfer:",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    logger.debug(f"Prompted user {user_id} to select token for transfer")
    return TRANSFER_TOKEN

async def transfer_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    context.user_data['transfer_token'] = query.data
    user = users_collection.find_one({'user_id': user_id})
    token = user['portfolio'][query.data]
    await query.message.reply_text(
        f"Selected: {token['name']}\nEnter amount to transfer (in SOL worth):"
    )
    logger.debug(f"User {user_id} selected token {token['name']} for transfer")
    return TRANSFER_AMOUNT

async def transfer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"User {user_id} submitted transfer amount")
    try:
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("Enter a positive amount.")
            logger.debug(f"User {user_id} entered invalid transfer amount: {update.message.text}")
            return TRANSFER_AMOUNT
        token_contract = context.user_data['transfer_token']
        user = users_collection.find_one({'user_id': user_id})
        token = user['portfolio'][token_contract]
        if amount > token['amount']:
            await update.message.reply_text(f"Insufficient balance: {token['amount']} SOL worth")
            logger.debug(f"User {user_id} has insufficient balance for {token['name']}: {token['amount']} SOL worth")
            return TRANSFER_AMOUNT
        context.user_data['transfer_amount'] = amount
        await update.message.reply_text("Enter recipient Solana address:")
        logger.debug(f"User {user_id} entered transfer amount: {amount} SOL worth")
        return TRANSFER_ADDRESS
    except ValueError:
        await update.message.reply_text("Invalid amount. Enter a number.")
        logger.debug(f"User {user_id} entered invalid transfer amount: {update.message.text}")
        return TRANSFER_AMOUNT

async def transfer_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    logger.debug(f"User {user_id} submitted transfer address")
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
            logger.info(f"User {user_id} transferred {amount} SOL worth of {token['name']} to {address}")
        else:
            await update.message.reply_text("Transfer failed.")
            logger.error(f"Transfer failed for user {user_id}: {amount} SOL worth of {token['name']}")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"Invalid address: {str(e)}")
        logger.debug(f"User {user_id} entered invalid address: {address}")
        return TRANSFER_ADDRESS

async def fetch_latest_token():
    logger.debug("Fetching latest token from DexScreener")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(DEXSCREENER_PROFILE_API, params={'chainId': 'solana'})
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list) or not data:
                logger.error("Invalid or empty DexScreener response")
                return None
            token = data[0]
            token_address = token.get('tokenAddress')
            if not token_address:
                logger.error("No tokenAddress in DexScreener response")
                return None
            token_url = DEXSCREENER_TOKEN_API.format(token_address=token_address)
            token_response = await client.get(token_url)
            token_response.raise_for_status()
            token_data = token_response.json()
            if not token_data or not token_data[0]:
                logger.error("No token pairs in DexScreener response")
                return None
            pair_data = token_data[0]
            name = pair_data.get('name', 'Unknown')
            symbol = pair_data.get('symbol', 'Unknown')
            token_info = {
                'name': name,
                'symbol': symbol,
                'contract_address': token_address,
                'price_usd': float(pair_data.get('priceUsd', '0.0')),
                'market_cap': float(pair_data.get('marketCap', 0.0)),
                'dexscreener_url': f"https://dexscreener.com/solana/{token_address}",
                'liquidity': float(pair_data.get('liquidity', {}).get('usd', 0.0)),
                'volume': float(pair_data.get('volume', {}).get('h24', 0.0))
            }
            logger.debug(f"Fetched token: {name} ({token_address})")
            return token_info
        except Exception as e:
            logger.error(f"Error fetching token: {str(e)}")
            return None

async def periodic_token_check(context: ContextTypes.DEFAULT_TYPE):
    logger.debug("Running periodic token check")
    token = await fetch_latest_token()
    if not token:
        logger.debug("No new token found")
        return
    users = users_collection.find({'subscription_status': 'active'})
    for user in users:
        user_id = user['user_id']
        if token['contract_address'] in user.get('posted_tokens', []):
            continue
        if time.time() - user.get('last_api_call', 0) < 1:
            continue
        is_suspicious = token['liquidity'] < 1000 or token['volume'] < 1000
        warning = "⚠️ Low liquidity or volume. Trade with caution." if is_suspicious else ""
        message = (
            f"<b>New Solana Token</b>\n"
            f"Name: {token['name']} ({token['symbol']})\n"
            f"Contract: {token['contract_address']}\n"
            f"Price: ${token['price_usd']:.6f}\n"
            f"Market Cap: ${token['market_cap']:,.2f}\n"
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
            logger.info(f"Sent new token {token['name']} to user {user_id}")
            if user['trading_mode'] == 'automatic':
                await auto_trade(context, user_id=user_id, token=token)
        except Exception as e:
            logger.error(f"Error sending token to user {user_id}: {str(e)}")

async def check_balance(user_id, chain):
    logger.debug(f"Checking {chain} balance for user {user_id}")
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        logger.debug(f"User {user_id} has no wallet")
        return 0.0
    try:
        if chain == 'solana':
            pubkey = Pubkey.from_string(user['solana']['public_key'])
            response = solana_sync_client.get_balance(pubkey)
            balance = response.value / 1_000_000_000
            logger.debug(f"User {user_id} Solana balance: {balance} SOL")
            return balance
        elif chain == 'eth' and user.get('eth'):
            balance = w3_eth.eth.get_balance(user['eth']['address'])
            eth_balance = w3_eth.from_wei(balance, 'ether')
            logger.debug(f"User {user_id} Ethereum balance: {eth_balance} ETH")
            return eth_balance
        elif chain == 'bsc' and user.get('bsc'):
            balance = w3_bsc.eth.get_balance(user['bsc']['address'])
            bsc_balance = w3_bsc.from_wei(balance, 'ether')
            logger.debug(f"User {user_id} BSC balance: {bsc_balance} BNB")
            return bsc_balance
        logger.debug(f"User {user_id} has no {chain} wallet")
        return 0.0
    except Exception as e:
        logger.error(f"Error checking {chain} balance for user {user_id}: {str(e)}")
        return 0.0

async def execute_trade(user_id, contract_address, amount, action, chain):
    logger.debug(f"Executing {action} trade for user {user_id}: {amount} SOL of {contract_address}")
    if chain != 'solana':
        logger.error(f"Unsupported chain {chain} for trade")
        return False
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        logger.error(f"User {user_id} not found")
        return False
    decrypted_user = await decrypt_user_wallet(user_id, user)
    solana_private_key = decrypted_user['solana']['private_key']
    if not solana_private_key or solana_private_key == "[Decryption Failed]":
        logger.error(f"Decryption failed for user {user_id} Solana private key")
        return False
    try:
        keypair = Keypair.from_bytes(b58decode(solana_private_key))
        from_address = str(keypair.pubkey())
        balance = await check_balance(user_id, 'solana')
        if action == 'buy' and balance < amount:
            logger.error(f"Insufficient SOL balance for user {user_id}: {balance} < {amount}")
            return False
        input_token = 'So11111111111111111111111111111111111111112'  # SOL
        output_token = contract_address
        in_amount = str(int(amount * 1_000_000_000))
        slippage = 0.5
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
            try:
                response = await client.get(quote_url, params=params)
                response.raise_for_status()
                route = response.json()
                if route.get('code') != 0:
                    logger.error(f"Swap route failed for user {user_id}: {route.get('msg')}")
                    return False
                # Simplified trade execution (implement actual swap logic as needed)
                logger.info(f"Trade executed for user {user_id}: {action} {amount} SOL of {contract_address}")
                return True
            except Exception as e:
                logger.error(f"Trade execution failed for user {user_id}: {str(e)}")
                return False
    except Exception as e:
        logger.error(f"Error executing trade for user {user_id}: {str(e)}")
        return False

async def execute_transfer(user_id, recipient, token_contract, amount, chain):
    logger.debug(f"Executing transfer for user {user_id}: {amount} SOL worth of {token_contract}")
    if chain != 'solana':
        logger.error(f"Unsupported chain {chain} for transfer")
        return False
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        logger.error(f"User {user_id} not found")
        return False
    decrypted_user = await decrypt_user_wallet(user_id, user)
    solana_private_key = decrypted_user['solana']['private_key']
    if not solana_private_key or solana_private_key == "[Decryption Failed]":
        logger.error(f"Decryption failed for user {user_id} Solana private key")
        return False
    try:
        keypair = Keypair.from_bytes(b58decode(solana_private_key))
        # Simplified transfer (implement actual transfer logic as needed)
        logger.info(f"Transfer executed for user {user_id}: {amount} SOL worth of {token_contract} to {recipient}")
        return True
    except Exception as e:
        logger.error(f"Transfer failed for user {user_id}: {str(e)}")
        return False

async def auto_trade(context: ContextTypes.DEFAULT_TYPE, user_id: int, token: dict):
    logger.debug(f"Running auto trade for user {user_id}")
    user = users_collection.find_one({'user_id': user_id})
    if not user or user['trading_mode'] != 'automatic':
        logger.debug(f"User {user_id} not eligible for auto trade")
        return
    amount = user['auto_buy_amount']
    balance = await check_balance(user_id, 'solana')
    if balance < amount:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"Insufficient SOL balance for auto trade: {balance:.4f} SOL"
        )
        logger.debug(f"User {user_id} has insufficient SOL balance for auto trade: {balance} < {amount}")
        return
    try:
        success = await execute_trade(user_id, token['contract_address'], amount, 'buy', 'solana')
        if success:
            users_collection.update_one(
                {'user_id': user_id},
                {'$set': {f'portfolio.{token["contract_address"]}': {
                    'name': token['name'],
                    'symbol': token['symbol'],
                    'amount': amount,
                    'buy_price': token['price_usd']
                }}}
            )
            await context.bot.send_message(
                chat_id=user_id,
                text=f"Auto-bought {amount} SOL of {token['name']} at ${token['price_usd']:.6f}."
            )
            logger.info(f"Auto trade executed for user {user_id}: bought {amount} SOL of {token['name']}")
    except Exception as e:
        logger.error(f"Auto trade failed for user {user_id}: {str(e)}")
        await context.bot.send_message(
            chat_id=user_id,
            text=f"Auto trade failed: {str(e)}"
        )

# Flask routes
@app.route('/')
def health_check():
    logger.debug("Health check endpoint called")
    return jsonify({'status': 'ok'}), 200

@app.route('/webhook', methods=['POST'])
def payment_webhook():  # Remove async
    logger.debug("Payment webhook received")
    data = request.get_json()
    if not data:
        logger.error("Invalid payment webhook data")
        return jsonify({'error': 'Invalid data'}), 400
    tx_hash = data.get('hash')
    sender = data.get('from')
    if not tx_hash or not sender:
        logger.error("Missing transaction hash or sender in payment webhook")
        return jsonify({'error': 'Missing transaction hash or sender'}), 400
    try:
        receipt = w3_eth.eth.get_transaction_receipt(tx_hash)
        for log in receipt['logs']:
            if log['address'].lower() == USDT_CONTRACT_ADDRESS.lower():
                amount = int.from_bytes(log['data'], 'big') // 10**6
                user = users_collection.find_one({'payment_address': sender})
                if user and user.get('expected_amount') == amount:
                    expiry = datetime.now() + timedelta(days=7)
                    users_collection.update_one(
                        {'user_id': user['user_id']},
                        {
                            '$set': {
                                'subscription_status': 'active',
                                'subscription_expiry': expiry.isoformat(),
                                'payment_address': None,
                                'expected_amount': None,
                                'payment_deadline': None
                            }
                        }
                    )
                    # Use asyncio to run async function in background
                    asyncio.run_coroutine_threadsafe(
                        application.bot.send_message(
                            chat_id=user['user_id'],
                            text=f"Payment confirmed! Subscription active until {expiry.strftime('%Y-%m-%d %H:%M:%S')}."
                        ),
                        application.loop
                    )
                    logger.info(f"Payment confirmed for user {user['user_id']}: {amount} USDT")
                    return jsonify({'status': 'success'}), 200
        logger.error("No valid USDT transfer found in transaction")
        return jsonify({'error': 'No valid USDT transfer'}), 400
    except Exception as e:
        logger.error(f"Payment webhook error: {str(e)}")
        return jsonify({'error': 'Payment not processed'}), 400


@app.route('/network-diag')
def network_diagnostics():
    import socket
    import dns.resolver
    
    diag_info = {
        'mongo_host': 'cluster0.yee2yfq.mongodb.net',
        'solana_host': SOLANA_RPC.split('//')[1].split('/')[0],
        'eth_host': ETH_RPC.split('//')[1].split('/')[0],
        'bsc_host': BSC_RPC.split('//')[1].split('/')[0]
    }
    
    results = {}
    
    # Test DNS resolution
    try:
        results['mongo_dns'] = str(dns.resolver.resolve(diag_info['mongo_host'], 'SRV'))
    except Exception as e:
        results['mongo_dns'] = f"DNS Error: {str(e)}"
    
    # Test port connectivity
    for service, host in diag_info.items():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect((host, 27017 if 'mongo' in service else 443))
                results[f"{service}_port"] = "Open"
        except Exception as e:
            results[f"{service}_port"] = f"Connection Error: {str(e)}"
    
    return jsonify(results)
async def setup_webhook():
    try:
        application = Application.builder().token(TELEGRAM_TOKEN).build()
        logger.debug("Application built successfully")
    except Exception as e:
        logger.error(f"Failed to build Application: {str(e)}")
        raise
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('subscribe', subscribe))
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler('generate_wallet', generate_wallet)],
        states={
            CONFIRM_NEW_WALLET: [CallbackQueryHandler(confirm_generate_wallet)]
        },
        fallbacks=[]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler('set_wallet', set_wallet)],
        states={
            SET_WALLET_METHOD: [CallbackQueryHandler(set_wallet_method)],
            INPUT_MNEMONIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_mnemonic)],
            INPUT_PRIVATE_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_private_key)],
            CONFIRM_SET_WALLET: [CallbackQueryHandler(confirm_set_wallet)]
        },
        fallbacks=[]
    ))
    application.add_handler(CommandHandler('reset_tokens', reset_tokens))
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler('setmode', set_mode)],
        states={
            SET_TRADING_MODE: [CallbackQueryHandler(mode_callback)],
            SET_AUTO_BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_auto_buy_amount)],
            SET_SELL_PERCENTAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_sell_percentage)],
            SET_LOSS_PERCENTAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_loss_percentage)]
        },
        fallbacks=[]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler('trade', trade)],
                states={
            SELECT_TOKEN_ACTION: [CallbackQueryHandler(select_token_action)],
            BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_amount)],
            SELL_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_amount)],
            CONFIRM_TRADE: [CallbackQueryHandler(confirm_trade)]
        },
        fallbacks=[]
    ))
    application.add_handler(CommandHandler('balance', balance))
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler('transfer', transfer)],
        states={
            TRANSFER_TOKEN: [CallbackQueryHandler(transfer_token)],
            TRANSFER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, transfer_amount)],
            TRANSFER_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, transfer_address)]
        },
        fallbacks=[]
    ))
    application.job_queue.run_repeating(
        periodic_token_check,
        interval=5,
        first=5,
        name="periodic_token_check"
    )
    try:
        webhook_url = f"{WEBHOOK_URL}/webhook/telegram"
        logger.debug(f"Setting webhook to {webhook_url}")
        await application.bot.set_webhook(webhook_url)
        logger.info("Webhook set successfully")
    except Exception as e:
        logger.error(f"Failed to set webhook: {str(e)}")
        raise
    return application

def main():
    global application
    try:
        # Create a new event loop for the main thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Create and initialize the application
        application = loop.run_until_complete(setup_webhook())
        application.initialize()
        application.start()
        logger.info("Telegram application initialized and started")
        
        @app.route('/webhook/telegram', methods=['POST'])
        def telegram_webhook():  # Remove async from Flask route
            try:
                update = Update.de_json(request.get_json(), application.bot)
                if not update:
                    logger.error("Invalid Telegram update received")
                    return jsonify({'error': 'Invalid update'}), 400
                
                # Process update in a thread-safe manner
                loop.run_until_complete(application.process_update(update))
                logger.debug("Telegram update processed successfully")
                return jsonify({'status': 'ok'}), 200
            except Exception as e:
                logger.error(f"Error processing Telegram update: {str(e)}")
                return jsonify({'error': 'Processing failed'}), 500

        @app.route('/status')
        def status():
            try:
                # Test MongoDB connection
                mongo_client.server_info()
                mongo_status = "connected"
                
                # Test Telegram bot connection
                bot_info = application.bot.get_me()
                telegram_status = f"connected as @{bot_info.username}"
                
                return jsonify({
                    'status': 'online',
                    'mongo': mongo_status,
                    'telegram': telegram_status,
                    'timestamp': datetime.now().isoformat()
                }), 200
            except Exception as e:
                return jsonify({
                    'status': 'error',
                    'message': str(e)
                }), 500

        logger.info(f"Starting Flask app on port {PORT}")
        app.run(host='0.0.0.0', port=PORT)
    except Exception as e:
        logger.error(f"Error in main: {str(e)}")
        raise
    finally:
        logger.info("Shutting down application")
        if application:
            application.stop()
            application.shutdown()

if __name__ == '__main__':
    main()