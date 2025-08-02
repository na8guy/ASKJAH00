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
    WebhookServer,
)
from solana.rpc.async_api import AsyncClient
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction
from base58 import b58encode
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
    logger.error("Missing required environment variables")
    raise ValueError("Missing required environment variables")

# MongoDB setup
try:
    logger.debug("Connecting to MongoDB")
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    logger.debug("MongoDB connection successful")
    db = mongo_client.get_database('trading_bot')
    users_collection = db.users
    users_collection.create_index('user_id', unique=True)
except ConnectionFailure as e:
    logger.error(f"Failed to connect to MongoDB: {str(e)}")
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
    iv = base64.b64decode(encrypted_data['iv'])
    ciphertext = base64.b64decode(encrypted_data['ciphertext'])
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded_data = decryptor.update(ciphertext) + decryptor.finalize()
    return padded_data.rstrip(b'\0').decode()

async def check_subscription(user_id: int) -> bool:
    user = users_collection.find_one({'user_id': user_id})
    if not user or user.get('subscription_status') != 'active':
        return False
    expiry = user.get('subscription_expiry')
    if not expiry:
        return False
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry)
    if datetime.now() >= expiry:
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {'subscription_status': 'inactive', 'subscription_expiry': None}}
        )
        return False
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
        solana_private_key = b58encode(solana_keypair.to_bytes()).decode()
    elif private_key:
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
            except:
                return "[Decryption Failed]"
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
            message = await update.message.reply_text(
                f"Welcome to the Multi-Chain Trading Bot!\n\n"
                f"A new wallet has been created for you.\n"
                f"**Mnemonic**: {decrypted_user['mnemonic']}\n"
                f"**Solana Public Key**: {user_data['solana']['public_key']}\n"
                f"**ETH/BSC Address**: {user_data['eth']['address'] if user_data['eth'] else 'N/A'}\n\n"
                f"⚠️ **Security Warning** ⚠️\n"
                f"1. **Never share your mnemonic or private keys**.\n"
                f"2. Store them securely offline.\n"
                f"3. This message will auto-delete in 30 seconds.\n"
                f"4. Use this wallet only for trading small amounts.\n\n"
                f"To access trading features, subscribe using /subscribe."
            )
            context.job_queue.run_once(
                lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
                30,
                user_id=user_id
            )
        except Exception as e:
            await update.message.reply_text(f"Error creating wallet: {str(e)}")
            logger.error(f"Error in start for user {user_id}: {str(e)}")
            return
    else:
        subscription_message = await get_subscription_status_message(user)
        await update.message.reply_text(
            f"Welcome back!\n"
            f"Solana wallet: {user['solana']['public_key']}\n"
            f"ETH/BSC wallet: {user['eth']['address'] if user['eth'] else 'N/A'}\n"
            f"{subscription_message}\n"
            f"Use /set_wallet to import a different wallet."
        )

async def get_subscription_status_message(user: dict) -> str:
    if user.get('subscription_status') != 'active':
        return "No active subscription. Use /subscribe to start."
    expiry = user.get('subscription_expiry')
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry)
    if expiry and expiry > datetime.now():
        return f"Subscription active until {expiry.strftime('%Y-%m-%d %H:%M:%S')}."
    users_collection.update_one(
        {'user_id': user['user_id']},
        {'$set': {'subscription_status': 'inactive', 'subscription_expiry': None}}
    )
    return "Subscription expired. Use /subscribe to renew."

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("Please use /start to create a wallet.")
        return
    if await check_subscription(user_id):
        expiry = user.get('subscription_expiry')
        await update.message.reply_text(
            f"Active subscription until {expiry.strftime('%Y-%m-%d %H:%M:%S')}."
        )
        return
    usdt_amount = 1.0
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
        f"Deadline: {payment_deadline.strftime('%Y-%m-%d %H:%M:%S')}"
    )

async def generate_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("Please use /start to create a wallet.")
        return ConversationHandler.END
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription. Use /subscribe.")
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
        reply_markup=reply_markup
    )
    return CONFIRM_NEW_WALLET

async def confirm_generate_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if query.data == 'cancel_new_wallet':
        await query.message.reply_text("Wallet generation cancelled.")
        return ConversationHandler.END
    mnemo = Mnemonic("english")
    mnemonic = mnemo.generate(strength=256)
    user_data = await set_user_wallet(user_id, mnemonic=mnemonic)
    users_collection.replace_one({'user_id': user_id}, user_data, upsert=True)
    decrypted_user = await decrypt_user_wallet(user_id, user_data)
    message = await query.message.reply_text(
        f"New wallet generated!\n"
        f"**Mnemonic**: {decrypted_user['mnemonic']}\n"
        f"**Solana Public Key**: {user_data['solana']['public_key']}\n"
        f"**ETH/BSC Address**: {user_data['eth']['address'] if user_data['eth'] else 'N/A'}\n\n"
        f"⚠️ **Security Warning**: Save your mnemonic securely. This message will auto-delete in 30 seconds."
    )
    context.job_queue.run_once(
        lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
        30,
        user_id=user_id
    )
    return ConversationHandler.END

async def set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription. Use /subscribe.")
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton("Mnemonic", callback_data='mnemonic')],
        [InlineKeyboardButton("Private Key", callback_data='private_key')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Import wallet using:\n- Mnemonic: 24-word BIP-39 phrase\n- Private Key: Solana base58 or ETH/BSC hex",
        reply_markup=reply_markup
    )
    return SET_WALLET_METHOD

async def set_wallet_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    context.user_data['wallet_method'] = query.data
    if query.data == 'mnemonic':
        message = await query.message.reply_text(
            "Enter your 24-word mnemonic phrase (space-separated)."
        )
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id
        )
        return INPUT_MNEMONIC
    else:
        message = await query.message.reply_text(
            "Enter your private key (Solana base58 or ETH/BSC hex)."
        )
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id
        )
        return INPUT_PRIVATE_KEY

async def input_mnemonic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
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
            message = await update.message.reply_text("Invalid mnemonic. Try again.")
            context.job_queue.run_once(
                lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
                30,
                user_id=user_id
            )
            return INPUT_MNEMONIC
    except Exception as e:
        message = await update.message.reply_text(f"Error: {str(e)}. Try again.")
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id
        )
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
            reply_markup=reply_markup
        )
        return CONFIRM_SET_WALLET
    user_data = await set_user_wallet(user_id, mnemonic=mnemonic)
    users_collection.insert_one(user_data)
    decrypted_user = await decrypt_user_wallet(user_id, user_data)
    message = await update.message.reply_text(
        f"Wallet imported!\n"
        f"**Mnemonic**: {decrypted_user['mnemonic']}\n"
        f"**Solana Public Key**: {user_data['solana']['public_key']}\n"
        f"**ETH/BSC Address**: {user_data['eth']['address'] if user_data['eth'] else 'N/A'}\n\n"
        f"⚠️ **Security Warning**: Save your mnemonic securely. This message will auto-delete in 30 seconds."
    )
    context.job_queue.run_once(
        lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
        30,
        user_id=user_id
    )
    return ConversationHandler.END

async def input_private_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
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
            user_id=user_id
        )
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
            reply_markup=reply_markup
        )
        return CONFIRM_SET_WALLET
    user_data = await set_user_wallet(user_id, private_key=private_key)
    users_collection.insert_one(user_data)
    decrypted_user = await decrypt_user_wallet(user_id, user_data)
    message = await update.message.reply_text(
        f"Wallet imported!\n"
        f"**Solana Public Key**: {user_data['solana']['public_key']}\n"
        f"**ETH/BSC Address**: {user_data['eth']['address'] if user_data['eth'] else 'N/A'}\n\n"
        f"⚠️ **Security Warning**: This message will auto-delete in 30 seconds."
    )
    context.job_queue.run_once(
        lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
        30,
        user_id=user_id
    )
    return ConversationHandler.END

async def confirm_set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if query.data == 'cancel_set_wallet':
        await query.message.reply_text("Wallet import cancelled.")
        return ConversationHandler.END
    wallet_input = context.user_data['wallet_input']
    method = context.user_data['wallet_method']
    user_data = await set_user_wallet(user_id, mnemonic=wallet_input if method == 'mnemonic' else None,
                                     private_key=wallet_input if method == 'private_key' else None)
    users_collection.replace_one({'user_id': user_id}, user_data, upsert=True)
    decrypted_user = await decrypt_user_wallet(user_id, user_data)
    message = await query.message.reply_text(
        f"Wallet imported!\n"
        f"**{'Mnemonic' if method == 'mnemonic' else 'Private Key'}**: {decrypted_user['mnemonic'] if method == 'mnemonic' else '[Hidden]'}\n"
        f"**Solana Public Key**: {user_data['solana']['public_key']}\n"
        f"**ETH/BSC Address**: {user_data['eth']['address'] if user_data['eth'] else 'N/A'}\n\n"
        f"⚠️ **Security Warning**: Save securely. This message will auto-delete in 30 seconds."
    )
    context.job_queue.run_once(
        lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
        30,
        user_id=user_id
    )
    return ConversationHandler.END

async def reset_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription. Use /subscribe.")
        return
    users_collection.update_one({'user_id': user_id}, {'$set': {'posted_tokens': []}})
    await update.message.reply_text("Posted tokens reset. You will receive all new tokens again.")

async def set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription. Use /subscribe.")
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
    mode = query.data
    users_collection.update_one({'user_id': user_id}, {'$set': {'trading_mode': mode}})
    if mode == 'manual':
        await query.message.reply_text("Trading mode set to Manual. Use /trade to trade.")
        return ConversationHandler.END
    await query.message.reply_text("Trading mode set to Automatic. Enter auto-buy amount in SOL:")
    return SET_AUTO_BUY_AMOUNT

async def set_auto_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("Enter a positive amount.")
            return SET_AUTO_BUY_AMOUNT
        users_collection.update_one({'user_id': user_id}, {'$set': {'auto_buy_amount': amount}})
        await update.message.reply_text("Enter sell percentage (e.g., 10 for 10%):")
        return SET_SELL_PERCENTAGE
    except ValueError:
        await update.message.reply_text("Invalid amount. Enter a number.")
        return SET_AUTO_BUY_AMOUNT

async def set_sell_percentage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        percentage = float(update.message.text)
        if percentage <= 0:
            await update.message.reply_text("Enter a positive percentage.")
            return SET_SELL_PERCENTAGE
        users_collection.update_one({'user_id': user_id}, {'$set': {'sell_percentage': percentage}})
        await update.message.reply_text("Enter loss percentage (e.g., 5 for 5%):")
        return SET_LOSS_PERCENTAGE
    except ValueError:
        await update.message.reply_text("Invalid percentage. Enter a number.")
        return SET_SELL_PERCENTAGE

async def set_loss_percentage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        percentage = float(update.message.text)
        if percentage <= 0:
            await update.message.reply_text("Enter a positive percentage.")
            return SET_LOSS_PERCENTAGE
        users_collection.update_one({'user_id': user_id}, {'$set': {'loss_percentage': percentage}})
        user = users_collection.find_one({'user_id': user_id})
        await update.message.reply_text(
            f"Automatic trading settings saved:\n"
            f"Auto-buy: {user['auto_buy_amount']} SOL\n"
            f"Sell at: {user['sell_percentage']}% profit\n"
            f"Stop-loss at: {user['loss_percentage']}% loss"
        )
        context.job_queue.run_repeating(auto_trade, interval=10, first=5, user_id=user_id)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("Invalid percentage. Enter a number.")
        return SET_LOSS_PERCENTAGE

async def trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription. Use /subscribe.")
        return ConversationHandler.END
    user = users_collection.find_one({'user_id': user_id})
    if user['trading_mode'] != 'manual':
        await update.message.reply_text("Set trading mode to Manual using /setmode.")
        return ConversationHandler.END
    token = await fetch_latest_token()
    if not token:
        await update.message.reply_text("Failed to fetch token data.")
        return ConversationHandler.END
    if token['contract_address'] in user.get('posted_tokens', []):
        await update.message.reply_text("No new tokens available. Use /reset_tokens to clear posted tokens.")
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
    return SELECT_TOKEN_ACTION

async def select_token_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not await check_subscription(user_id):
        await query.message.reply_text("You need an active subscription. Use /subscribe.")
        return ConversationHandler.END
    user = users_collection.find_one({'user_id': user_id})
    if user['trading_mode'] != 'manual':
        await query.message.reply_text("Set trading mode to Manual using /setmode.")
        return ConversationHandler.END
    action, contract_address = query.data.split('_', 1)
    token = context.user_data.get('current_token', {})
    if not token or token['contract_address'] != contract_address:
        async with httpx.AsyncClient() as client:
            token_url = DEXSCREENER_TOKEN_API.format(token_address=contract_address)
            response = await client.get(token_url)
            response.raise_for_status()
            token_data = response.json()
            if not token_data or not token_data[0]:
                await query.message.reply_text("Failed to fetch token data.")
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
    context.user_data['trade_action'] = action
    if action == 'buy':
        await query.message.reply_text(
            f"Selected: {token['name']} ({token['symbol']})\nEnter amount to buy in SOL:"
        )
        return BUY_AMOUNT
    else:
        portfolio = user.get('portfolio', {})
        if contract_address not in portfolio:
            await query.message.reply_text(f"You don't hold {token['name']}.")
            return ConversationHandler.END
        await query.message.reply_text(
            f"Selected: {token['name']} ({token['symbol']})\n"
            f"Available: {portfolio[contract_address]['amount']} SOL worth\n"
            f"Enter amount to sell in SOL:"
        )
        return SELL_AMOUNT

async def buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("Enter a positive amount.")
            return BUY_AMOUNT
        balance = await check_balance(user_id, 'solana')
        if balance < amount:
            await update.message.reply_text(f"Insufficient SOL balance: {balance:.4f} SOL")
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
            reply_markup=reply_markup
        )
        return CONFIRM_TRADE
    except ValueError:
        await update.message.reply_text("Invalid amount. Enter a number.")
        return BUY_AMOUNT

async def sell_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("Enter a positive amount.")
            return SELL_AMOUNT
        token = context.user_data['current_token']
        user = users_collection.find_one({'user_id': user_id})
        portfolio = user.get('portfolio', {})
        if token['contract_address'] not in portfolio:
            await update.message.reply_text(f"You don't hold {token['name']}.")
            return ConversationHandler.END
        if amount > portfolio[token['contract_address']]['amount']:
            await update.message.reply_text(
                f"Insufficient balance: {portfolio[token['contract_address']]['amount']} SOL worth"
            )
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
            reply_markup=reply_markup
        )
        return CONFIRM_TRADE
    except ValueError:
        await update.message.reply_text("Invalid amount. Enter a number.")
        return SELL_AMOUNT

async def confirm_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
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
            await query.message.reply_text(f"Bought {amount} SOL of {token['name']} at ${token['price_usd']:.6f}.")
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
            await query.message.reply_text(f"Sold {amount} SOL of {token['name']} at ${token['price_usd']:.6f}.")
    else:
        await query.message.reply_text("Trade failed.")
    return ConversationHandler.END

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription. Use /subscribe.")
        return
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("Please use /start to create a wallet.")
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
    await update.message.reply_text(message)

async def transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not await check_subscription(user_id):
        await update.message.reply_text("You need an active subscription. Use /subscribe.")
        return ConversationHandler.END
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("Please use /start to create a wallet.")
        return ConversationHandler.END
    portfolio = user.get('portfolio', {})
    if not portfolio:
        await update.message.reply_text("No tokens to transfer.")
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
    context.user_data['transfer_token'] = query.data
    user = users_collection.find_one({'user_id': user_id})
    token = user['portfolio'][query.data]
    await query.message.reply_text(
        f"Selected: {token['name']}\nEnter amount to transfer (in SOL worth):"
    )
    return TRANSFER_AMOUNT

async def transfer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("Enter a positive amount.")
            return TRANSFER_AMOUNT
        token_contract = context.user_data['transfer_token']
        user = users_collection.find_one({'user_id': user_id})
        token = user['portfolio'][token_contract]
        if amount > token['amount']:
            await update.message.reply_text(f"Insufficient balance: {token['amount']} SOL worth")
            return TRANSFER_AMOUNT
        context.user_data['transfer_amount'] = amount
        await update.message.reply_text("Enter recipient Solana address:")
        return TRANSFER_ADDRESS
    except ValueError:
        await update.message.reply_text("Invalid amount. Enter a number.")
        return TRANSFER_AMOUNT

async def transfer_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
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
            await update.message.reply_text("Transfer failed.")
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
            return {
                'name': name,
                'symbol': symbol,
                'contract_address': token_address,
                'price_usd': float(pair_data.get('priceUsd', '0.0')),
                'market_cap': float(pair_data.get('marketCap', 0.0)),
                'dexscreener_url': f"https://dexscreener.com/solana/{token_address}",
                'liquidity': float(pair_data.get('liquidity', {}).get('usd', 0.0)),
                'volume': float(pair_data.get('volume', {}).get('h24', 0.0))
            }
        except Exception as e:
            logger.error(f"Error fetching token: {str(e)}")
            return None

async def periodic_token_check(context: ContextTypes.DEFAULT_TYPE):
    token = await fetch_latest_token()
    if not token:
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
            balance = w3_eth.eth.get_balance(user['eth']['address'])
            return w3_eth.from_wei(balance, 'ether')
        elif chain == 'bsc' and user.get('bsc'):
            balance = w3_bsc.eth.get_balance(user['bsc']['address'])
            return w3_bsc.from_wei(balance, 'ether')
        return 0.0
    except Exception as e:
        logger.error(f"Error checking {chain} balance: {str(e)}")
        return 0.0

async def execute_trade(user_id, contract_address, amount, action, chain):
    if chain != 'solana':
        return False
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        return False
    decrypted_user = await decrypt_user_wallet(user_id, user)
    solana_private_key = decrypted_user['solana']['private_key']
    if not solana_private_key or solana_private_key == "[Decryption Failed]":
        return False
    keypair = Keypair.from_bytes(b58decode(solana_private_key))
    from_address = str(keypair.pubkey())
    balance = await check_balance(user_id, 'solana')
    if action == 'buy' and balance < amount:
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
                logger.error(f"Swap route failed: {route.get('msg')}")
                return False
            # Simplified trade execution (implement actual swap logic as needed)
            return True
        except Exception as e:
            logger.error(f"Trade execution failed: {str(e)}")
            return False

async def execute_transfer(user_id, recipient, token_contract, amount, chain):
    if chain != 'solana':
        return False
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        return False
    decrypted_user = await decrypt_user_wallet(user_id, user)
    solana_private_key = decrypted_user['solana']['private_key']
    if not solana_private_key or solana_private_key == "[Decryption Failed]":
        return False
    keypair = Keypair.from_bytes(b58decode(solana_private_key))
    # Simplified transfer (implement actual transfer logic as needed)
    return True

async def auto_trade(context: ContextTypes.DEFAULT_TYPE, user_id: int, token: dict):
    user = users_collection.find_one({'user_id': user_id})
    if not user or user['trading_mode'] != 'automatic':
        return
    amount = user['auto_buy_amount']
    balance = await check_balance(user_id, 'solana')
    if balance < amount:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"Insufficient SOL balance for auto trade: {balance:.4f} SOL"
        )
        return
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

# Flask routes
@app.route('/')
def health_check():
    return jsonify({'status': 'ok'}), 200

@app.route('/webhook', methods=['POST'])
async def payment_webhook():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid data'}), 400
    tx_hash = data.get('hash')
    sender = data.get('from')
    if not tx_hash or not sender:
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
                    await application.bot.send_message(
                        chat_id=user['user_id'],
                        text=f"Payment confirmed! Subscription active until {expiry.strftime('%Y-%m-%d %H:%M:%S')}."
                    )
                    return jsonify({'status': 'success'}), 200
    except Exception as e:
        logger.error(f"Payment webhook error: {str(e)}")
    return jsonify({'error': 'Payment not processed'}), 400

async def setup_webhook():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
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
    application.job_queue.run_repeating(periodic_token_check, interval=5, first=5)
    await application.bot.set_webhook(f"{WEBHOOK_URL}/webhook/telegram")
    return application

def main():
    loop = asyncio.get_event_loop()
    global application
    application = loop.run_until_complete(setup_webhook())
    
    @app.route('/webhook/telegram', methods=['POST'])
    async def telegram_webhook():
        update = Update.de_json(request.get_json(), application.bot)
        await application.process_update(update)
        return jsonify({'status': 'ok'}), 200

    # Run Flask with WebhookServer
    webhook_server = WebhookServer('0.0.0.0', PORT, app)
    loop.run_until_complete(application.run_webhook(
        webhook_server=webhook_server,
        webhook_path='/webhook/telegram',
        close_loop=False
    ))

if __name__ == '__main__':
    main()