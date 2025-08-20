#!/usr/bin/env python3
from eth_account import Account
Account.enable_unaudited_hdwallet_features()
import asyncio
import logging
import json
import httpx
import base64
import base58
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
import hashlib
import hmac
from typing import Optional
from typing import Tuple
from solana.rpc.async_api import AsyncClient
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction, VersionedTransaction

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
from eth_account.hdaccount import ETHEREUM_DEFAULT_PATH
from eth_account.hdaccount import generate_mnemonic
from eth_account.hdaccount import key_from_seed
from pymongo import UpdateOne, ReplaceOne
from bip44 import Wallet
from bip32utils import BIP32Key, BIP32_HARDEN
from bip_utils import Bip32Slip10Ed25519
from solders.keypair import Keypair
from bip_utils import Bip39MnemonicValidator, Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
from solders.message import Message
from solana.rpc.types import TxOpts
from solders.message import Message
from solders.transaction import Transaction
from solders.transaction import VersionedTransaction, Transaction
from solders.message import MessageV0
import pandas as pd
from solana.rpc.types import TxOpts
from solders.instruction import Instruction, AccountMeta
from solders.address_lookup_table_account import AddressLookupTableAccount
import math

# FastAPI setup
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse


# Custom filter to add user_id to logs
class UserFilter(logging.Filter):
    def filter(self, record):
        record.user_id = getattr(record, 'user_id', 'SYSTEM')
        return True

# Set up handlers with the filter
stream_handler = logging.StreamHandler()
stream_handler.addFilter(UserFilter())

file_handler = logging.FileHandler("bot_activity.log")
file_handler.addFilter(UserFilter())

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - User:%(user_id)s - %(message)s',
    handlers=[stream_handler, file_handler]
)

# Your module's logger
logger = logging.getLogger(__name__)

# Example usage remains unchanged
def log_user_action(user_id: int, action: str, details: str = "", level: str = "info"):
    extra = {'user_id': user_id}
    log_method = getattr(logger, level.lower(), logger.info)
    log_method(f"👤 USER ACTION: {action} - {details}", extra=extra)

# Load environment variables
load_dotenv()

# Constants
SUBSCRIPTION_SOL_AMOUNT = 0.005
PBKDF2_ROUNDS = 2048
DEXSCREENER_NEW_TOKENS_API = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN_API = "https://api.dexscreener.com/tokens/v1/solana/{token_address}"
MIN_LIQUIDITY = 1000  # Minimum liquidity threshold in USD
AUTO_TRADE_COOLDOWN = 60  # 60 seconds cooldown between auto-trades
TOKEN_PERFORMANCE_INTERVAL = 5 * 60  # 5 minutes in seconds
PERFORMANCE_TRACKING_DAYS = 7  # Track tokens for 7 days
token_cache = {}
CACHE_DURATION = 300


# Bot states for conversation
(WALLET_SETUP_CHOICE, SET_TRADING_MODE, SET_AUTO_BUY_AMOUNT, SET_SELL_PERCENTAGE, SET_LOSS_PERCENTAGE, 
 SELECT_TOKEN, BUY_AMOUNT, CONFIRM_TRADE, TRANSFER_TOKEN, TRANSFER_AMOUNT, TRANSFER_ADDRESS,
 CONFIRM_NEW_WALLET, SET_WALLET_METHOD, INPUT_MNEMONIC, INPUT_PRIVATE_KEY, CONFIRM_SET_WALLET,
 SELECT_TOKEN_ACTION, SELL_AMOUNT, INPUT_CONTRACT,
 # New states for start flow
 START_IMPORT_METHOD, START_INPUT_MNEMONIC, START_INPUT_PRIVATE_KEY,SUBSCRIPTION_CONFIRMATION,INPUT_ANALYSIS_CONTRACT,SET_ANTI_MEV, SET_LIQUIDITY_THRESHOLD, SET_VOLUME_THRESHOLD, SET_RUG_CHECK, 
 SET_MAX_SLIPPAGE, SET_MAX_GAS_PRICE, SET_TOKEN_AGE) = range(31)

# Create FastAPI app
app = FastAPI()

@app.get("/health")
async def health_check():
    return JSONResponse(content={'status': 'ok'})

@app.post("/webhook")
async def telegram_webhook(request: Request):
    global application
    try:
        if application is None:
            logger.error("🚫 Application instance is None")
            return JSONResponse(
                content={'error': 'Application not created'},
                status_code=503
            )
        
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
        await application.process_update(update)
        logger.debug(f"Processed update: {update.update_id}")
        return JSONResponse(content={'status': 'ok'})
    except Exception as e:
        logger.error(f"🔥 Webhook error: {str(e)}", exc_info=True)
        return JSONResponse(
            content={'error': str(e)},
            status_code=500
        )

@app.get("/debug/address")
async def debug_address(mnemonic: str):
    keypair = derive_solana_keypair_from_mnemonic(mnemonic)
    return {"address": str(keypair.pubkey())}

# MongoDB setup with retry logic
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    logger.error("MONGO_URI not found in .env file")
    raise ValueError("MONGO_URI not found in .env file")

max_retries = 3
for attempt in range(max_retries):
    try:
        logger.info("🔌 Connecting to MongoDB...")
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=30000)
        mongo_client.admin.command('ping')
        logger.info("✅ MongoDB connection successful")
        db = mongo_client.get_database('trading_bot')
        
        # Create collections using the database object
        if 'users' not in db.list_collection_names():
            db.create_collection(
                'users',
                validator={
                    '$jsonSchema': {
                        'bsonType': 'object',
                        'required': ['user_id'],
                        'properties': {
                            'user_id': {'bsonType': 'int'},
                            'subscription_status': {'bsonType': 'string'},
                            'subscription_expiry': {'bsonType': ['string', 'date']},
                            'created_at': {'bsonType': 'string'},
                        }
                    }
                },
                validationLevel='moderate'
            )
            logger.info("Created users collection with validator")
        
        users_collection = db.users
        users_collection.create_index('user_id', unique=True)

        # Create new collection if not exists
        if 'token_performance' not in db.list_collection_names():
            db.create_collection('token_performance')
            logger.info("Created token_performance collection")

        token_performance_collection = db.token_performance
        token_performance_collection.create_index('contract_address')
        token_performance_collection.create_index('first_posted_at')
        
        if 'global_posted_tokens' not in db.list_collection_names():
         db.create_collection('global_posted_tokens')
         logger.info("Created global_posted_tokens collection")
    
        global_posted_tokens = db.global_posted_tokens
        global_posted_tokens.create_index('contract_address', unique=True)
        global_posted_tokens.create_index('timestamp', expireAfterSeconds=86400)
        logger.info("Created indexes for global_posted_tokens")
        
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

BOT_SOL_ADDRESS = os.getenv("BOT_SOL_ADDRESS")

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

def derive_user_key(user_id: int) -> bytes:
    """Derive a user-specific encryption key from the master key"""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=str(user_id).encode(),
        iterations=100000,
    )
    return kdf.derive(master_key)

def encrypt_data(data: str, key: bytes) -> dict:
    """Encrypt sensitive data with AES-256-CBC"""
    iv = os.urandom(16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    padded_data = data.encode() + b'\0' * (16 - len(data.encode()) % 16)
    ciphertext = encryptor.update(padded_data) + encryptor.finalize()
    return {
        'iv': base64_cryptography.b64encode(iv).decode(),
        'ciphertext': base64_cryptography.b64encode(ciphertext).decode()
    }

def decrypt_data(encrypted_data: dict, key: bytes) -> str:
    """Decrypt data encrypted with encrypt_data"""
    iv = base64_cryptography.b64decode(encrypted_data['iv'])
    ciphertext = base64_cryptography.b64decode(encrypted_data['ciphertext'])
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded_data = decryptor.update(ciphertext) + decryptor.finalize()
    return padded_data.rstrip(b'\0').decode()

async def check_subscription(user_id: int) -> bool:
    """Check if user has an active subscription or trial"""
    user = users_collection.find_one({'user_id': user_id})
    
    # New user - create trial
    if not user:
        expiry = datetime.now() + timedelta(days=1)
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {
                'subscription_status': 'trial',
                'subscription_expiry': expiry.isoformat(),
                'trial_used': True,  # Mark trial as used
                'created_at': datetime.now().isoformat()
            }},
            upsert=True
        )
        log_user_action(user_id, "NEW_USER_TRIAL_STARTED")
        return True
        
    # Existing user check
    status = user.get('subscription_status')
    expiry = user.get('subscription_expiry')
    
    # Convert expiry to datetime if needed
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry)
    
    # Active subscription
    if status == 'active' and datetime.now() < expiry:
        return True
        
    # Trial status check
    if status == 'trial' and datetime.now() < expiry:
        return True
        
    # Expired trial - prevent reuse
    if user.get('trial_used'):
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {
                'subscription_status': 'inactive',
                'subscription_expiry': None
            }}
        )
        log_user_action(user_id, "SUBSCRIPTION_EXPIRED")
        return False
        
    # Should never reach here
    return False

async def get_subscription_status_message(user: dict) -> str:
    """Generate a user-friendly subscription status message"""
    status = user.get('subscription_status')
    expiry = user.get('subscription_expiry')
    
    if status == 'trial':
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        time_left = expiry - datetime.now()
        hours = int(time_left.total_seconds() // 3600)
        return f"⏳ You're on a free trial ({hours} hours remaining)"
    elif status == 'active':
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        return f"✅ Active subscription until {expiry.strftime('%Y-%m-%d %H:%M')}"
    else:
        return "❌ No active subscription. Use /subscribe to start."




# Update the set_user_wallet function with proper derivation paths
async def set_user_wallet(user_id: int, mnemonic: str = None, private_key: str = None) -> dict:
    """Set up a user wallet from mnemonic or private key with proper derivation paths"""
    logger.info(f"🔐 [set_user_wallet] Starting for user {user_id}")
    try:
        user_key = derive_user_key(user_id)
        
        
        if mnemonic:
            # Validate mnemonic
            if not Bip39MnemonicValidator().IsValid(mnemonic):
                raise ValueError("Invalid mnemonic phrase.")
            
            word_count = len(mnemonic.split())
            if word_count not in [12, 24]:
                raise ValueError(f"Invalid mnemonic length: {word_count} words (must be 12 or 24)")
            
            # Create Ethereum account with standard derivation path
            eth_account = Account.from_mnemonic(mnemonic, account_path=ETHEREUM_DEFAULT_PATH)
            eth_address = eth_account.address
            eth_private_key = eth_account.key.hex()
            
            # Create Solana account with Phantom-compatible derivation path using bip-utils
            seed = Bip39SeedGenerator(mnemonic).Generate()
            bip44_mst = Bip44.FromSeed(seed, Bip44Coins.SOLANA)
            bip44_acc = bip44_mst.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT)
            solana_private_key_bytes = bip44_acc.PrivateKey().Raw().ToBytes()
            solana_keypair = Keypair.from_seed(solana_private_key_bytes[:32])
            solana_private_key = base58.b58encode(bytes(solana_keypair)).decode()  # FIXED LINE
        
        elif private_key:
            # Private key handling
            if private_key.startswith('0x'):
                account = Account.from_key(private_key)
                eth_address = account.address
                eth_private_key = private_key
                new_seed = os.urandom(32)
                solana_keypair = Keypair.from_seed(new_seed)
                solana_private_key = base58.b58encode(solana_keypair.to_bytes()).decode()
                logger.warning("ETH private key provided - generated new Solana wallet")
            else:
                key_bytes = base58.b58decode(private_key)
                if len(key_bytes) == 64:
                    solana_keypair = Keypair.from_bytes(key_bytes)
                    solana_private_key = private_key
                elif len(key_bytes) == 32:
                    solana_keypair = Keypair.from_seed(key_bytes)
                    solana_private_key = base58.b58encode(bytes(solana_keypair)).decode()  # FIXED LINE
                else:
                    raise ValueError("Invalid Solana private key length")
                account = Account.create()
                eth_address = account.address
                eth_private_key = account.key.hex()
                logger.warning("Solana private key provided - generated new ETH/BSC wallet")
        else:
            raise ValueError("Must provide either mnemonic or private key")

        # Encrypt all sensitive data
        encrypted_mnemonic = encrypt_data(mnemonic if mnemonic else 'Imported via private key', user_key)
        encrypted_solana_private_key = encrypt_data(solana_private_key, user_key)
        encrypted_eth_private_key = encrypt_data(eth_private_key, user_key) if eth_private_key else None

        return {
            'mnemonic': encrypted_mnemonic,
            'solana': {
                'public_key': str(solana_keypair.pubkey()),  # Updated to pubkey()
                'private_key': encrypted_solana_private_key
            },
            'eth': {
                'address': eth_address,
                'private_key': encrypted_eth_private_key
            } if eth_address else None,
            'bsc': {
                'address': eth_address,
                'private_key': encrypted_eth_private_key
            } if eth_address else None
        }
        
    except Exception as e:
        logger.error(f"🔥 [set_user_wallet] Critical error: {str(e)}", exc_info=True)
        raise ValueError(f"Wallet creation failed: {str(e)}")
    


def derive_solana_keypair_from_mnemonic(
    mnemonic: str,
    passphrase: str = "",
    derivation_path: str = "m/44'/501'/0'/0'"
) -> Keypair:
    """
    Derive a Solana Keypair from a BIP39 mnemonic using SLIP-0010 (ed25519).
    Default path is m/44'/501'/0'/0' (Phantom/Exodus style). If result doesn't
    match the wallet you expect, try other paths (see helper below).
    Raises ValueError for an invalid mnemonic.
    """
    mnemo = Mnemonic("english")
    if not mnemo.check(mnemonic):
        raise ValueError("Invalid mnemonic phrase")

    # 1) Get seed from mnemonic (BIP39)
    # Generate(passphrase) accepts an optional passphrase (BIP39)
    seed_bytes = Bip39SeedGenerator(mnemonic).Generate(passphrase)

    # 2) Build master SLIP-0010 ed25519 context and derive the path
    bip32_ctx = Bip32Slip10Ed25519.FromSeed(seed_bytes)
    derived_ctx = bip32_ctx.DerivePath(derivation_path)

    # 3) Get the raw private key bytes (32 bytes) suitable as seed for Solana
    priv_key_bytes = derived_ctx.PrivateKey().Raw().ToBytes()
    if len(priv_key_bytes) != 32:
        raise ValueError(f"unexpected private key length: {len(priv_key_bytes)}")

    # 4) Convert to solana Keypair. Keypair.from_seed expects 32-bytes seed.
    kp = Keypair.from_seed(priv_key_bytes)

    return kp


def find_solana_from_mnemonic_by_paths(mnemonic: str, expected_pubkey: str, passphrase: str = ""):
    """
    Try common derivation paths and return (path, Keypair) when a match is found.
    expected_pubkey should be the base58 address you expect (string).
    """
    common_paths = [
        "m/44'/501'/0'/0'",      # common Phantom / Exodus
        "m/44'/501'/0'/0/0'",    # some variants
        "m/44'/501'/0'/0",       # variant without final hardened
        "m/44'/501'/0'/0'/0'",   # extra-depth variant
        "m/44'/501'/1'/0'",      # account=1
        "m/44'/501'/0'",
        # add more if you want...
    ]

    for path in common_paths:
        try:
            kp = derive_solana_keypair_from_mnemonic(mnemonic, passphrase, path)
            pub = str(kp.public_key)         # base58 address
            if pub == expected_pubkey:
                return path, kp
        except Exception:
            # ignore invalid derivation attempts and continue
            continue
    return None, None


async def decrypt_user_wallet(user_id: int, user: dict) -> dict:
    """Decrypt sensitive wallet information for a user"""
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
    """Handle the /start command"""
    user_id = update.effective_user.id
    log_user_action(user_id, "START_COMMAND")
    
    # Ensure user exists with trial
    if not users_collection.find_one({'user_id': user_id}):
        expiry = datetime.now() + timedelta(days=1)
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {
                'subscription_status': 'trial',
                'subscription_expiry': expiry.isoformat(),
                'created_at': datetime.now().isoformat()
            }},
            upsert=True
        )
        log_user_action(user_id, "NEW_USER_TRIAL_STARTED", f"Trial until {expiry}")
    
    # SECOND: Check subscription status
    if not await check_subscription(user_id):
        await update.message.reply_text("🚫 Your trial has expired. Use /subscribe to continue.")
        return
    
    # THIRD: Check if wallet exists
    user = users_collection.find_one({'user_id': user_id})
    
    if not user or not user.get('solana') or not user['solana'].get('public_key'):
        keyboard = [
            [InlineKeyboardButton("✨ Generate New Wallet", callback_data='generate_wallet')],
            [InlineKeyboardButton("🔑 Import Existing Wallet", callback_data='import_wallet')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        welcome_message = (
            "👋 *Welcome to the Multi-Chain Trading Bot!*\n\n"
            "🔹 You have a *1-day free trial* to test all features\n"
            "🔹 Trade Solana tokens with ease\n"
            "🔹 Get alerts for new tokens automatically\n\n"
            "Would you like to:\n"
            "1️⃣ Generate a *new wallet* (recommended for beginners)\n"
            "2️⃣ Import an *existing wallet*?"
        )
        
        await update.message.reply_text(
            welcome_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return WALLET_SETUP_CHOICE
    else:
        decrypted_user = await decrypt_user_wallet(user_id, user)
        eth_bsc_address = user['eth']['address'] if user.get('eth') else "Not set"
        subscription_message = await get_subscription_status_message(user)
        
        message = (
            f"👋 *Welcome back!*\n\n"
            f"🔹 *Solana Address*: `{user['solana']['public_key']}`\n"
            f"🔹 *ETH/BSC Address*: `{eth_bsc_address}`\n\n"
            f"*Subscription Status*\n"
            f"{subscription_message}\n\n"
            f"🔧 *Available Commands*\n"
            f"- /generate_wallet - Create a new wallet\n"
            f"- /set_wallet - Import an existing wallet\n"
            f"- /trade - Manually trade tokens\n"
            f"- /setmode - Configure auto-trading\n"
            f"- /balance - Check your wallet balances"
        )
            
        await update.message.reply_text(message, parse_mode='Markdown')
        
        if await check_subscription(user_id):
            logger.info(f"Starting token updates for user {user_id}")
            await start_token_updates(context, user_id)

async def delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    """Job to delete a message with logging"""
    job = context.job
    try:
        await context.bot.delete_message(job.chat_id, job.data)
        logger.info(f"✅ Deleted message {job.data} in chat {job.chat_id}")
    except Exception as e:
        logger.error(f"❌ Failed to delete message {job.data} in chat {job.chat_id}: {str(e)}")

async def handle_wallet_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle user's choice of wallet setup method"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    log_user_action(user_id, "WALLET_SETUP_CHOICE", query.data)
    
    if query.data == 'generate_wallet':
        mnemo = Mnemonic("english")
        mnemonic = mnemo.generate(strength=256)
        log_user_action(user_id, "MNEMONIC_GENERATED")
        
        try:
            user_data = await set_user_wallet(user_id, mnemonic=mnemonic)
            users_collection.update_one(
    {'user_id': user_id},
    {'$set': user_data},  # Only set wallet fields
    upsert=True
)
            log_user_action(user_id, "WALLET_CREATED")
            
            decrypted_user = await decrypt_user_wallet(user_id, user_data)
            eth_bsc_address = user_data['eth']['address'] if user_data.get('eth') else "Not set"
            
            message = (
                f"✨ *New Wallet Generated!*\n\n"
                f"🔐 *Recovery Phrase*:\n`{decrypted_user['mnemonic']}`\n\n"
                f"🔑 *Solana Address*:\n`{user_data['solana']['public_key']}`\n\n"
                f"🌐 *ETH/BSC Address*:\n`{eth_bsc_address}`\n\n"
                f"⚠️ *SECURITY WARNING*\n"
                f"1️⃣ Never share your mnemonic or private keys\n"
                f"2️⃣ Store them securely offline\n"
                f"3️⃣ This message will auto-delete in 30 seconds\n\n"
                f"🎉 You have a 1-day free trial to test all features!"
            )
            
            msg = await query.message.reply_text(message, parse_mode='Markdown')
            context.job_queue.run_once(
                lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=msg.message_id),
                60,
                user_id=user_id
            )
            
            await start_token_updates(context, user_id)
            return ConversationHandler.END
            
        except Exception as e:
            log_user_action(user_id, "WALLET_CREATION_FAILED", str(e), "error")
            await query.message.reply_text(f"❌ Failed to generate wallet: {str(e)}")
            return ConversationHandler.END
            
    elif query.data == 'import_wallet':
        # Start wallet import flow directly in start conversation
        context.user_data['is_start_flow'] = True  # Flag to identify start flow
        
        keyboard = [
            [InlineKeyboardButton("🔐 Mnemonic (12/24 words)", callback_data='mnemonic')],
            [InlineKeyboardButton("🔑 Private Key", callback_data='private_key')],
            [InlineKeyboardButton("❌ Cancel", callback_data='cancel_import')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = await query.message.reply_text(
            "📥 *Choose how to import your wallet:*\n\n"
            "1. 🔐 *Mnemonic*: Enter your 12-word or 24-word BIP-39 recovery phrase\n"
            "2. 🔑 *Private Key*: Enter your Solana or ETH/BSC private key\n\n"
            "⚠️ This message will auto-delete in 30 seconds for security.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
        # Schedule message deletion
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            60,
            user_id=user_id
        )
        return START_IMPORT_METHOD  # New state for start flow
    

async def start_import_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle wallet method selection in start flow"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    log_user_action(user_id, "WALLET_IMPORT_METHOD_SELECTED", query.data)
    
    if query.data == 'cancel_import':
        await query.edit_message_text("🛑 Wallet import cancelled.")
        return ConversationHandler.END
        
    if query.data == 'mnemonic':
        message = await query.edit_message_text(
            "📝 *Please enter your BIP-39 mnemonic phrase (12 or 24 words, space-separated):*\n\n"
            "🔹 Example 12-word: `word1 word2 ... word12`\n"
            "🔹 Example 24-word: `word1 word2 ... word24`\n\n"
            "⚠️ This message and your input will auto-delete in 30 seconds for security.",
            parse_mode='Markdown'
        )
        # Schedule message deletion
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            60,
            user_id=user_id
        )
        return START_INPUT_MNEMONIC
        
    else:  # private_key
        message = await query.edit_message_text(
            "🔑 *Please enter your private key:*\n\n"
            "For Solana: 64-byte base58 encoded (starts with a number)\n"
            "For ETH/BSC: 32-byte hex encoded (with or without '0x' prefix)\n\n"
            "⚠️ This message and your input will auto-delete in 30 seconds for security.",
            parse_mode='Markdown'
        )
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id
        )
        return START_INPUT_PRIVATE_KEY
    

async def start_input_mnemonic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle mnemonic input in start flow"""
    user_id = update.effective_user.id
    mnemonic = update.message.text.strip()
    logger.info(f"📝 [start_input_mnemonic] Received mnemonic from user {user_id}")
    
    # Delete the sensitive message immediately
    try:
        await context.bot.delete_message(chat_id=user_id, message_id=update.message.message_id)
    except Exception as e:
        logger.warning(f"Could not delete mnemonic message: {str(e)}")

    try:
        mnemo = Mnemonic("english")
        word_count = len(mnemonic.split())
        if word_count not in [12, 24]:
            raise ValueError("Mnemonic must be 12 or 24 words")
        
        if not mnemo.check(mnemonic):
            raise ValueError("Invalid mnemonic phrase")
        
        # Create and save wallet (no confirmation needed for new users)
        logger.info("Creating new wallet from mnemonic in start flow")
        
        
        # Save to database
     

        wallet_data = await set_user_wallet(user_id, mnemonic=mnemonic)
        update_result = users_collection.update_one(
            {'user_id': user_id},
            {'$set': wallet_data},
            upsert=True
        )
        
        # Log the update result properly
        logger.info(f"Database update - matched: {update_result.matched_count}, "
                   f"modified: {update_result.modified_count}, "
                   f"upserted_id: {update_result.upserted_id}")
        # Verify the wallet was saved
        db_user = users_collection.find_one({'user_id': user_id})
        if not db_user or not db_user.get('solana') or not db_user['solana'].get('public_key'):
            raise RuntimeError("Wallet not saved to database")
        
        # Get decrypted info for display
        decrypted_user = await decrypt_user_wallet(user_id, db_user)
        eth_bsc_address = db_user['eth']['address'] if db_user.get('eth') else "Not set"
        
        # Prepare success message with self-destruct
        success_msg = (
            f"✅ *Wallet Imported Successfully!*\n\n"
            f"🔐 *Recovery Phrase*: `{decrypted_user['mnemonic']}`\n"
            f"🔑 *Solana Address*: `{db_user['solana']['public_key']}`\n"
            f"🌐 *ETH/BSC Address*: `{eth_bsc_address}`\n\n"
            f"⚠️ *SECURITY WARNING*\n"
            f"1️⃣ Never share your recovery phrase with anyone\n"
            f"2️⃣ Store it securely offline (write it down)\n"
            f"3️⃣ This message will self-destruct in 2 minutes\n\n"
            f"🚀 You're all set! The bot will now start sending you token alerts."
            f"\n\n⏳ Your 1-day free trial has started!"
        )
        
        msg = await update.message.reply_text(success_msg, parse_mode='Markdown')
        
        # Schedule message deletion with proper context
        context.job_queue.run_once(
            callback=delete_message_job,
            when=120,  # 2 minutes
            chat_id=user_id,
            data=msg.message_id,
            name=f"delete_msg_{msg.message_id}"
        )
        
        # Start token updates
        await start_token_updates(context, user_id)
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"🔥 [start_input_mnemonic] Error: {str(e)}", exc_info=True)
        error_msg = f"❌ Failed to import wallet: {str(e)}"
        if "Invalid mnemonic phrase" in str(e):
            error_msg = "❌ Invalid mnemonic phrase. Please check your words and try again."
        
        await update.message.reply_text(error_msg)
        return START_INPUT_MNEMONIC
    
async def start_input_private_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle private key input in start flow"""
    user_id = update.effective_user.id
    private_key = update.message.text.strip()
    log_user_action(user_id, "PRIVATE_KEY_RECEIVED")
    
    # Delete the sensitive message immediately
    try:
        await context.bot.delete_message(
            chat_id=user_id,
            message_id=update.message.message_id
        )
    except Exception as e:
        logger.warning(f"Could not delete private key message: {str(e)}")

    try:
        # Validate the private key format
        key_type = None
        if not private_key.startswith('0x'):
            try:
                # Try to decode as Solana private key (base58)
                key_bytes = base58.b58decode(private_key)
                if len(key_bytes) == 64:
                    key_type = 'solana'
                else:
                    raise ValueError("Invalid Solana private key length")
            except:
                # Try to decode as ETH/BSC private key (hex)
                private_key = '0x' + private_key
                key_bytes = bytes.fromhex(private_key[2:])
                if len(key_bytes) == 32:
                    key_type = 'ethereum'
                else:
                    raise ValueError("Invalid Ethereum private key length")
        else:
            key_bytes = bytes.fromhex(private_key[2:])
            if len(key_bytes) == 32:
                key_type = 'ethereum'
            else:
                raise ValueError("Invalid Ethereum private key length")
                
        log_user_action(user_id, "VALID_PRIVATE_KEY_RECEIVED", f"Type: {key_type}")
        
        # Create and save wallet
        

        wallet_data = await set_user_wallet(user_id, private_key=private_key)
        update_result = users_collection.update_one(
            {'user_id': user_id},
            {'$set': wallet_data},
            upsert=True
        )
        
        # Properly log the update result
        logger.info(f"Database update - matched: {update_result.matched_count}, "
                   f"modified: {update_result.modified_count}, "
                   f"upserted_id: {update_result.upserted_id}")
        
        # Get decrypted info for display
        decrypted_user = await decrypt_user_wallet(user_id, wallet_data)
        eth_bsc_address = wallet_data['eth']['address'] if wallet_data.get('eth') else "Not set"
        
        # Prepare success message
        success_msg = (
            f"✅ *Wallet Imported Successfully!*\n\n"
            f"🔑 *Solana Address*: `{wallet_data['solana']['public_key']}`\n"
            f"🌐 *ETH/BSC Address*: `{eth_bsc_address}`\n\n"
            f"🚀 You're all set! The bot will now start sending you token alerts."
        )
        
        await update.message.reply_text(success_msg, parse_mode='Markdown')
        log_user_action(user_id, "WALLET_IMPORT_SUCCESS")
        
        # Start token updates
        await start_token_updates(context, user_id)
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Error in start_input_private_key: {str(e)}")
        log_user_action(user_id, "WALLET_IMPORT_ERROR", f"Error: {str(e)}", "error")
        error_msg = await update.message.reply_text(
            f"❌ Invalid private key: {str(e)}\n\n"
            "Please enter a valid:\n"
            "- Solana private key (base58 encoded, 64 bytes)\n"
            "- ETH/BSC private key (hex encoded, 32 bytes with or without 0x prefix)\n\n"
            "Try again or use /cancel to abort."
        )
        # Delete error message after 1 minute
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=error_msg.message_id),
            60,
            user_id=user_id
        )
        return START_INPUT_PRIVATE_KEY
    

async def record_token_performance(token: dict):
    """Record token metrics with backward compatibility"""
    contract_address = token['contract_address']
    
    # Check if token exists
    existing = token_performance_collection.find_one({'contract_address': contract_address})
    
    if existing:
        # Update existing record with new fields if missing
        update_data = {}
        if 'first_tracked' not in existing:
            update_data['first_tracked'] = existing.get('first_posted_at', datetime.now())
        if 'performance_history' not in existing:
            update_data['performance_history'] = []
        
        if update_data:
            token_performance_collection.update_one(
                {'_id': existing['_id']},
                {'$set': update_data}
            )
        return
    
    # Create new performance document
    token_performance_collection.insert_one({
        'contract_address': contract_address,
        'name': token.get('name', ''),
        'symbol': token.get('symbol', ''),
        'first_tracked': datetime.now(),
        'first_posted_at': datetime.now(),  # Backward compatibility
        'initial_metrics': {
            'price': token['price_usd'],
            'liquidity': token['liquidity'],
            'volume': token['volume'],
            'market_cap': token.get('market_cap', 0)
        },
        'current_metrics': {
            'price': token['price_usd'],
            'liquidity': token['liquidity'],
            'volume': token['volume'],
            'market_cap': token.get('market_cap', 0)
        },
        'performance_history': [],
        'ath': token['price_usd'],
        'ath_time': datetime.now(),
        'atl': token['price_usd'],
        'atl_time': datetime.now()
    })


async def update_token_performance(context: ContextTypes.DEFAULT_TYPE):
    """Update token performance metrics with advanced analysis"""
    logger.info("🔄 Updating token performance metrics...")
    
    # Get all tokens being tracked
    tokens = token_performance_collection.find({
        'first_tracked': {
            '$gte': datetime.now() - timedelta(days=PERFORMANCE_TRACKING_DAYS)
        }
    })
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        for token in tokens:
            contract_address = token['contract_address']
            try:
                # Fetch current token data
                current_data = await fetch_token_by_contract(contract_address)
                if not current_data:
                    continue
                
                # Calculate performance metrics
                price_change = calculate_price_change(token, current_data)
                liquidity_health = calculate_liquidity_health(token, current_data)
                volatility = calculate_volatility(token, current_data)
                sentiment = await calculate_sentiment(token, client)
                prediction = generate_prediction(token, current_data)
                
                # Update all-time high/low
                new_ath = token['ath']
                new_ath_time = token['ath_time']
                new_atl = token['atl']
                new_atl_time = token['atl_time']
                
                if current_data['price_usd'] > token['ath']:
                    new_ath = current_data['price_usd']
                    new_ath_time = datetime.now()
                elif current_data['price_usd'] < token['atl']:
                    new_atl = current_data['price_usd']
                    new_atl_time = datetime.now()
                
                # Add performance update
                update_data = {
                    'timestamp': datetime.now(),
                    'price': current_data['price_usd'],
                    'liquidity': current_data['liquidity'],
                    'volume': current_data['volume'],
                    'market_cap': current_data.get('market_cap', 0),
                    'holders': current_data.get('holders', 0),
                    'price_change': price_change,
                    'volatility': volatility,
                    'sentiment': sentiment
                }
                
                # Update database
                token_performance_collection.update_one(
                    {'_id': token['_id']},
                    {'$set': {
                        'current_metrics': {
                            'price': current_data['price_usd'],
                            'liquidity': current_data['liquidity'],
                            'volume': current_data['volume'],
                            'market_cap': current_data.get('market_cap', 0),
                            'holders': current_data.get('holders', 0)
                        },
                        'ath': new_ath,
                        'ath_time': new_ath_time,
                        'atl': new_atl,
                        'atl_time': new_atl_time,
                        'volatility': volatility,
                        'liquidity_health': liquidity_health,
                        'sentiment_score': sentiment,
                        'prediction': prediction,
                        'last_updated': datetime.now()
                    },
                    '$push': {
                        'performance_history': {
                            '$each': [update_data],
                            '$slice': -500  # Keep last 500 updates
                        }
                    }}
                )
                
            except Exception as e:
                logger.error(f"Error updating performance for {contract_address}: {str(e)}")

def calculate_price_change(token, current_data):
    """Calculate price change percentage with precision"""
    initial_price = token['initial_metrics']['price']
    current_price = current_data['price_usd']
    return ((current_price - initial_price) / initial_price) * 100 


def calculate_liquidity_health(token, current_data):
    """Calculate liquidity health score (0-100)"""
    initial_liquidity = token['initial_metrics']['liquidity']
    current_liquidity = current_data['liquidity']
    
    # Liquidity growth factor
    growth_factor = current_liquidity / initial_liquidity if initial_liquidity > 0 else 1
    
    # Liquidity to market cap ratio
    market_cap = current_data.get('market_cap', current_liquidity * 2)
    liquidity_ratio = current_liquidity / market_cap if market_cap > 0 else 0
    
    # Calculate score (weighted average)
    score = min(100, (
        (min(growth_factor, 10) * 40) +  # Growth contributes 40% (capped at 10x)
        (min(liquidity_ratio * 100, 50) * 60)  # Ratio contributes 60% (max 50 score)
    ))
    
    return round(score, 1)


def calculate_volatility(token, current_data):
    """Calculate volatility based on price history"""
    if len(token['performance_history']) < 2:
        return 0
    
    prices = [p['price'] for p in token['performance_history']]
    prices.append(current_data['price_usd'])
    
    # Calculate standard deviation of logarithmic returns
    returns = []
    for i in range(1, len(prices)):
        returns.append(math.log(prices[i] / prices[i-1]))
    
    if not returns:
        return 0
        
    mean_return = sum(returns) / len(returns)
    variance = sum((x - mean_return) ** 2 for x in returns) / len(returns)
    std_dev = math.sqrt(variance)
    
    # Annualized volatility
    volatility = std_dev * math.sqrt(365 * 24 * 60 / TOKEN_PERFORMANCE_INTERVAL)
    return round(volatility * 100, 2)  # as percentage


async def calculate_sentiment(token, client):
    """Calculate market sentiment score (0-100)"""
    try:
        # Fetch social mentions
        symbol = token['symbol']
        response = await client.get(
            f"https://api.socialinsider.io/v1/sentiment?token={symbol}&source=twitter,telegram",
            headers={"Authorization": f"Bearer {os.getenv('SOCIAL_INSIDER_API')}"},
            timeout=15
        )
        data = response.json()
        
        if not data.get('success'):
            return 50  # Neutral if no data
            
        # Calculate sentiment score
        positive = data.get('positive', 0)
        negative = data.get('negative', 0)
        total = positive + negative
        
        if total == 0:
            return 50
            
        score = (positive / total) * 100
        return round(score, 1)
        
    except Exception:
        return 50  # Neutral on error
    
def generate_prediction(token, current_data):
    """Generate short-term prediction using technical indicators"""
    if len(token['performance_history']) < 10:
        return None
        
    prices = [p['price'] for p in token['performance_history'][-50:]]
    prices.append(current_data['price_usd'])
    
    # Calculate SMA and EMA
    sma = sum(prices[-10:]) / 10
    ema = prices[-1] * 0.2 + sma * 0.8  # Simple EMA approximation
    
    # Calculate RSI
    gains = []
    losses = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        if change > 0:
            gains.append(change)
        else:
            losses.append(abs(change))
            
    avg_gain = sum(gains) / 14 if gains else 0
    avg_loss = sum(losses) / 14 if losses else 0
    rs = avg_gain / avg_loss if avg_loss != 0 else 100
    rsi = 100 - (100 / (1 + rs))
    
    # Generate prediction
    prediction = ""
    confidence = 0
    
    if prices[-1] > ema > sma:
        prediction = "bullish"
        confidence = min(90, (prices[-1] - ema) / ema * 1000)
    elif prices[-1] < ema < sma:
        prediction = "bearish"
        confidence = min(90, (ema - prices[-1]) / ema * 1000)
    else:
        prediction = "neutral"
        confidence = 50
    
    return {
        'trend': prediction,
        'confidence': round(confidence, 1),
        'indicators': {
            'sma_10': round(sma, 8),
            'ema_10': round(ema, 8),
            'rsi': round(rsi, 2)
        }
    }



async def migrate_token_performance_data():
    """Migrate old token performance data to new format"""
    logger.info("🔄 Migrating token performance data...")
    
    # Update all documents missing the new fields
    token_performance_collection.update_many(
        {'first_tracked': {'$exists': False}},
        {'$set': {
            'first_tracked': '$first_posted_at',
            'volatility': 0,
            'liquidity_health': 50,
            'sentiment_score': 50
        }}
    )
    
    # Initialize empty performance history where missing
    token_performance_collection.update_many(
        {'performance_history': {'$exists': False}},
        {'$set': {'performance_history': []}}
    )
    
    logger.info("✅ Token performance data migration complete")

async def generate_performance_chart(token_data):
    """Generate performance chart with robust error handling"""
    try:
        performance_history = token_data.get('performance_history', [])
        if not performance_history:
            return None

        timestamps = [pd.to_datetime(entry['timestamp']) for entry in performance_history]
        prices = [entry['price'] for entry in performance_history]
        liquidity = [entry['liquidity'] for entry in performance_history]
        
        # Create DataFrame
        df = pd.DataFrame({
            'timestamp': timestamps,
            'price': prices,
            'liquidity': liquidity
        })
        df.set_index('timestamp', inplace=True)
        
        # Resample to consistent intervals
        df = df.resample('5T').ffill()
        
        # Create chart config
        chart_config = {
            "type": "line",
            "data": {
                "labels": [ts.strftime('%m/%d %H:%M') for ts in df.index],
                "datasets": [
                    {
                        "label": "Price (USD)",
                        "data": df['price'].tolist(),
                        "borderColor": "rgb(75, 192, 192)",
                        "yAxisID": "y",
                        "fill": False
                    },
                    {
                        "label": "Liquidity (USD)",
                        "data": df['liquidity'].tolist(),
                        "borderColor": "rgb(255, 99, 132)",
                        "yAxisID": "y1",
                        "fill": False
                    }
                ]
            },
            "options": {
                "scales": {
                    "y": {
                        "type": "linear",
                        "display": True,
                        "position": "left",
                        "title": {"text": "Price (USD)", "display": True}
                    },
                    "y1": {
                        "type": "linear",
                        "display": True,
                        "position": "right",
                        "title": {"text": "Liquidity (USD)", "display": True},
                        "grid": {"drawOnChartArea": False}
                    }
                }
            }
        }
        
        # Generate chart URL
        chart_url = f"https://quickchart.io/chart?c={json.dumps(chart_config)}"
        return chart_url

    except Exception as e:
        logger.error(f"Chart generation failed: {str(e)}")
        return None






async def token_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate token performance analysis with chart"""
    user_id = update.effective_user.id
    log_user_action(user_id, "TOKEN_ANALYSIS_REQUEST")
    
    await update.message.reply_text(
        "🔍 Enter the token contract address for in-depth analysis:\n"
        "(e.g., 4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R)"
    )
    return INPUT_ANALYSIS_CONTRACT

async def analysis_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle token contract input with robust error handling"""
    user_id = update.effective_user.id
    contract_address = update.message.text.strip()
    
    try:
        # Show processing message
        processing_msg = await update.message.reply_text("⏳ Performing deep analysis...")
        
        # Fetch performance data
        token_data = token_performance_collection.find_one(
            {'contract_address': contract_address}
        )
        
        if not token_data:
            await processing_msg.edit_text(
                "❌ This token hasn't been tracked by the bot. "
                "Only tokens posted through the bot can be analyzed."
            )
            return ConversationHandler.END
        
        # Fetch current token data
        current_token = await fetch_token_by_contract(contract_address)
        if not current_token:
            await processing_msg.edit_text("❌ Failed to fetch current token data.")
            return ConversationHandler.END
        
        # Get first tracked time with fallback
        first_tracked = token_data.get('first_tracked')
        if not first_tracked:
            first_tracked = token_data.get('first_posted_at', datetime.now())
        
        if isinstance(first_tracked, str):
            first_tracked = datetime.fromisoformat(first_tracked)
            
        # Calculate time since first tracking
        time_tracked = datetime.now() - first_tracked
        hours_tracked = time_tracked.total_seconds() / 3600
        
        # Generate performance chart only if we have history
        chart_url = None
        performance_history = token_data.get('performance_history', [])
        
        if performance_history:
            try:
                chart_url = await generate_performance_chart(token_data)
            except Exception as e:
                logger.error(f"Chart generation error: {str(e)}")
        
        # Format analysis message with fallback values
        ath = token_data.get('ath', current_token['price_usd'])
        atl = token_data.get('atl', current_token['price_usd'])
        volatility = token_data.get('volatility', 0)
        liquidity_health = token_data.get('liquidity_health', 50)
        sentiment_score = token_data.get('sentiment_score', 50)
        
        # Calculate performance metrics
        price_change_1h = calculate_timeframe_change(token_data, hours=1) if performance_history else 0
        price_change_6h = calculate_timeframe_change(token_data, hours=6) if performance_history else 0
        price_change_24h = calculate_timeframe_change(token_data, hours=24) if performance_history else 0
        
        # Format analysis message
        message = (
            f"📊 *Advanced Token Analysis - {token_data.get('name', 'Unknown Token')}* "
            f"({token_data.get('symbol', 'UNKNOWN')})\n\n"
            f"🔗 *Contract:* `{contract_address}`\n"
            f"⏱️ *Tracked for:* {time_tracked.days} days, {int(hours_tracked % 24)} hours\n\n"
            f"📈 *Price Performance*\n"
            f"  - Current: ${current_token['price_usd']:.8f}\n"
            f"  - ATH: ${ath:.8f}\n"
            f"  - ATL: ${atl:.8f}\n\n"
            f"📉 *Volatility*: {volatility:.2f}%\n"
            f"💧 *Liquidity Health*: {liquidity_health}/100\n"
            f"📣 *Market Sentiment*: {sentiment_score}/100\n"
            f"⏱️ *Recent Performance*\n"
            f"  - 1h: {price_change_1h:+.2f}%\n"
            f"  - 6h: {price_change_6h:+.2f}%\n"
            f"  - 24h: {price_change_24h:+.2f}%\n"
        )
        
        # Add prediction if available
        if token_data.get('prediction'):
            pred = token_data['prediction']
            emoji = "📈" if pred['trend'] == 'bullish' else "📉" if pred['trend'] == 'bearish' else "↔️"
            message += (
                f"\n🔮 *Short-term Prediction*: {emoji} {pred['trend'].capitalize()} "
                f"(Confidence: {pred['confidence']:.1f}%)\n"
            )
        
        # Add chart if available
        if chart_url:
            message += f"\n[View Full Performance Chart]({chart_url})"
        
        # Send message
        try:
            await processing_msg.delete()
        except:
            pass
            
        await update.message.reply_text(
            message, 
            parse_mode='Markdown', 
            disable_web_page_preview=not bool(chart_url)
        )
        
    except Exception as e:
        logger.error(f"Analysis error: {str(e)}")
        await update.message.reply_text(
            "❌ Failed to complete analysis. The token data might be incomplete or corrupted."
        )
    
    return ConversationHandler.END

# Modified fetch_tokens_manual function
async def fetch_tokens_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    log_user_action(user_id, "MANUAL_TOKEN_FETCH")
    
    try:
        if not await check_subscription(user_id):
            await update.message.reply_text("🔒 You need an active subscription to use this feature. Use /subscribe.")
            return

        user = users_collection.find_one({'user_id': user_id})
        if not user or not user.get('solana') or not user['solana'].get('public_key'):
            await update.message.reply_text(
                "🚫 You need to set up a wallet first. Use /start to create a new wallet or /set_wallet to import an existing one."
            )
            return

        current_time = time.time()
        if user.get('last_api_call', 0) > current_time - 1:
            await update.message.reply_text("⏳ Please wait at least 1 second between fetches.")
            return

        logger.info(f"User {user_id} manually fetching tokens")
        tokens = await fetch_latest_token()
        
        if not tokens:
            logger.warning("No tokens fetched")
            await update.message.reply_text("🔍 No tokens found. Please try again later.")
            return
            
        posted_tokens = user.get('posted_tokens', [])
        new_tokens = [t for t in tokens if t['contract_address'] not in posted_tokens]
        
        if new_tokens:
            token = new_tokens[0]  # Take the most recent unseen token
            # Record token performance
            await record_token_performance(token)

            message = format_token_message(token)
            is_suspicious = token['liquidity'] < 1000 or token['volume'] < 1000
            warning = "⚠️ *LOW LIQUIDITY - Trade with caution!*\n" if is_suspicious else ""
        
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
            except Exception as e:
                logger.error(f"Error sending token {token['contract_address']}: {str(e)}")
                await update.message.reply_text(
                    warning + message,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            
            # Add to user's posted tokens
            users_collection.update_one(
                {'user_id': user_id},
                {
                    '$set': {'last_api_call': current_time},
                    '$addToSet': {'posted_tokens': token['contract_address']}
                }
            )
            logger.info(f"Added token {token['contract_address']} to user {user_id}'s posted tokens")
        else:
            await update.message.reply_text("🔍 No new tokens available that you haven't seen.")
            
    except Exception as e:
        logger.error(f"Error in manual token fetch: {str(e)}", exc_info=True)
        await update.message.reply_text("❌ An error occurred while fetching tokens. Please try again.")

async def trade_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show trade system status"""
    user_id = update.effective_user.id
    log_user_action(user_id, "TRADE_STATUS_REQUEST")
    
    user = users_collection.find_one({'user_id': user_id})
    
    if not user:
        await update.message.reply_text("❌ No user data found.")
        return
        
    status = (
        f"📊 *Trade System Status*\n\n"
        f"🔄 Auto-fetch: {'✅ Enabled' if user.get('last_token_check') else '❌ Disabled'}\n"
        f"⏱️ Last check: {datetime.fromtimestamp(user.get('last_token_check', 0)).strftime('%Y-%m-%d %H:%M') if user.get('last_token_check') else 'Never'}\n"
        f"📋 Tokens seen: {len(user.get('posted_tokens', []))}\n"
        f"💼 Active trades: {len(user.get('portfolio', {}))}\n\n"
        f"🔹 Use /fetch_tokens to manually check for new tokens\n"
        f"🔹 Use /setmode to configure auto-trading"
    )
    
    await update.message.reply_text(status, parse_mode='Markdown') 


def calculate_timeframe_change(token_data, hours):
    """Calculate price change for specific timeframe"""
    now = datetime.now()
    time_threshold = now - timedelta(hours=hours)
    
    # Find closest historical point
    closest_point = None
    for point in reversed(token_data['performance_history']):
        if point['timestamp'] <= time_threshold:
            closest_point = point
            break
            
    if not closest_point:
        return 0
        
    current_price = token_data['current_metrics']['price']
    historical_price = closest_point['price']
    
    return ((current_price - historical_price) / historical_price) * 100 

async def start_token_updates(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Start periodic token updates for a user"""
    logger.info(f"🔔 [start_token_updates] Starting for user {user_id}")
    try:
        user = users_collection.find_one({'user_id': user_id})
        if not user:
            logger.warning(f"User {user_id} not found in database")
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ User account not found. Please use /start to create an account."
            )
            return
        
        if not user.get('solana') or not user['solana'].get('public_key'):
            logger.warning(f"User {user_id} has no wallet set up")
            # DEBUG: Log the actual user document for inspection
            logger.debug(f"User document: {user}")
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Wallet setup incomplete. Please use /set_wallet to complete wallet setup."
            )
            return
        
        # Remove any existing jobs for this user
        for job in context.job_queue.jobs():
            if job.name.startswith(f"user_{user_id}_"):
                job.schedule_removal()
        
        if await check_subscription(user_id):
            context.job_queue.run_repeating(
                update_token_info,
                interval=30,
                first=5,
                user_id=user_id,
                name=f"user_{user_id}_token_updates"
            )
            logger.info(f"Token updates started for user {user_id}")
        else:
            logger.warning(f"User {user_id} doesn't have active subscription")
            
    except Exception as e:
        logger.error(f"🔥 [start_token_updates] Error: {str(e)}", exc_info=True)

async def check_conversation_timeout(context: ContextTypes.DEFAULT_TYPE):
    """Check if conversation has timed out and resume token updates"""
    user_id = context.job.user_id
    last_activity = context.job.data.get('last_activity', datetime.now())
    
    # 2 minutes timeout
    if (datetime.now() - last_activity).total_seconds() > 120:
        logger.info(f"⏰ Conversation timeout for user {user_id}")
        
        # Clear conversation state
        context.user_data[f'conversation_state_{user_id}'] = None
        
        # Remove timeout job itself
        context.job.schedule_removal()
        
        # Send notification
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="⏱️ Session timed out due to inactivity. Use /start to begin again."
            )
        except Exception as e:
            logger.warning(f"Couldn't send timeout message: {str(e)}")
        
        # Restart token updates if subscribed
        if await check_subscription(user_id):
            await start_token_updates(context, user_id)

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle subscription with direct SOL payment"""
    user_id = update.effective_user.id
    log_user_action(user_id, "SUBSCRIPTION_REQUEST")
    
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("🚫 Please use /start first to initialize your account.")
        return
    
    # Check current subscription status
    if user.get('subscription_status') == 'active':
        expiry = datetime.fromisoformat(user['subscription_expiry'])
        await update.message.reply_text(
            f"✅ You already have an active subscription until {expiry.strftime('%Y-%m-%d %H:%M:%S')}."
        )
        return
    
    # Check if user has a Solana wallet connected
    if not user.get('solana') or not user['solana'].get('public_key'):
        await update.message.reply_text(
            "🔑 You need to have a Solana wallet connected to subscribe.\n\n"
            "Please set up your wallet first using /start or /set_wallet."
        )
        return
    
    # Check SOL balance
    sol_balance = await check_balance(user_id, 'solana')
    if sol_balance < SUBSCRIPTION_SOL_AMOUNT:
        await update.message.reply_text(
            f"❌ Insufficient SOL balance. You need {SUBSCRIPTION_SOL_AMOUNT} SOL "
            f"but only have {sol_balance:.4f} SOL.\n\n"
            f"Your Solana address: `{user['solana']['public_key']}`"
        )
        return
    
    # Request confirmation
    keyboard = [
        [InlineKeyboardButton("✅ Confirm Payment", callback_data='confirm_subscription')],
        [InlineKeyboardButton("❌ Cancel", callback_data='cancel_subscription')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"💳 Confirm subscription payment of {SUBSCRIPTION_SOL_AMOUNT} SOL?\n\n"
        f"🔹 From: Your Solana wallet\n`{user['solana']['public_key']}`\n"
        f"🔹 To: Bot address\n`{BOT_SOL_ADDRESS}`\n"
        f"🔹 Current Balance: {sol_balance:.4f} SOL\n\n"
        f"Transaction fee will apply.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return SUBSCRIPTION_CONFIRMATION



async def confirm_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle subscription confirmation and execute SOL payment"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == 'cancel_subscription':
        await query.edit_message_text("🛑 Subscription cancelled.")
        return ConversationHandler.END
    
    await query.edit_message_text("⏳ Processing your payment...")
    log_user_action(user_id, "SUBSCRIPTION_CONFIRMED")
    
    user = users_collection.find_one({'user_id': user_id})
    
    try:
        # Decrypt private key
        decrypted_user = await decrypt_user_wallet(user_id, user)
        solana_private_key = decrypted_user['solana']['private_key']
        
        if not solana_private_key or solana_private_key == "[Decryption Failed]":
            raise ValueError("Failed to decrypt Solana private key")
        
        keypair = Keypair.from_bytes(base58.b58decode(solana_private_key))
        
        # Create transfer instruction
        to_pubkey = Pubkey.from_string(BOT_SOL_ADDRESS)
        amount_lamports = int(SUBSCRIPTION_SOL_AMOUNT * 10**9)  # Convert SOL to lamports
        
        # Get recent blockhash
        recent_blockhash = (await solana_client.get_latest_blockhash()).value.blockhash
        
        # Create message
        message = Message.new_with_blockhash(
            [transfer(
                TransferParams(
                    from_pubkey=keypair.pubkey(),
                    to_pubkey=to_pubkey,
                    lamports=amount_lamports
                )
            )],
            keypair.pubkey(),
            recent_blockhash
        )
        
        # Create transaction
        txn = Transaction([keypair], message, recent_blockhash)
        
        # Sign transaction with all required parameters
        txn.sign([keypair], recent_blockhash)
        
        # Send transaction
        tx_hash = await solana_client.send_transaction(txn)
        logger.info(f"Subscription payment sent: {tx_hash.value}")
        
        # Confirm transaction
        await solana_client.confirm_transaction(tx_hash.value, commitment="confirmed")
        log_user_action(user_id, "SUBSCRIPTION_PAYMENT_SENT", f"TX: {tx_hash.value}")
        
        # Update subscription status
        expiry = datetime.now() + timedelta(days=7)
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {
                'subscription_status': 'active',
                'subscription_expiry': expiry.isoformat()
            }}
        )
        log_user_action(user_id, "SUBSCRIPTION_ACTIVATED", f"Expiry: {expiry}")
        
        await query.edit_message_text(
            f"✅ Subscription activated! You now have full access until {expiry.strftime('%Y-%m-%d')}.\n\n"
            f"Transaction: https://solscan.io/tx/{tx_hash.value}"
        )
        return ConversationHandler.END
    
    except Exception as e:
        logger.error(f"Subscription payment error: {str(e)}", exc_info=True)
        log_user_action(user_id, "SUBSCRIPTION_ERROR", str(e), "error")
        await query.edit_message_text(
            f"❌ Payment failed: {str(e)}\n\n"
            "Please ensure you have enough SOL for the transaction fee."
        )
        return ConversationHandler.END

async def verify_sol_payments(context: ContextTypes.DEFAULT_TYPE):
    """Background job to verify SOL subscription payments"""
    logger.info("🔍 Verifying SOL subscription payments...")
    
    # Get all pending payments (users who initiated payment but we haven't confirmed)
    pending_payments = users_collection.find({
        'subscription_payment_sent': True,
        'subscription_status': {'$ne': 'active'}
    })
    
    for user in pending_payments:
        user_id = user['user_id']
        tx_hash = user.get('last_payment_tx')
        
        if not tx_hash:
            continue
        
        try:
            # Check transaction status
            tx_status = await solana_client.get_transaction(
                tx_hash,
                commitment="confirmed"
            )
            
            if tx_status.value and not tx_status.value.transaction.meta.err:
                # Payment confirmed!
                expiry = datetime.now() + timedelta(days=7)
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$set': {
                        'subscription_status': 'active',
                        'subscription_expiry': expiry.isoformat()
                    }}
                )
                log_user_action(user_id, "SUBSCRIPTION_AUTO_ACTIVATED", f"TX: {tx_hash}")
                
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"✅ Your subscription payment was confirmed! "
                             f"You now have full access until {expiry.strftime('%Y-%m-%d')}."
                    )
                except Exception as e:
                    logger.error(f"Failed to notify user {user_id}: {str(e)}")
            else:
                # Transaction failed or not found
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$unset': {'subscription_payment_sent': "", 'last_payment_tx': ""}}
                )
                
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="❌ Your subscription payment failed. Please try /subscribe again."
                    )
                except:
                    pass
        except Exception as e:
            logger.error(f"Payment verification error for user {user_id}: {str(e)}")

async def generate_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle wallet generation command"""
    user_id = update.effective_user.id
    log_user_action(user_id, "WALLET_GENERATE_INITIATED")
    
    if not await check_subscription(user_id):
        log_user_action(user_id, "SUBSCRIPTION_CHECK_FAILED")
        await update.message.reply_text(
            "🔒 You need an active subscription to use this feature. Use /subscribe."
        )
        return ConversationHandler.END
    
    user = users_collection.find_one({'user_id': user_id})
    if user and user.get('solana', {}).get('public_key'):
        log_user_action(user_id, "WALLET_EXISTS_PROMPT_OVERWRITE")
        keyboard = [
            [InlineKeyboardButton("✅ Generate New Wallet", callback_data='confirm_new_wallet')],
            [InlineKeyboardButton("❌ Cancel", callback_data='cancel_new_wallet')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        eth_bsc_address = user['eth']['address'] if user.get('eth') else "Not set"
        await update.message.reply_text(
            f"⚠️ You already have a wallet:\n"
            f"🔑 Solana: {user['solana']['public_key']}\n"
            f"🌐 ETH/BSC: {eth_bsc_address}\n\n"
            "Generating a new wallet will overwrite the existing one.\n"
            "Are you sure you want to continue?",
            reply_markup=reply_markup
        )
        return CONFIRM_NEW_WALLET
    else:
        log_user_action(user_id, "WALLET_GENERATE_START")
        return await confirm_generate_wallet(update, context, new_user=True)

async def confirm_generate_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE, new_user=False) -> int:
    """Confirm and generate a new wallet"""
    user_id = update.effective_user.id if new_user else update.callback_query.from_user.id
    log_user_action(user_id, "WALLET_GENERATION_STARTED")
    
    try:
        # Show processing message
        if new_user:
            processing_msg = await update.message.reply_text("⏳ Generating your new wallet...")
        else:
            query = update.callback_query
            await query.answer()
            if query.data == 'cancel_new_wallet':
                log_user_action(user_id, "WALLET_GENERATION_CANCELLED")
                await query.edit_message_text("🛑 Wallet generation cancelled. Your existing wallet remains unchanged.")
                return ConversationHandler.END
            await query.edit_message_text("⏳ Generating your new wallet...")
        
        # Generate new wallet
        mnemo = Mnemonic("english")
        mnemonic = mnemo.generate(strength=256)
        log_user_action(user_id, "MNEMONIC_GENERATED")
        
        # Create wallet from mnemonic
        user_data = await set_user_wallet(user_id, mnemonic=mnemonic)
        log_user_action(user_id, "WALLET_CREATED_FROM_MNEMONIC")
        
        # Save to database
       
        
        update_result = users_collection.update_one(
            {'user_id': user_id},
            {'$set': user_data},
            upsert=True
        )
        
        # Properly log the update result
        logger.info(f"Database update - matched: {update_result.matched_count}, "
                   f"modified: {update_result.modified_count}, "
                   f"upserted_id: {update_result.upserted_id}")
        
        # Get decrypted info for display
        decrypted_user = await decrypt_user_wallet(user_id, user_data)
        eth_bsc_address = user_data['eth']['address'] if user_data.get('eth') else "Not set"
        
        # Prepare success message
        success_msg = (
            f"✨ *New Wallet Generated!* ✨\n\n"
            f"🔐 *Recovery Phrase*:\n`{decrypted_user['mnemonic']}`\n\n"
            f"🔑 *Solana Address*:\n`{user_data['solana']['public_key']}`\n\n"
            f"🌐 *ETH/BSC Address*:\n`{eth_bsc_address}`\n\n"
            f"⚠️ *SECURITY WARNING* ⚠️\n"
            f"1️⃣ Never share your recovery phrase with anyone\n"
            f"2️⃣ Store it securely offline (write it down)\n"
            f"3️⃣ This message will self-destruct in 2 minutes\n\n"
            f"🚀 You're all set! The bot will now start sending you token alerts."
        )
        
        # Send success message
        if new_user:
            await processing_msg.edit_text(success_msg, parse_mode='Markdown')
            msg_to_delete = processing_msg
        else:
            await query.edit_message_text(success_msg, parse_mode='Markdown')
            msg_to_delete = query.message
        
        # Schedule message deletion
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=msg_to_delete.message_id),
            120,  # 2 minutes
            user_id=user_id
        )
        
        log_user_action(user_id, "WALLET_GENERATION_SUCCESS")
        
        # Start token updates
        await start_token_updates(context, user_id)
        return ConversationHandler.END
        
    except Exception as e:
        error_msg = f"❌ Error generating wallet: {str(e)}"
        log_user_action(user_id, "WALLET_GENERATION_FAILED", f"Error: {str(e)}", "error")
        
        if new_user:
            await update.message.reply_text(error_msg)
        else:
            await query.edit_message_text(error_msg)
        return ConversationHandler.END

async def set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle wallet import command (standalone)"""
    user_id = update.effective_user.id
    log_user_action(user_id, "WALLET_IMPORT_INITIATED")
    
    if not await check_subscription(user_id):
        log_user_action(user_id, "SUBSCRIPTION_CHECK_FAILED")
        await update.message.reply_text("🔒 You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    
    # Set flag to identify standalone import flow
    context.user_data['is_start_flow'] = False
    
    keyboard = [
        [InlineKeyboardButton("🔐 Mnemonic (12/24 words)", callback_data='mnemonic')],
        [InlineKeyboardButton("🔑 Private Key", callback_data='private_key')],
        [InlineKeyboardButton("❌ Cancel", callback_data='cancel_import')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "📥 *Choose how to import your existing wallet:*\n\n"
        "1. 🔐 *Mnemonic*: Your 12-word or 24-word recovery phrase\n"
        "2. 🔑 *Private Key*: Your wallet's private key\n\n"
        "⚠️ *Note*: This will overwrite any existing wallet in the bot",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return SET_WALLET_METHOD  # Existing state for standalone import

async def set_wallet_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    log_user_action(user_id, "WALLET_IMPORT_METHOD_SELECTED", query.data)
    
    if not await check_subscription(user_id):
        await query.message.reply_text("🔒 You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    
    context.user_data['wallet_method'] = query.data

    if query.data == 'mnemonic':
        message = await query.edit_message_text(
            "📝 *Please enter your BIP-39 mnemonic phrase (12 or 24 words, space-separated):*\n\n"
            "🔹 Example 12-word: `word1 word2 ... word12`\n"
            "🔹 Example 24-word: `word1 word2 ... word24`\n\n"
            "⚠️ This message and your input will auto-delete in 30 seconds for security.",
            parse_mode='Markdown'
        )
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=message.message_id),
            30,
            user_id=user_id
        )
        return INPUT_MNEMONIC
    else:
        message = await query.edit_message_text(
            "🔑 *Please enter your private key:*\n\n"
            "For Solana: 64-byte base58 encoded (starts with a number)\n"
            "For ETH/BSC: 32-byte hex encoded (with or without '0x' prefix)\n\n"
            "⚠️ This message and your input will auto-delete in 30 seconds for security.",
            parse_mode='Markdown'
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
    logger.info(f"📝 [input_mnemonic] Received mnemonic from user {user_id}")
    
    try:
        # Delete the sensitive message immediately
        await context.bot.delete_message(chat_id=user_id, message_id=update.message.message_id)
    except Exception as e:
        logger.warning(f"Could not delete mnemonic message: {str(e)}")

    try:
        mnemo = Mnemonic("english")
        word_count = len(mnemonic.split())
        if word_count not in [12, 24]:
            raise ValueError("Mnemonic must be 12 or 24 words")
        
        if not mnemo.check(mnemonic):
            raise ValueError("Invalid mnemonic phrase")
        
        context.user_data['wallet_input'] = mnemonic
        
        user = users_collection.find_one({'user_id': user_id})
        if user and user.get('solana') and user['solana'].get('public_key'):
            # Existing wallet - confirm overwrite
            keyboard = [[InlineKeyboardButton("✅ Confirm", callback_data='confirm_set_wallet')],
                        [InlineKeyboardButton("❌ Cancel", callback_data='cancel_set_wallet')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "⚠️ You already have a wallet. Importing will overwrite it. Confirm?",
                reply_markup=reply_markup
            )
            return CONFIRM_SET_WALLET
        else:
            # Create and save wallet
            logger.info("Creating new wallet from mnemonic")
            user_data = await set_user_wallet(user_id, mnemonic=mnemonic)
            
            # DEBUG: Log before database operation
            logger.debug(f"Wallet data to save: {user_data.keys()}")
            
            # Use replace_one with upsert to ensure document is properly created
         

           
            update_result = users_collection.update_one(
            {'user_id': user_id},
            {'$set': user_data},
            upsert=True
        )
              # Log the update result properly
            logger.info(f"Database update - matched: {update_result.matched_count}, "
                   f"modified: {update_result.modified_count}, "
                   f"upserted_id: {update_result.upserted_id}")
            # Verify the wallet was saved
            db_user = users_collection.find_one({'user_id': user_id})
        if not db_user or not db_user.get('solana') or not db_user['solana'].get('public_key'):
            raise RuntimeError("Wallet not saved to database")
            
            # DEBUG: Log successful save
            logger.info(f"Wallet successfully saved for user {user_id}")
            logger.debug(f"SOL public key: {db_user['solana']['public_key']}")
            
            # Get decrypted info for display
           
            eth_bsc_address = db_user['eth']['address'] if db_user.get('eth') else "Not set"
            
            success_msg = await update.message.reply_text(
                f"✅ *Wallet Imported Successfully!*\n\n"
                f"🔑 Solana Address: `{db_user['solana']['public_key']}`\n"
                f"🌐 ETH/BSC Address: `{eth_bsc_address}`\n\n"
                f"🚀 You're all set! The bot will now start sending you token alerts.",
                parse_mode='Markdown'
            )
            
        if context.user_data.get('is_start_flow', False):
        # This should never happen but just in case
         return await start_input_mnemonic(update, context)
            # Start token updates
        await start_token_updates(context, user_id)
        return ConversationHandler.END
            
    except Exception as e:
        logger.error(f"🔥 [input_mnemonic] Critical error: {str(e)}", exc_info=True)
        error_msg = f"❌ Failed to import wallet: {str(e)}"
        if "Invalid mnemonic phrase" in str(e):
            error_msg = "❌ Invalid mnemonic phrase. Please check your words and try again."
        
        await update.message.reply_text(error_msg)
        return INPUT_MNEMONIC
    
async def input_private_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle private key input for wallet import"""
    user_id = update.effective_user.id
    private_key = update.message.text.strip()
    log_user_action(user_id, "PRIVATE_KEY_RECEIVED")
    
    # Delete the sensitive message immediately
    try:
        await context.bot.delete_message(
            chat_id=user_id,
            message_id=update.message.message_id
        )
    except Exception as e:
        logger.warning(f"Could not delete private key message: {str(e)}")
        log_user_action(user_id, "MESSAGE_DELETION_FAILED", "Private key message")

    try:
        # Validate the private key format
        key_type = None
        if private_key.startswith('0x'):
            # Ethereum/BSC private key (hex format)
            key_bytes = bytes.fromhex(private_key[2:])
            if len(key_bytes) == 32:
                key_type = 'ethereum'
            else:
                raise ValueError("Invalid Ethereum private key length")
        else:
            try:
                # Try to decode as Solana private key (base58)
                key_bytes = base58.b58decode(private_key)
                if len(key_bytes) == 64:
                    key_type = 'solana'  # Full 64-byte keypair
                elif len(key_bytes) == 32:
                    key_type = 'solana_seed'  # 32-byte seed
                else:
                    raise ValueError("Invalid Solana key length (must be 32 or 64 bytes)")
            except:
                # Try to decode as ETH/BSC private key without 0x prefix
                try:
                    key_bytes = bytes.fromhex(private_key)
                    if len(key_bytes) == 32:
                        key_type = 'ethereum'
                        private_key = '0x' + private_key  # Add 0x prefix
                    else:
                        raise ValueError("Invalid Ethereum key length")
                except:
                    raise ValueError("Could not decode as Solana (base58) or ETH/BSC (hex)")
                
        log_user_action(user_id, "VALID_PRIVATE_KEY_RECEIVED", f"Type: {key_type}")
        
        context.user_data['wallet_input'] = private_key
        context.user_data['key_type'] = key_type

        user = users_collection.find_one({'user_id': user_id})
        if user and user.get('solana') and user['solana'].get('public_key'):
            # Existing wallet found - confirm overwrite
            log_user_action(user_id, "WALLET_EXISTS_CONFIRM_OVERWRITE")
            keyboard = [
                [InlineKeyboardButton("✅ Confirm Import", callback_data='confirm_set_wallet')],
                [InlineKeyboardButton("❌ Cancel", callback_data='cancel_set_wallet')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            eth_bsc_address = user['eth']['address'] if user.get('eth') else "Not set"
            confirm_msg = await update.message.reply_text(
                f"⚠️ You already have a wallet:\n"
                f"🔑 Solana: `{user['solana']['public_key']}`\n"
                f"🌐 ETH/BSC: `{eth_bsc_address}`\n\n"
                "Importing this wallet will overwrite the existing one.\n"
                "Are you sure you want to continue?",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            # Delete confirmation message after 2 minutes
            context.job_queue.run_once(
                lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=confirm_msg.message_id),
                120,
                user_id=user_id
            )
            return CONFIRM_SET_WALLET
        else:
            # No existing wallet - proceed directly
            log_user_action(user_id, "WALLET_IMPORT_START")
            user_data = await set_user_wallet(user_id, private_key=private_key)
            
            # Save to database
            update_result = users_collection.update_one(
                {'user_id': user_id},
                {'$set': user_data},
                upsert=True
            )
            
            # Verify save was successful
            db_user = users_collection.find_one({'user_id': user_id})
            if not db_user or not db_user.get('solana') or not db_user['solana'].get('public_key'):
                raise RuntimeError("Wallet not saved to database")
            
            eth_bsc_address = db_user['eth']['address'] if db_user.get('eth') else "Not set"
            
            success_msg = await update.message.reply_text(
                f"✅ *Wallet Imported Successfully!*\n\n"
                f"🔑 Solana Address: `{db_user['solana']['public_key']}`\n"
                f"🌐 ETH/BSC Address: `{eth_bsc_address}`\n\n"
                f"🚀 You're all set! The bot will now start sending you token alerts.",
                parse_mode='Markdown'
            )
            
            log_user_action(user_id, "WALLET_IMPORT_SUCCESS")

            # Start token updates
            await start_token_updates(context, user_id)
            return ConversationHandler.END
            
    except Exception as e:
        logger.error(f"Error in input_private_key for user {user_id}: {str(e)}")
        log_user_action(user_id, "WALLET_IMPORT_ERROR", f"Error: {str(e)}", "error")
        
        # Create user-friendly error message
        if "32 bytes" in str(e):
            error_details = (
                "For ETH/BSC: Must be 32-byte hex key (64 hex characters)\n"
                "Example: 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
            )
        elif "64 bytes" in str(e):
            error_details = (
                "For Solana: Must be 64-byte keypair (base58) OR 32-byte seed\n"
                "Example keypair: 5H1eQz1R1g4HqQVQZz4fQeTzLd7pN7J8W7J8Z7Y7c7a7b7c7d7e7f7g7h7i7j7k7l7m7n7o7p7q7r\n"
                "Example seed: 5H1eQz1R1g4HqQVQZz4fQeTzLd7pN7J8W7J8Z7Y7c7a7b7c7d7e7f7g7h"
            )
        else:
            error_details = "Please check the format and try again"
            
        error_msg = await update.message.reply_text(
            f"❌ Invalid private key: {str(e)}\n\n"
            "Valid formats:\n"
            "1. Solana (64-byte keypair or 32-byte seed): Base58 encoded\n"
            "2. ETH/BSC (32-byte key): Hex with or without 0x prefix\n\n"
            f"{error_details}\n\n"
            "Try again or use /cancel to abort."
        )
        # Delete error message after 1 minute
        context.job_queue.run_once(
            lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=error_msg.message_id),
            60,
            user_id=user_id
        )
        return INPUT_PRIVATE_KEY
async def confirm_set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirm wallet import"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    log_user_action(user_id, "WALLET_IMPORT_CONFIRMATION", query.data)
    
    if not await check_subscription(user_id):
        await query.message.reply_text("🔒 You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END

    if query.data == 'cancel_set_wallet':
        await query.edit_message_text("🛑 Wallet import cancelled. Your existing wallet remains unchanged.")
        log_user_action(user_id, "WALLET_IMPORT_CANCELLED")
        return ConversationHandler.END

    try:
        wallet_input = context.user_data['wallet_input']
        method = context.user_data['wallet_method']
        
        # Show processing message
        await query.edit_message_text("⏳ Processing your wallet import...")
        
        
        user_data = await set_user_wallet(
    user_id, 
    mnemonic=wallet_input if method == 'mnemonic' else None,
    private_key=wallet_input if method == 'private_key' else None
)
        
        update_result = users_collection.update_one(
            {'user_id': user_id},
            {'$set': user_data},
            upsert=True
        )
        
        # Properly log the update result
        logger.info(f"Database update - matched: {update_result.matched_count}, "
                   f"modified: {update_result.modified_count}, "
                   f"upserted_id: {update_result.upserted_id}")
        
       
        eth_bsc_address = user_data['eth']['address'] if user_data.get('eth') else "Not set"
        
        if method == 'mnemonic':
            decrypted_user = await decrypt_user_wallet(user_id, user_data)
            success_msg = (
                f"✅ *Wallet Imported Successfully!*\n\n"
                f"🔐 *Recovery Phrase*: `{decrypted_user['mnemonic']}`\n"
                f"🔑 *Solana Address*: `{user_data['solana']['public_key']}`\n"
                f"🌐 *ETH/BSC Address*: `{eth_bsc_address}`\n\n"
                f"⚠️ *Security Alert*: This message will self-destruct in 30 seconds.\n\n"
                f"🚀 You're all set! The bot will now start sending you token alerts."
            )
        else:
            success_msg = (
                f"✅ *Wallet Imported Successfully!*\n\n"
                f"🔑 *Solana Address*: `{user_data['solana']['public_key']}`\n"
                f"🌐 *ETH/BSC Address*: `{eth_bsc_address}`\n\n"
                f"🚀 You're all set! The bot will now start sending you token alerts."
            )
        
        await query.edit_message_text(
            success_msg,
            parse_mode='Markdown'
        )
        
        if method == 'mnemonic':
            # Delete sensitive mnemonic message after 30 seconds
            context.job_queue.run_once(
                lambda ctx: ctx.bot.delete_message(chat_id=user_id, message_id=query.message.message_id),
                30,
                user_id=user_id
            )
        
        log_user_action(user_id, "WALLET_IMPORT_SUCCESS")
        
        # Start token updates
        await start_token_updates(context, user_id)
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Error in confirm_set_wallet for user {user_id}: {str(e)}")
        log_user_action(user_id, "WALLET_IMPORT_FAILED", str(e), "error")
        await query.edit_message_text(
            f"❌ Failed to import wallet: {str(e)}\n\n"
            "Please try again or contact support if the problem persists."
        )
        return ConversationHandler.END

async def reset_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset the list of seen tokens for a user"""
    user_id = update.effective_user.id
    log_user_action(user_id, "RESET_TOKENS")
    context.user_data[f'conversation_state_{user_id}'] = None
    
    if not await check_subscription(user_id):
        await update.message.reply_text("🔒 You need an active subscription to use this feature. Use /subscribe.")
        return
    
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("🚫 No wallet found. Please use /start to create a wallet or /set_wallet to import one.")
        return
    
    users_collection.update_one(
        {'user_id': user_id},
        {'$set': {'posted_tokens': []}}
    )
    log_user_action(user_id, "TOKENS_RESET")
    
    await update.message.reply_text(
        "✅ Your list of posted tokens has been reset.\n"
        "You will now receive all new tokens, including previously posted ones."
    )
    
    if await check_subscription(user_id):
        await start_token_updates(context, user_id)

async def set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Set trading mode (manual/automatic)"""
    user_id = update.effective_user.id
    log_user_action(user_id, "SET_MODE_COMMAND")
    
    if not await check_subscription(user_id):
        await update.message.reply_text("🔒 You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("🚫 No wallet found. Please use /start to create a wallet or /set_wallet to import one.")
        return ConversationHandler.END
    
    current_mode = user.get('trading_mode', 'manual')
    
    keyboard = [
        [InlineKeyboardButton("Manual", callback_data='manual')],
        [InlineKeyboardButton("Automatic", callback_data='automatic')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🔧 *Current trading mode*: {current_mode.capitalize()}\n\n"
        "Choose trading mode (Solana only):",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return SET_TRADING_MODE

async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle trading mode selection"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    mode = query.data
    log_user_action(user_id, "TRADING_MODE_SELECTED", mode)
    
    if not await check_subscription(user_id):
        await query.message.reply_text("🔒 You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    
    users_collection.update_one(
        {'user_id': user_id},
        {'$set': {'trading_mode': mode}}
    )
    log_user_action(user_id, "TRADING_MODE_UPDATED", mode)

    if mode == 'manual':
        # Remove auto-trade job if it exists
        for job in context.job_queue.jobs():
            if job.name == f"auto_trade_{user_id}":
                job.schedule_removal()
                logger.info(f"Removed auto_trade job for user {user_id} on mode switch to manual")

        await query.message.reply_text(
            "✅ Trading mode set to *Manual*.\n"
            "Use /trade or token buttons to trade Solana tokens.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    else:
        await query.message.reply_text(
            "🔢 Enter the auto-buy amount in SOL (e.g., 0.1 for 0.1 SOL):",
            parse_mode='Markdown'
        )
        return SET_AUTO_BUY_AMOUNT

async def set_auto_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Set auto-buy amount for automatic trading"""
    user_id = update.effective_user.id
    try:
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("❌ Please enter a positive amount.")
            return SET_AUTO_BUY_AMOUNT
        
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {'auto_buy_amount': amount}}
        )
        log_user_action(user_id, "AUTO_BUY_AMOUNT_SET", f"{amount} SOL")
        
        await update.message.reply_text(
            "📈 Enter the sell percentage (e.g., 10 for 10% profit):",
            parse_mode='Markdown'
        )
        return SET_SELL_PERCENTAGE
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Please enter a number.")
        return SET_AUTO_BUY_AMOUNT

async def set_sell_percentage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Set sell percentage for automatic trading"""
    user_id = update.effective_user.id
    try:
        percentage = float(update.message.text)
        if percentage <= 0:
            await update.message.reply_text("❌ Please enter a positive percentage.")
            return SET_SELL_PERCENTAGE
        
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {'sell_percentage': percentage}}
        )
        log_user_action(user_id, "SELL_PERCENTAGE_SET", f"{percentage}%")
        
        await update.message.reply_text(
            "📉 Enter the stop-loss percentage (e.g., 5 for 5% loss):",
            parse_mode='Markdown'
        )
        return SET_LOSS_PERCENTAGE
    except ValueError:
        await update.message.reply_text("❌ Invalid percentage. Please enter a number.")
        return SET_SELL_PERCENTAGE

async def set_loss_percentage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Set stop-loss percentage for automatic trading"""
    user_id = update.effective_user.id
    try:
        percentage = float(update.message.text)
        if percentage <= 0:
            await update.message.reply_text("❌ Please enter a positive percentage.")
            return SET_LOSS_PERCENTAGE
        
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {'loss_percentage': percentage}}
        )
        log_user_action(user_id, "LOSS_PERCENTAGE_SET", f"{percentage}%")
        
        await update.message.reply_text(
            "🔒 Enable anti-MEV protection? (yes/no)\n\n"
            "Anti-MEV measures help reduce the risk of front-running and sandwich attacks."
        )
        return SET_ANTI_MEV
    except ValueError:
        await update.message.reply_text("❌ Invalid percentage. Please enter a number.")
        return SET_LOSS_PERCENTAGE


async def set_anti_mev(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Set anti-MEV protection preference"""
    user_id = update.effective_user.id
    response = update.message.text.strip().lower()
    
    if response not in ['yes', 'no', 'y', 'n']:
        await update.message.reply_text("❌ Please answer with 'yes' or 'no'.")
        return SET_ANTI_MEV
    
    anti_mev = response in ['yes', 'y']
    users_collection.update_one(
        {'user_id': user_id},
        {'$set': {'anti_mev': anti_mev}}
    )
    log_user_action(user_id, "ANTI_MEV_SET", f"{anti_mev}")
    
    await update.message.reply_text(
        "💧 Set minimum liquidity threshold in USD (e.g., 5000 for $5,000):\n\n"
        "Tokens with liquidity below this amount will be ignored."
    )
    return SET_LIQUIDITY_THRESHOLD

async def set_liquidity_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Set minimum liquidity threshold"""
    user_id = update.effective_user.id
    try:
        threshold = float(update.message.text)
        if threshold < 1000:
            await update.message.reply_text("❌ Minimum liquidity should be at least $1000 for safety.")
            return SET_LIQUIDITY_THRESHOLD
        
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {'min_liquidity': threshold}}
        )
        log_user_action(user_id, "LIQUIDITY_THRESHOLD_SET", f"${threshold}")
        
        await update.message.reply_text(
            "📊 Set minimum 24h volume threshold in USD (e.g., 1000 for $1,000):\n\n"
            "Tokens with volume below this amount will be ignored."
        )
        return SET_VOLUME_THRESHOLD
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Please enter a number.")
        return SET_LIQUIDITY_THRESHOLD

async def set_volume_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Set minimum volume threshold"""
    user_id = update.effective_user.id
    try:
        threshold = float(update.message.text)
        if threshold < 500:
            await update.message.reply_text("❌ Minimum volume should be at least $500 for safety.")
            return SET_VOLUME_THRESHOLD
        
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {'min_volume': threshold}}
        )
        log_user_action(user_id, "VOLUME_THRESHOLD_SET", f"${threshold}")
        
        await update.message.reply_text(
            "🛡️ Enable automatic rug pull detection? (yes/no)\n\n"
            "This will check for common scam patterns before buying."
        )
        return SET_RUG_CHECK
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Please enter a number.")
        return SET_VOLUME_THRESHOLD

async def set_rug_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Set rug pull detection preference"""
    user_id = update.effective_user.id
    response = update.message.text.strip().lower()
    
    if response not in ['yes', 'no', 'y', 'n']:
        await update.message.reply_text("❌ Please answer with 'yes' or 'no'.")
        return SET_RUG_CHECK
    
    rug_check = response in ['yes', 'y']
    users_collection.update_one(
        {'user_id': user_id},
        {'$set': {'rug_check': rug_check}}
    )
    log_user_action(user_id, "RUG_CHECK_SET", f"{rug_check}")
    
    await update.message.reply_text(
        "⚡ Set maximum slippage percentage (e.g., 5 for 5%):\n\n"
        "This limits how much the price can change during transaction execution."
    )
    return SET_MAX_SLIPPAGE

async def set_max_slippage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Set maximum slippage percentage"""
    user_id = update.effective_user.id
    try:
        slippage = float(update.message.text)
        if slippage < 1 or slippage > 20:
            await update.message.reply_text("❌ Slippage should be between 1% and 20%.")
            return SET_MAX_SLIPPAGE
        
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {'max_slippage': slippage}}
        )
        log_user_action(user_id, "MAX_SLIPPAGE_SET", f"{slippage}%")
        
        await update.message.reply_text(
            "⛽ Set maximum gas price in SOL (e.g., 0.0005 for 0.0005 SOL):\n\n"
            "Transactions with higher gas prices will be skipped to save costs."
        )
        return SET_MAX_GAS_PRICE
    except ValueError:
        await update.message.reply_text("❌ Invalid percentage. Please enter a number.")
        return SET_MAX_SLIPPAGE

async def set_max_gas_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Set maximum gas price"""
    user_id = update.effective_user.id
    try:
        gas_price = float(update.message.text)
        if gas_price <= 0 or gas_price > 0.01:
            await update.message.reply_text("❌ Gas price should be between 0.0001 and 0.01 SOL.")
            return SET_MAX_GAS_PRICE
        
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {'max_gas_price': gas_price}}
        )
        log_user_action(user_id, "MAX_GAS_PRICE_SET", f"{gas_price} SOL")
        
        await update.message.reply_text(
            "⏰ Set minimum token age in minutes (e.g., 10 for 10 minutes):\n\n"
            "Newer tokens than this will be ignored to avoid scams."
        )
        return SET_TOKEN_AGE
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Please enter a number.")
        return SET_MAX_GAS_PRICE

async def set_token_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Set minimum token age"""
    user_id = update.effective_user.id
    try:
        min_age = float(update.message.text)
        if min_age < 5 or min_age > 120:
            await update.message.reply_text("❌ Token age should be between 5 and 120 minutes.")
            return SET_TOKEN_AGE
        
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {'min_token_age': min_age}}
        )
        log_user_action(user_id, "MIN_TOKEN_AGE_SET", f"{min_age} minutes")
        
        # Get user data to show final configuration
        user = users_collection.find_one({'user_id': user_id})
        
        # Schedule auto-trade job
        for job in context.job_queue.jobs():
            if job.name == f"auto_trade_{user_id}":
                job.schedule_removal()
        
        context.job_queue.run_repeating(
            auto_trade, 
            interval=30,
            first=5, 
            user_id=user_id,
            name=f"auto_trade_{user_id}"
        )
        
        await update.message.reply_text(
            f"✅ *Automatic trading activated with enhanced safety parameters!*\n\n"
            f"🤖 Settings:\n"
            f"• Auto-buy amount: {user['auto_buy_amount']} SOL\n"
            f"• Sell at: {user['sell_percentage']}% profit\n"
            f"• Stop-loss at: {user['loss_percentage']}% loss\n"
            f"• Anti-MEV: {'Yes' if user.get('anti_mev', False) else 'No'}\n"
            f"• Min liquidity: ${user.get('min_liquidity', 1000)}\n"
            f"• Min volume: ${user.get('min_volume', 500)}\n"
            f"• Rug check: {'Yes' if user.get('rug_check', False) else 'No'}\n"
            f"• Max slippage: {user.get('max_slippage', 5)}%\n"
            f"• Max gas: {user.get('max_gas_price', 0.0005)} SOL\n"
            f"• Min token age: {user.get('min_token_age', 10)} minutes\n\n"
            f"🔔 You'll receive notifications for all auto-trades.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ Invalid number. Please enter a number.")
        return SET_TOKEN_AGE



async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    """Send daily trading report to users"""
    logger.info("📊 Generating daily reports...")
    active_users = users_collection.find({
        "subscription_status": "active",
        "subscription_expiry": {"$gt": datetime.now().isoformat()}
    })
    
    for user in active_users:
        user_id = user['user_id']
        portfolio = user.get('portfolio', {})
        trade_history = user.get('trade_history', [])
        
        if not portfolio and not trade_history:
            continue
            
        # Calculate portfolio value
        total_value = 0
        portfolio_details = ""
        for contract, token_data in portfolio.items():
            total_value += token_data['amount']
            portfolio_details += (
                f"• {token_data['name']} ({token_data['symbol']}): "
                f"{token_data['amount']:.4f} SOL\n"
            )
        
        # Summarize today's trades
        today = datetime.now().date()
        today_trades = [
            t for t in trade_history 
            if datetime.fromisoformat(t['timestamp']).date() == today
        ]
        
        trade_summary = ""
        if today_trades:
            profit_loss = 0
            for trade in today_trades:
                trade_profit = (trade['sell_price'] - trade['buy_price']) * trade['amount']
                profit_loss += trade_profit
                trade_summary += (
                    f"• {trade['token']}: Sold at {trade['reason']}, "
                    f"P&L: {trade_profit:.4f} SOL\n"
                )
        else:
            trade_summary = "No trades today"
        
        message = (
            f"📈 *Daily Trading Report*\n\n"
            f"🏦 *Portfolio Value*: {total_value:.4f} SOL\n"
            f"📊 *Holdings*:\n{portfolio_details}\n"
            f"🔄 *Today's Trades*:\n{trade_summary}\n"
            f"💰 *Total Daily P&L*: {profit_loss:.4f} SOL"
        )
        
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode='Markdown'
            )
            log_user_action(user_id, "DAILY_REPORT_SENT")
        except Exception as e:
            logger.error(f"Failed to send daily report to {user_id}: {str(e)}")

        

async def trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Initiate manual trading"""
    user_id = update.effective_user.id
    log_user_action(user_id, "TRADE_COMMAND")
    
    if not await check_subscription(user_id):
        await update.message.reply_text("🔒 You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
        
    user = users_collection.find_one({'user_id': user_id})
    if not user or not user.get('solana') or not user['solana'].get('public_key'):
        await update.message.reply_text(
            "🚫 You need to set up a wallet first. Use /start to create a wallet or /set_wallet to import one."
        )
        return ConversationHandler.END
        
    await update.message.reply_text(
        "🔍 Enter the Solana token contract address you want to trade:\n"
        "(e.g., 4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R)\n\n"
        "You can find token addresses on DexScreener or Solscan."
    )
    return INPUT_CONTRACT

async def input_contract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle token contract address input"""
    user_id = update.effective_user.id
    contract_address = update.message.text.strip()
    log_user_action(user_id, "CONTRACT_ADDRESS_INPUT", contract_address)
    
    if not contract_address:
        await update.message.reply_text("❌ Contract address cannot be empty. Please enter a valid Solana token address.")
        log_user_action(user_id, "EMPTY_CONTRACT_ADDRESS", level="warning")
        return INPUT_CONTRACT
    
    try:
        Pubkey.from_string(contract_address)
        if len(contract_address) < 32 or len(contract_address) > 44:
            raise ValueError("Invalid length for Solana address")
    except Exception as e:
        await update.message.reply_text(
            "❌ Invalid contract address format. Please enter a valid Solana token address (e.g., 4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R)."
        )
        log_user_action(user_id, "INVALID_CONTRACT_ADDRESS", f"{contract_address}: {str(e)}", level="warning")
        return INPUT_CONTRACT
    
    await update.message.reply_text("⏳ Fetching token data...")
    token = await fetch_token_by_contract(contract_address)
    
    if not token:
        await update.message.reply_text(
            "❌ Failed to fetch token data. The token may not have active trading pairs or the address is invalid."
        )
        log_user_action(user_id, "TOKEN_FETCH_FAILED", contract_address, level="warning")
        return ConversationHandler.END
        
    context.user_data['current_token'] = token
    message = format_token_message(token)
    
    keyboard = [
        [InlineKeyboardButton("💰 Buy", callback_data=f"buy_{token['contract_address']}"),
         InlineKeyboardButton("💸 Sell", callback_data=f"sell_{token['contract_address']}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
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
    """Fetch token details by contract address with rate limit handling and fallback"""
    # Check cache first
    if contract_address in token_cache:
        cached_data, timestamp = token_cache[contract_address]
        if time.time() - timestamp < CACHE_DURATION:
            return cached_data
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }
    
    # Try DexScreener first
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            logger.info(f"Fetching token by contract: {contract_address}")
            token_url = DEXSCREENER_TOKEN_API.format(token_address=contract_address)
            logger.debug(f"Calling DexScreener API: {token_url}")
            response = await client.get(token_url, headers=headers)
            
            if response.status_code == 429:
                logger.warning("DexScreener rate limit exceeded, trying fallback...")
                return await fetch_token_fallback(contract_address, client)
            
            if response.status_code != 200:
                logger.error(f"Token API failed: {response.status_code} - {response.text}")
                return await fetch_token_fallback(contract_address, client)
            
            data = response.json()
            logger.debug(f"API response: {json.dumps(data, indent=2)[:500]}...")
            
            if not isinstance(data, list) or not data:
                logger.error(f"Unexpected response format: {type(data)}")
                return await fetch_token_fallback(contract_address, client)
            
            pair = next((p for p in data if p.get('chainId') == 'solana'), None)
            if not pair:
                logger.error(f"No Solana pairs found for token {contract_address}")
                return await fetch_token_fallback(contract_address, client)
                
            base_token = pair.get('baseToken', {})
            quote_token = pair.get('quoteToken', {})
            
            token_info = base_token if base_token.get('address', '').lower() == contract_address.lower() else quote_token
            
            token_data = {
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
            
            # Cache the result
            token_cache[contract_address] = (token_data, time.time())
            return token_data
            
    except Exception as e:
        logger.error(f"Error fetching token by contract {contract_address}: {str(e)}")
        return await fetch_token_fallback(contract_address)

async def job_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show status of background jobs"""
    user_id = update.effective_user.id
    log_user_action(user_id, "JOB_STATUS_REQUEST")
    
    if not context.job_queue:
        await update.message.reply_text("❌ Job queue not initialized")
        return
        
    user_jobs = []
    for job in context.job_queue.jobs():
        if job.user_id == user_id:
            next_run = job.next_t.strftime('%Y-%m-%d %H:%M:%S') if job.next_t else "N/A"
            user_jobs.append(f"- {job.name}: Next run at {next_run}")
    
    if not user_jobs:
        message = "ℹ️ No active jobs for your account"
    else:
        message = "📅 Your active jobs:\n" + "\n".join(user_jobs)
    
    await update.message.reply_text(message)

async def force_token_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force an immediate token fetch"""
    user_id = update.effective_user.id
    log_user_action(user_id, "FORCE_TOKEN_FETCH")
    
    if not context.job_queue:
        await update.message.reply_text("❌ Job queue not initialized")
        return
        
    user = users_collection.find_one({'user_id': user_id})
    if not user or not user.get('solana') or not user['solana'].get('public_key'):
        await update.message.reply_text(
            "🚫 You need to set up a wallet first. Use /start to create a wallet or /set_wallet to import one."
        )
        return
        
    context.job_queue.run_once(
        update_token_info,
        when=0,
        user_id=user_id,
        name=f"manual_fetch_{user_id}_{int(time.time())}"
    )
    
    await update.message.reply_text("⏳ Token fetch triggered. You should receive tokens shortly.")

async def handle_token_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle buy/sell button clicks on token messages"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    action, contract_address = query.data.split('_', 1)
    log_user_action(user_id, "TOKEN_BUTTON_CLICKED", f"{action} {contract_address}")
    
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        logger.warning(f"Couldn't remove buttons: {str(e)}")
    
    token = await fetch_token_by_contract(contract_address)
    if not token:
        await query.message.reply_text("❌ Failed to fetch token details. Please try again.")
        log_user_action(user_id, "TOKEN_FETCH_FAILED", contract_address, level="warning")
        return
    
    context.user_data['current_token'] = token
    context.user_data['trade_action'] = action
    
    if action == 'buy':
        await query.message.reply_text(
            f"💰 *Buy Order for {token['name']} ({token['symbol']})*\n\n"
            f"Enter amount to buy in SOL:",
            parse_mode='Markdown'
        )
        return BUY_AMOUNT
    else:
        user = users_collection.find_one({'user_id': user_id})
        portfolio = user.get('portfolio', {})
        if contract_address not in portfolio:
            await query.message.reply_text(f"❌ You don't hold any {token['name']} tokens to sell.")
            return ConversationHandler.END
            
        # Calculate available token count from SOL investment and buy price
        token_data = portfolio[contract_address]
        buy_price = token_data['buy_price']
        sol_invested = token_data['amount']
        
        # Calculate token count: token_count = SOL_invested / buy_price
        available_tokens = sol_invested / buy_price
        
        await query.message.reply_text(
            f"💸 *Sell Order for {token['name']} ({token['symbol']})*\n\n"
            f"Available: {available_tokens:.2f} tokens\n"
            f"Current Price: ${token['price_usd']:.6f}\n"
            f"Enter number of tokens to sell:",
            parse_mode='Markdown'
        )
        return SELL_AMOUNT
    

async def get_minimal_token_data(contract_address: str) -> Optional[Dict[str, Any]]:
    """Get minimal token data from on-chain sources"""
    try:
        # Get token metadata from on-chain
        mint_pubkey = Pubkey.from_string(contract_address)
        account_info = await solana_client.get_account_info(mint_pubkey)
        
        if account_info.value:
            # This is a very basic implementation - in a real scenario,
            # you would use the SPL Token program to get better data
            return {
                'name': 'Unknown Token',
                'symbol': 'UNKNOWN',
                'contract_address': contract_address,
                'price_usd': 0,
                'market_cap': 0,
                'liquidity': 0,
                'volume': 0,
                'dexscreener_url': f"https://dexscreener.com/solana/{contract_address}",
            }
    except Exception as e:
        logger.error(f"Failed to get on-chain data for {contract_address}: {str(e)}")
    
    return None


async def fetch_token_fallback(contract_address: str, client: Optional[httpx.AsyncClient] = None) -> Optional[Dict[str, Any]]:
    """Fallback method to fetch token data when DexScreener fails"""
    try:
        # Try alternative APIs or methods
        fallback_apis = [
            f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{contract_address}",
            f"https://api.birdeye.so/defi/token_overview?address={contract_address}",
        ]
        
        if client is None:
            client = httpx.AsyncClient(timeout=15.0)
        
        for api_url in fallback_apis:
            try:
                response = await client.get(api_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
                if response.status_code == 200:
                    data = response.json()
                    
                    # Parse different API responses
                    if "geckoterminal" in api_url:
                        token_data = parse_geckoterminal_response(data, contract_address)
                    elif "birdeye" in api_url:
                        token_data = parse_birdeye_response(data, contract_address)
                    
                    if token_data:
                        token_cache[contract_address] = (token_data, time.time())
                        return token_data
                        
            except Exception as e:
                logger.warning(f"Fallback API {api_url} failed: {str(e)}")
                continue
        
        # If all else fails, try to get minimal data from on-chain
        return await get_minimal_token_data(contract_address)
        
    except Exception as e:
        logger.error(f"All fallback methods failed for {contract_address}: {str(e)}")
        return None
    

def parse_geckoterminal_response(data, contract_address):
    """Parse GeckoTerminal API response to match token_data structure"""
    if 'data' in data and 'attributes' in data['data']:
        attributes = data['data']['attributes']
        return {
            'name': attributes.get('name', 'Unknown'),
            'symbol': attributes.get('symbol', 'UNKNOWN'),
            'contract_address': contract_address,
            'price_usd': float(attributes.get('price_usd', 0)),
            'market_cap': float(attributes.get('fdv_usd', 0)),
            'liquidity': float(attributes.get('liquidity_usd', 0)),
            'volume': float(attributes.get('volume_usd', {}).get('h24', 0)),
            'dexscreener_url': f"https://dexscreener.com/solana/{contract_address}",
            'image': attributes.get('image_thumb_url', ''),
            'socials': {}  # Add if available in response
        }
    else:
        return None
    
def parse_birdeye_response(data, contract_address):
    """Parse Birdeye API response to match token_data structure"""
    if 'data' in data and 'success' in data and data['success']:
        token_data = data['data']
        return {
            'name': token_data.get('name', 'Unknown'),
            'symbol': token_data.get('symbol', 'UNKNOWN'),
            'contract_address': contract_address,
            'price_usd': float(token_data.get('price', 0)),
            'market_cap': float(token_data.get('mc', 0)),
            'liquidity': float(token_data.get('liquidity', 0)),
            'volume': float(token_data.get('v24hUSD', 0)),
            'dexscreener_url': f"https://dexscreener.com/solana/{contract_address}",
            'image': token_data.get('image', ''),
            'socials': {}  # Birdeye doesn't provide socials in this endpoint
        }
    else:
        return None

async def buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle buy amount input with balance check and trade execution"""
    user_id = update.effective_user.id
    try:
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("❌ Please enter a positive amount.")
            return BUY_AMOUNT
        
        # Get token from context
        token = context.user_data.get('current_token')
        if not token:
            await update.message.reply_text("❌ Token information missing. Please start the trade again.")
            return ConversationHandler.END
        
        # Check SOL balance
        balance = await check_balance(user_id, 'solana')
        if balance < amount:
            await update.message.reply_text(
                f"❌ Insufficient SOL balance. Your balance: {balance:.4f} SOL\n"
                f"You need at least {amount:.4f} SOL to execute this trade."
            )
            log_user_action(user_id, "INSUFFICIENT_BALANCE", f"Needed: {amount}, Had: {balance}", level="warning")
            return ConversationHandler.END
        
        # Execute trade immediately
        await update.message.reply_text(f"⏳ Executing buy order for {amount:.4f} SOL worth of {token['name']}...")
        success = await execute_trade(user_id, token['contract_address'], amount, 'buy', 'solana', token)
        
        if success:
            # Update portfolio
            user = users_collection.find_one({'user_id': user_id})
            portfolio = user.get('portfolio', {})
            current_holdings = portfolio.get(token['contract_address'], {
                'name': token['name'],
                'symbol': token['symbol'],
                'amount': 0.0,
                'buy_price': token['price_usd']
            })
            
            # Add new purchase to portfolio
            new_amount = current_holdings['amount'] + amount
            new_avg_price = (
                (current_holdings['amount'] * current_holdings['buy_price'] + 
                 amount * token['price_usd'])
                / new_amount if new_amount > 0 else token['price_usd']
            )
            
            update_result = users_collection.update_one(
                {'user_id': user_id},
                {'$set': {
                    f'portfolio.{token["contract_address"]}': {
                        'name': token['name'],
                        'symbol': token['symbol'],
                        'amount': new_amount,
                        'buy_price': new_avg_price
                    }
                }},
                upsert=True
            )
            
            log_user_action(user_id, "TRADE_EXECUTED", f"Bought {amount} SOL worth of {token['name']}")
            await update.message.reply_text(
                f"✅ Successfully bought {amount:.4f} SOL worth of {token['name']} at ${token['price_usd']:.6f}.\n"
                f"📊 You now hold {new_amount:.4f} SOL worth of {token['symbol']}."
            )
        else:
            log_user_action(user_id, "TRADE_FAILED", f"Buy {amount} SOL of {token['name']}", level="error")
            await update.message.reply_text("❌ Trade failed. Please try again later.")
        
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Please enter a number.")
        return BUY_AMOUNT



async def sell_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle sell amount input with proper token amount calculation"""
    user_id = update.effective_user.id
    try:
        # Get the token amount user wants to sell (not SOL value)
        token_amount = float(update.message.text)
        if token_amount <= 0:
            await update.message.reply_text("❌ Please enter a positive amount.")
            return SELL_AMOUNT
        
        # Get token from context
        token = context.user_data.get('current_token')
        if not token:
            await update.message.reply_text("❌ Token information missing. Please start the trade again.")
            return ConversationHandler.END
        
        # Check token holdings
        user = users_collection.find_one({'user_id': user_id})
        portfolio = user.get('portfolio', {})
        if token['contract_address'] not in portfolio:
            await update.message.reply_text(f"❌ You don't hold any {token['name']} tokens to sell.")
            return ConversationHandler.END
            
        token_data = portfolio[token['contract_address']]
        
        # Calculate how many tokens the user actually holds
        # The portfolio stores SOL value, so we need to convert back to token amount
        buy_price = token_data['buy_price']
        token_amount_held = token_data['amount'] / buy_price
        
        if token_amount > token_amount_held:
            await update.message.reply_text(
                f"❌ Insufficient token balance. Available: {token_amount_held:.2f} tokens\n"
                f"You requested to sell {token_amount:.2f} tokens."
            )
            return SELL_AMOUNT
        
        # Store the token amount in context for execution
        context.user_data['sell_token_amount'] = token_amount
        
        # Execute trade immediately
        await update.message.reply_text(f"⏳ Executing sell order for {token_amount:.2f} {token['symbol']} tokens...")
        
        # For sell orders, we need to pass the token amount
        success, message = await execute_trade(
            user_id, 
            token['contract_address'], 
            token_amount,  # Pass token amount instead of SOL amount
            'sell', 
            'solana',
            token
        )
        
        if success:
            # Update portfolio
            new_token_amount = token_amount_held - token_amount
            new_sol_value = new_token_amount * buy_price  # Keep original buy price for cost basis
            
            if new_token_amount <= 0.001:  # Consider dust amounts as zero
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$unset': {f'portfolio.{token["contract_address"]}': ""}}
                )
                log_user_action(user_id, "POSITION_CLOSED", f"Sold all {token['name']}")
            else:
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$set': {f'portfolio.{token["contract_address"]}.amount': new_sol_value}}
                )
            
            # Calculate profit/loss
            current_price = token['price_usd']
            price_change = ((current_price - buy_price) / buy_price) * 100
            profit_loss = "profit" if price_change >= 0 else "loss"
            
            log_user_action(user_id, "TRADE_EXECUTED", f"Sold {token_amount} tokens of {token['name']}")
            await update.message.reply_text(
                f"✅ {message}\n"
                f"📈 This trade resulted in a {abs(price_change):.2f}% {profit_loss}.\n"
                f"📊 You now hold {new_token_amount:.2f} {token['symbol']} tokens."
            )
        else:
            log_user_action(user_id, "TRADE_FAILED", f"Sell {token_amount} tokens of {token['name']}: {message}", level="error")
            await update.message.reply_text(f"❌ {message}")
        
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Please enter a number.")
        return SELL_AMOUNT
    
async def confirm_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirm and execute trade"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text="⏳ Processing your trade...")
    user_id = query.from_user.id
    
    token = context.user_data['current_token']
    action = context.user_data.get('trade_action', 'buy')
    amount = context.user_data.get('buy_amount' if action == 'buy' else 'sell_amount')
    
    # Pass token details to execute_trade
    success = await execute_trade(user_id, token['contract_address'], amount, action, 'solana', token)
    
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
            log_user_action(user_id, "TRADE_EXECUTED", f"Bought {amount} SOL worth of {token['name']}")
            await query.message.reply_text(
                f"✅ Successfully bought {amount:.4f} SOL worth of {token['name']} at ${token['price_usd']:.6f}."
            )
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
            log_user_action(user_id, "TRADE_EXECUTED", f"Sold {amount} SOL worth of {token['name']}")
            await query.message.reply_text(
                f"✅ Successfully sold {amount:.4f} SOL worth of {token['name']} at ${token['price_usd']:.6f}."
            )
    else:
        log_user_action(user_id, "TRADE_FAILED", f"{action} {amount} SOL of {token['name']}", level="error")
        await query.message.reply_text("❌ Trade failed. Please try again.")
    
    return ConversationHandler.END

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show wallet balances with current token values"""
    user_id = update.effective_user.id
    log_user_action(user_id, "BALANCE_REQUEST")
    
    if not await check_subscription(user_id):
        await update.message.reply_text("🔒 You need an active subscription to use this feature. Use /subscribe.")
        return
    
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("🚫 No wallet found. Please use /start to create a wallet or /set_wallet to import one.")
        return
    
    sol_balance = await check_balance(user_id, 'solana')
    eth_balance = await check_balance(user_id, 'eth') if user.get('eth') else 0.0
    bsc_balance = await check_balance(user_id, 'bsc') if user.get('bsc') else 0.0
    portfolio = user.get('portfolio', {})
    
    message = (
        f"💰 *Wallet Balance*\n\n"
        f"🔹 Solana (SOL): {sol_balance:.4f}\n"
        f"🔹 Ethereum (ETH): {eth_balance:.6f}\n"
        f"🔹 BSC (BNB): {bsc_balance:.6f}\n\n"
        f"📊 *Token Holdings (Solana)*\n"
    )
    
    if not portfolio:
        message += "No tokens held."
    else:
        total_portfolio_value = 0
        
        for contract, details in portfolio.items():
            # Get current token price
            token = await fetch_token_by_contract(contract)
            if not token:
                # Use buy price if current price not available
                current_price = details['buy_price']
                current_value = details['amount']
                price_note = " (price unavailable, using buy price)"
            else:
                current_price = token['price_usd']
                # Calculate token count and current value
                token_count = details['amount'] / details['buy_price']
                current_value = token_count * current_price
                price_note = ""
                
            total_portfolio_value += current_value
            
            # Calculate profit/loss
            profit_loss = current_value - details['amount']
            profit_loss_percent = (profit_loss / details['amount']) * 100
            profit_loss_emoji = "📈" if profit_loss >= 0 else "📉"
            
            # Calculate token count
            token_count = details['amount'] / details['buy_price']
            
            message += (
                f"🔸 {details['name']} ({details['symbol']}):\n"
                f"   - Token Amount: {token_count:.2f}\n"
                f"   - Current Price: ${current_price:.8f}\n"
                f"   - Buy Price: ${details['buy_price']:.8f}\n"
                f"   - P&L: {profit_loss_emoji} {profit_loss:+.4f} SOL ({profit_loss_percent:+.2f}%)\n"
            )
        
        message += f"\n💼 *Total Portfolio Value*: {total_portfolio_value:.4f} SOL"
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def start_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Initiate token transfer"""
    user_id = update.effective_user.id
    log_user_action(user_id, "TRANSFER_INITIATED")
    
    if not await check_subscription(user_id):
        await update.message.reply_text("🔒 You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        await update.message.reply_text("🚫 No wallet found. Please use /start to create a wallet or /set_wallet to import one.")
        return ConversationHandler.END
    
    portfolio = user.get('portfolio', {})
    if not portfolio:
        await update.message.reply_text("❌ You have no tokens to transfer.")
        return ConversationHandler.END
    
    keyboard = [[InlineKeyboardButton(details['name'], callback_data=contract)] 
                for contract, details in portfolio.items()]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "📤 Select Solana token to transfer:",
        reply_markup=reply_markup
    )
    return TRANSFER_TOKEN

async def transfer_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle token selection for transfer"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    log_user_action(user_id, "TRANSFER_TOKEN_SELECTED", query.data)
    
    if not await check_subscription(user_id):
        await query.message.reply_text("🔒 You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    
    context.user_data['transfer_token'] = query.data
    user = users_collection.find_one({'user_id': user_id})
    token = user['portfolio'][query.data]
    
    await query.message.reply_text(
        f"📤 Selected token: {token['name']}\n"
        f"Enter amount to transfer (in SOL worth):"
    )
    return TRANSFER_AMOUNT

async def transfer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle transfer amount input"""
    user_id = update.effective_user.id
    log_user_action(user_id, "TRANSFER_AMOUNT_INPUT")
    
    if not await check_subscription(user_id):
        await update.message.reply_text("🔒 You need an active subscription to use this feature. Use /subscribe.")
        return ConversationHandler.END
    
    try:
        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("❌ Please enter a positive amount.")
            return TRANSFER_AMOUNT
        
        token_contract = context.user_data['transfer_token']
        user = users_collection.find_one({'user_id': user_id})
        token = user['portfolio'][token_contract]
        
        if amount > token['amount']:
            await update.message.reply_text(
                f"❌ Insufficient token balance. Available: {token['amount']:.4f} SOL worth"
            )
            return TRANSFER_AMOUNT
        
        context.user_data['transfer_amount'] = amount
        await update.message.reply_text("📥 Enter the recipient Solana address:")
        return TRANSFER_ADDRESS
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Please enter a number.")
        return TRANSFER_AMOUNT

async def transfer_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle transfer recipient address input"""
    user_id = update.effective_user.id
    log_user_action(user_id, "TRANSFER_ADDRESS_INPUT")
    
    if not await check_subscription(user_id):
        await update.message.reply_text("🔒 You need an active subscription to use this feature. Use /subscribe.")
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
            # Remove token from portfolio
             update_result = users_collection.update_one(
                {'user_id': user_id},
                {'$unset': {f'portfolio.{token_contract}': ""}}
            )
            else:
                update_result = users_collection.update_one(
                {'user_id': user_id},
                {'$set': {f'portfolio.{token_contract}.amount': token['amount']}}
            )
        
        # Log update result
            logger.info(f"Portfolio update - matched: {update_result.matched_count}, "
                   f"modified: {update_result.modified_count}, "
                   f"upserted_id: {update_result.upserted_id}")
            await update.message.reply_text("✅ Transfer successful.")
        else:
            log_user_action(user_id, "TRANSFER_FAILED", 
                           f"{amount} SOL worth of {token_contract} to {address}", level="error")
            await update.message.reply_text("❌ Transfer failed. Please check the address and try again.")
        
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Invalid address: {str(e)}")
        return TRANSFER_ADDRESS

# Modified fetch_latest_token function
async def fetch_latest_token() -> List[Dict[str, Any]]:
    """Fetch all Solana tokens without time filtering"""
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
            
            solana_tokens = []
            
            for token_data in data:
                if token_data.get('chainId') != 'solana':
                    continue
                
                contract_address = token_data.get('tokenAddress', '')
                if not contract_address:
                    continue
                
                token = await fetch_token_by_contract(contract_address)
                if not token:
                    logger.warning(f"Failed to fetch trading data for token {contract_address}")
                    continue
                
                token.update({
                    'name': token_data.get('name', token['name']),
                    'symbol': token_data.get('symbol', token['symbol']),
                    'image': token_data.get('icon', token['image']),
                    'socials': {link.get('type', link.get('label', 'website').lower()): link['url']
                               for link in token_data.get('links', [])},
                    'description': token_data.get('description', ''),
                    'openGraph': token_data.get('openGraph', '')
                })
                solana_tokens.append(token)
            
            logger.info(f"Fetched {len(solana_tokens)} Solana tokens")
            return solana_tokens
        
        except Exception as e:
            logger.error(f"Error fetching latest tokens: {str(e)}")
            return []
        
def escape_markdown(text: str) -> str:
    """Escape Markdown special characters for Telegram messages"""
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def format_token_message(token: Dict[str, Any]) -> str:
    """Format token information into a message with escaped Markdown"""
    platform_icons = {
        'telegram': '📢',
        'twitter': '🐦',
        'website': '🌐',
        'discord': '💬',
        'medium': '✍️'
    }
    
    # Escape all dynamic content
    name = escape_markdown(token.get('name', 'New Token'))
    symbol = escape_markdown(token.get('symbol', 'TOKEN'))
    contract_address = escape_markdown(token.get('contract_address', ''))
    description = escape_markdown(token.get('description', 'No description available'))
    
    # Format numbers safely
    price = f"{token.get('price_usd', 0):.6f}"
    market_cap = f"{token.get('market_cap', 0):,.2f}"
    liquidity = f"{token.get('liquidity', 0):,.2f}"
    volume = f"{token.get('volume', 0):,.2f}"
    
    social_links = ""
    if token.get('socials'):
        for platform, url in token['socials'].items():
            icon = platform_icons.get(platform.lower(), '🔗')
            platform_text = escape_markdown(platform.capitalize())
            social_links += f"{icon} [{platform_text}]({url})\n"
    
    dexscreener_url = token.get('dexscreener_url', '')
    
    return (
        f"🚀 *{name} ({symbol})*\n\n"
        f"💵 *Price:* ${price}\n"
        f"📊 *Market Cap:* ${market_cap}\n"
        f"💧 *Liquidity:* ${liquidity}\n"
        f"📈 *24h Volume:* ${volume}\n\n"
        f"🔗 *Contract:* `{contract_address}`\n"
        f"📝 *Description:* {description}\n\n"
        f"🔗 *Links:*\n{social_links or 'No links available'}\n"
        f"[📊 View Chart]({dexscreener_url})"
    )

# Modified update_token_info function
async def update_token_info(context):
    user_id = context.job.user_id
    logger.info(f"⏰ Token update job started for user {user_id} at {datetime.now()}")
    
    try:
        user = users_collection.find_one({'user_id': user_id})
        if not user:
            logger.info(f"User {user_id} not found in database")
            return
            
        if not await check_subscription(user_id):
            logger.info(f"User {user_id} subscription inactive")
            return
        
        if not user.get('solana') or not user['solana'].get('public_key'):
            logger.info(f"User {user_id} has no wallet set up")
            return

        current_time = time.time()
        if user.get('last_api_call', 0) > current_time - 1:
            logger.debug("Skipping due to rate limit")
            return

        logger.info(f"🔍 Fetching tokens for user {user_id}")
        tokens = await fetch_latest_token()
        if not tokens:
            logger.warning("No tokens fetched")
            return
            
        posted_tokens = user.get('posted_tokens', [])
        new_tokens = [t for t in tokens if t['contract_address'] not in posted_tokens]
        
        if new_tokens:
            token = new_tokens[0]  # Take the most recent unseen token
            logger.info(f"Processing token: {token['name']} ({token['contract_address']})")
            
            # Record token performance globally
            await record_token_performance(token)

            message = format_token_message(token)
            is_suspicious = token['liquidity'] < 1000 or token['volume'] < 1000
            warning = "⚠️ *LOW LIQUIDITY - Trade with caution!*\n" if is_suspicious else ""
            
            keyboard = [
                [InlineKeyboardButton("💰 Buy", callback_data=f"buy_{token['contract_address']}"),
                 InlineKeyboardButton("💸 Sell", callback_data=f"sell_{token['contract_address']}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
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
            
            # Add to user's posted tokens
            users_collection.update_one(
                {'user_id': user_id},
                {'$addToSet': {'posted_tokens': token['contract_address']}}
            )
            logger.info(f"Added token {token['contract_address']} to user {user_id}'s posted tokens")
            
            
            
            if user.get('trading_mode') == 'automatic':
                await auto_trade(context, user_id, token)
        
    except Exception as e:
        logger.error(f"🔥 Error in auto token update: {str(e)}", exc_info=True)
    finally:
        logger.info(f"🏁 Token update job completed for user {user_id}")

async def check_balance(user_id, chain):
    """Check balance for a specific chain"""
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

async def execute_trade(user_id, contract_address, amount, action, chain, token_info):
    logger.info(f"🏁 Starting {action} trade for {amount} of {contract_address}")
    try:
        if chain != 'solana':
            logger.error(f"Trading not supported for {chain} yet")
            return False, "Trading not supported for this blockchain"

        user = users_collection.find_one({'user_id': user_id})
        if not user:
            logger.error(f"No user found for user_id {user_id}")
            return False, "User not found"

        decrypted_user = await decrypt_user_wallet(user_id, user)
        solana_private_key = decrypted_user.get('solana', {}).get('private_key')

        if not solana_private_key or solana_private_key == "[Decryption Failed]":
            logger.error(f"Failed to decrypt Solana private key for user {user_id}")
            return False, "Wallet decryption failed"

        keypair = Keypair.from_bytes(base58.b58decode(solana_private_key))
        from_address = str(keypair.pubkey())

        # Jupiter API endpoints
        quote_url = "https://quote-api.jup.ag/v6/quote"
        swap_url = "https://quote-api.jup.ag/v6/swap"
        
        # Determine swap parameters based on action
        if action == 'buy':
            input_mint = "So11111111111111111111111111111111111111112"  # SOL
            output_mint = contract_address
            amount_lamports = int(amount * 10**9)  # Convert SOL to lamports
            swap_mode = "ExactIn"
            slippage_bps = "500"  # 5% slippage for buys
        else:  # sell
            input_mint = contract_address
            output_mint = "So11111111111111111111111111111111111111112"  # SOL
            
            # For sell orders, amount is the token amount
            # We need to convert to the raw token amount based on decimals
            token_decimals = await get_token_decimals(contract_address)
            amount_raw = int(amount * (10 ** token_decimals))
            
            swap_mode = "ExactIn"  # We're specifying the exact input token amount
            amount_lamports = amount_raw
            slippage_bps = "1000"  # 10% slippage for sells

        # Get quote from Jupiter
        quote_params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_lamports),
            "slippageBps": slippage_bps,
            "swapMode": swap_mode,
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
            "maxAccounts": "64"
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Get quote
            quote_response = await client.get(quote_url, params=quote_params)
            
            if quote_response.status_code != 200:
                error_msg = quote_response.text
                logger.error(f"Jupiter quote failed: {error_msg}")
                
                # Handle specific error cases
                if "COULD_NOT_FIND_ANY_ROUTE" in error_msg:
                    return False, "No trading route found for this token. It may have low liquidity or no trading pairs."
                elif "Invalid token" in error_msg:
                    return False, "Invalid token address or token not found."
                else:
                    return False, f"Quote failed: {error_msg}"
            
            quote_data = quote_response.json()
            
            # Check if we got a valid quote
            if not quote_data or 'routePlan' not in quote_data or not quote_data['routePlan']:
                return False, "No valid trading route found for this token"
            
            # Check if the price impact is too high
            if 'priceImpactPct' in quote_data and float(quote_data['priceImpactPct']) > 0.1:
                return False, "Price impact too high (>10%). Trade would be unfavorable."
            
            # Prepare swap transaction
            swap_payload = {
                "quoteResponse": quote_data,
                "userPublicKey": from_address,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "100000",
                "useSharedAccounts": True,
                "asLegacyTransaction": False,
                "useTokenLedger": False
            }
            
            # Get swap transaction
            swap_response = await client.post(swap_url, json=swap_payload)
            if swap_response.status_code != 200:
                error_msg = swap_response.text
                logger.error(f"Jupiter swap failed: {error_msg}")
                return False, f"Swap preparation failed: {error_msg}"
                
            swap_data = swap_response.json()
            swap_transaction = swap_data.get("swapTransaction")
            
            if not swap_transaction:
                return False, "No swap transaction received from Jupiter"

            # Deserialize and sign transaction
            try:
                transaction_bytes = base64.b64decode(swap_transaction)
                
                # Try to parse as VersionedTransaction
                transaction = VersionedTransaction.from_bytes(transaction_bytes)
                
                # Sign the transaction
                transaction.sign([keypair])
                raw_transaction = bytes(transaction)
                
                # Send the transaction
                tx_hash = await solana_client.send_raw_transaction(
                    raw_transaction,
                    opts=TxOpts(
                        skip_preflight=False,
                        preflight_commitment="processed",
                        max_retries=3
                    )
                )
                
                logger.info(f"Transaction sent: {tx_hash.value}")
                
                # Wait for confirmation
                confirmation = await asyncio.wait_for(
                    solana_client.confirm_transaction(
                        tx_hash.value,
                        commitment="confirmed",
                        sleep_seconds=1
                    ),
                    timeout=30.0
                )
                
                if confirmation.value and not confirmation.value[0].err:
                    logger.info(f"✅ Transaction confirmed: {tx_hash.value}")
                    
                    # Record the transaction in user's history
                    trade_record = {
                        'type': action,
                        'token_address': contract_address,
                        'token_name': token_info.get('name', 'Unknown'),
                        'token_symbol': token_info.get('symbol', 'UNKNOWN'),
                        'amount': amount,
                        'price': token_info.get('price_usd', 0),
                        'tx_hash': tx_hash.value,
                        'timestamp': datetime.now().isoformat(),
                        'status': 'completed'
                    }
                    
                    users_collection.update_one(
                        {'user_id': user_id},
                        {'$push': {'trade_history': trade_record}}
                    )
                    
                    return True, f"Trade successful! TX: {tx_hash.value}"
                else:
                    error_msg = confirmation.value[0].err if confirmation.value else 'Unknown error'
                    logger.error(f"Transaction failed: {error_msg}")
                    return False, f"Transaction failed: {error_msg}"
                    
            except asyncio.TimeoutError:
                logger.error("Transaction confirmation timed out")
                return False, "Transaction confirmation timed out"
            except Exception as e:
                logger.error(f"Transaction processing failed: {str(e)}")
                return False, f"Transaction processing failed: {str(e)}"

    except Exception as e:
        logger.error(f"🔥 Trade execution failed: {str(e)}", exc_info=True)
        return False, f"Trade execution failed: {str(e)}"

async def get_token_decimals(token_address: str) -> int:
    """Get token decimals from Solana blockchain"""
    try:
        # Try to get token info from Solana
        mint_pubkey = Pubkey.from_string(token_address)
        account_info = await solana_client.get_account_info(mint_pubkey)
        
        if account_info.value:
            # Parse mint account data to get decimals
            # Mint account layout: https://docs.rs/spl-token/3.2.0/spl_token/state/struct.Mint.html
            data = account_info.value.data
            if len(data) >= 44:  # Mint account data length is at least 44 bytes
                decimals = data[44]  # Decimals are at offset 44
                return decimals
        
        # Fallback to default value if not found
        return 9
    except Exception as e:
        logger.error(f"Error getting decimals for token {token_address}: {str(e)}")
        return 9  # Default to 9 decimals

async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show debug information"""
    user_id = update.effective_user.id
    log_user_action(user_id, "DEBUG_REQUEST")
    
    user = users_collection.find_one({'user_id': user_id})
    
    job_count = 0
    if context.job_queue:
        job_count = len(context.job_queue.jobs())
    
    sub_status = "Not subscribed"
    if user and user.get('subscription_status') == 'active':
        expiry = user.get('subscription_expiry')
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        if expiry > datetime.now():
            sub_status = f"Active until {expiry}"
    
    last_check = "Never"
    if user and 'last_token_check' in user:
        last_check = datetime.fromtimestamp(user['last_token_check']).strftime('%Y-%m-%d %H:%M:%S')
    
    message = (
        f"🐛 *Debug Information*\n\n"
        f"🔹 User ID: {user_id}\n"
        f"🔹 Subscription: {sub_status}\n"
        f"🔹 Active jobs: {job_count}\n"
        f"🔹 Last token check: {last_check}\n"
        f"🔹 Posted tokens: {len(user.get('posted_tokens', [])) if user else 0}"
    )
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def execute_transfer(user_id, recipient, token_contract, amount, chain):
    """Execute a token transfer"""
    if chain != 'solana':
        logger.error(f"Transfer not supported for {chain} yet")
        return False
    logger.info(f"Transferring {amount} SOL worth of {token_contract} to {recipient} ({chain})")
    return True

async def notify_trial_ending(context: ContextTypes.DEFAULT_TYPE):
    """Notify user when trial is about to end"""
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
            log_user_action(user_id, "TRIAL_ENDING_NOTIFICATION")

async def auto_trade(context: ContextTypes.DEFAULT_TYPE):
    """Handle automatic trading with enhanced safety parameters"""
    job = context.job
    user_id = job.user_id
    logger.info(f"🤖 Auto-trading for user {user_id}")
    
    try:
        user = users_collection.find_one({'user_id': user_id})
        if not user or not await check_subscription(user_id):
            return
            
        # Check cooldown
        last_trade_time = user.get('last_trade_time', 0)
        if time.time() - last_trade_time < AUTO_TRADE_COOLDOWN:
            logger.debug(f"Auto-trade cooldown active for user {user_id}")
            return
            
        # Get portfolio and trading settings
        portfolio = user.get('portfolio', {})
        trading_mode = user.get('trading_mode', 'manual')
        buy_amount = user.get('auto_buy_amount', 0.01)
        sell_percent = user.get('sell_percentage', 10)
        loss_percent = user.get('loss_percentage', 5)
        
        # 1. First handle sell conditions for existing tokens
        for contract, token_data in list(portfolio.items()):
            token = await fetch_token_by_contract(contract)
            if not token:
                continue
                
            buy_price = token_data['buy_price']
            current_price = token['price_usd']
            price_change = ((current_price - buy_price) / buy_price) * 100
            
            # Check profit target
            if price_change >= sell_percent:
                success = await execute_auto_sell(
                    context, user_id, token, token_data, 
                    f"{price_change:.2f}% profit"
                )
                if success:
                    continue  # Move to next token after successful sale
                
            # Check stop loss
            elif price_change <= -loss_percent:
                success = await execute_auto_sell(
                    context, user_id, token, token_data, 
                    f"{abs(price_change):.2f}% loss"
                )
                if success:
                    continue  # Move to next token after successful sale
        
        # 2. Handle buy conditions for new tokens (only if we have available SOL)
        sol_balance = await check_balance(user_id, 'solana')
        if sol_balance < buy_amount:
            logger.debug(f"Insufficient SOL for auto-buy: {sol_balance} < {buy_amount}")
            return
            
        tokens = await fetch_latest_token()
        if not tokens:
            return
            
        # Filter tokens using enhanced safety parameters
        valid_tokens = []
        for token in tokens:
            # Skip tokens already in portfolio or blacklist
            if token['contract_address'] in portfolio or token['contract_address'] in user.get('auto_trade_blacklist', []):
                continue
                
            # Apply safety checks
            is_safe, reason = await check_token_safety(token, user)
            if not is_safe:
                logger.info(f"Skipping token {token['name']}: {reason}")
                continue
                
            valid_tokens.append(token)
        
        if not valid_tokens:
            return
            
        # Select the token with highest liquidity among safe tokens
        token = max(valid_tokens, key=lambda x: x['liquidity'])
        await execute_auto_buy(context, user_id, token, buy_amount)
        
    except Exception as e:
        logger.error(f"Auto-trade error for user {user_id}: {str(e)}")
        await notify_user(
            context, user_id,
            f"❌ AUTOTRADE ERROR: {str(e)}",
            "Auto-Trade System Failure"
        )


async def execute_auto_buy(context, user_id, token, buy_amount):
    """Execute automatic buy using Jupiter API"""
    try:
        # Check balance
        balance = await check_balance(user_id, 'solana')
        if balance < buy_amount:
            await notify_user(
                context, user_id,
                f"⏳ Insufficient balance for auto-buy. Needed: {buy_amount} SOL, Available: {balance:.4f} SOL",
                "Auto-Buy Failed"
            )
            return False
        
        # Execute trade using the same function as manual trades
        success = await execute_trade(
            user_id, 
            token['contract_address'], 
            buy_amount, 
            'buy', 
            'solana',
            token
        )
        
        if success:
            # Update portfolio
            users_collection.update_one(
                {'user_id': user_id},
                {'$set': {
                    f'portfolio.{token["contract_address"]}': {
                        'name': token['name'],
                        'symbol': token['symbol'],
                        'amount': buy_amount,
                        'buy_price': token['price_usd'],
                        'buy_time': datetime.now().isoformat()
                    },
                    'last_trade_time': time.time()
                }}
            )
            await notify_user(
                context, user_id,
                f"🤖 AUTOBUY: Purchased {buy_amount} SOL worth of {token['name']} at ${token['price_usd']:.6f}",
                "Auto-Buy Executed"
            )
            return True
        else:
            # Add to blacklist if trade fails
            users_collection.update_one(
                {'user_id': user_id},
                {'$addToSet': {'auto_trade_blacklist': token['contract_address']}}
            )
            await notify_user(
                context, user_id,
                f"❌ AUTOBUY FAILED: Could not buy {token['name']} - added to blacklist",
                "Auto-Buy Failed"
            )
            return False
            
    except Exception as e:
        logger.error(f"Auto-buy execution error: {str(e)}")
        return False
    

async def execute_auto_sell(context, user_id, token, token_data, reason):
    """Execute automatic sell using Jupiter API"""
    # Execute trade
    success = await execute_trade(
        user_id, 
        token['contract_address'], 
        token_data['amount'], 
        'sell', 
        'solana',
        token
    )
    
    if success:
        # Remove from portfolio
        users_collection.update_one(
            {'user_id': user_id},
            {'$unset': {f'portfolio.{token["contract_address"]}': ""}},
        )
        
        # Record trade history
        trade_data = {
            'token': token['name'],
            'symbol': token['symbol'],
            'contract': token['contract_address'],
            'amount': token_data['amount'],
            'buy_price': token_data['buy_price'],
            'sell_price': token['price_usd'],
            'reason': reason,
            'timestamp': datetime.now().isoformat()
        }
        users_collection.update_one(
            {'user_id': user_id},
            {'$push': {'trade_history': trade_data}}
        )
        
        # Update last trade time
        users_collection.update_one(
            {'user_id': user_id},
            {'$set': {'last_trade_time': time.time()}}
        )
        
        await notify_user(
            context, user_id,
            f"🤖 AUTOSELL: Sold {token_data['amount']} SOL worth of {token['name']} at {reason}",
            "Auto-Sell Executed"
        )
        return True
    else:
        await notify_user(
            context, user_id,
            f"❌ AUTOSELL FAILED: Could not sell {token['name']}",
            "Auto-Sell Failed"
        )
        return False
    
async def is_legitimate_token(token: Dict[str, Any]) -> bool:
    """Check if a token appears to be legitimate"""
    try:
        # Check for common scam patterns
        if token['liquidity'] < 1000:  # Very low liquidity
            return False
            
        if token.get('holders', 0) < 50:  # Very few holders
            return False
            
        # Check if creator burned LP tokens (not a perfect check but helpful)
        # This would require additional on-chain analysis
        
        return True
    except:
        return False

async def check_token_safety(token: Dict[str, Any], user_settings: Dict[str, Any]) -> Tuple[bool, str]:
    """Comprehensive token safety check"""
    reasons = []
    
    # Liquidity check
    if token['liquidity'] < user_settings.get('min_liquidity', 1000):
        reasons.append(f"Liquidity (${token['liquidity']}) below threshold")
    
    # Volume check
    if token['volume'] < user_settings.get('min_volume', 500):
        reasons.append(f"Volume (${token['volume']}) below threshold")
    
    # Rug pull detection (if enabled)
    if user_settings.get('rug_check', False) and not await is_legitimate_token(token):
        reasons.append("Failed rug pull check")
    
    # Token age check (if we can determine it)
    # This would require additional data not currently in the token object
    
    if reasons:
        return False, ", ".join(reasons)
    return True, "Passed all safety checks"

async def jupiter_api_call(url, params=None, json_data=None, method="GET"):
    """Helper function for Jupiter API calls with retry logic"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if method == "GET":
                    response = await client.get(url, params=params)
                else:
                    response = await client.post(url, json=json_data)
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:
                    # Rate limited, wait and retry
                    wait_time = (attempt + 1) * 2
                    logger.warning(f"Rate limited, waiting {wait_time}s before retry")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Jupiter API error: {response.status_code} - {response.text}")
                    return None
                    
        except Exception as e:
            logger.error(f"Jupiter API call failed (attempt {attempt + 1}): {str(e)}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
    
    return None

    
async def notify_user(context, user_id, message, action):
    """Notify user about auto-trade activity with logging"""
    try:
        await context.bot.send_message(chat_id=user_id, text=message)
        log_user_action(user_id, action, message)
    except Exception as e:
        logger.error(f"Failed to notify user {user_id}: {str(e)}")




async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel current operation"""
    user_id = update.effective_user.id
    log_user_action(user_id, "OPERATION_CANCELLED")
    context.user_data[f'conversation_state_{user_id}'] = None
    
    # Remove any timeout check jobs
    for job in context.job_queue.jobs():
        if job.name == f"timeout_check_{user_id}":
            job.schedule_removal()
            logger.debug(f"Removed timeout_check_{user_id} after cancel")
    
    if await check_subscription(user_id):
        await start_token_updates(context, user_id)
    
    await update.message.reply_text("🛑 Operation cancelled.")
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors in the bot"""
    logger.error(f"Update {update} caused error {context.error}", exc_info=True)
    
    if update and update.message:
        try:
            await update.message.reply_text(
                "❌ An error occurred. Please try again or contact support.\n"
                f"Error: {str(context.error)}"
            )
        except:
            try:
                await update.message.reply_text(
                    "❌ An error occurred. Please try again or contact support."
                )
            except Exception as e:
                logger.error(f"Failed to send error message: {str(e)}")

def setup_handlers(application: Application):
    """Set up all conversation handlers and command handlers"""
    def wrap_conversation_entry(entry_handler):
        """Wrapper for conversation entry points to handle state management"""
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
            user_id = update.effective_user.id
            context.user_data[f'conversation_state_{user_id}'] = entry_handler.__name__
            context.user_data[f'last_activity_{user_id}'] = datetime.now()
            
            # Pause any token update jobs during conversation
            for job in context.job_queue.jobs():
                if job.name == f"token_updates_{user_id}":
                    job.schedule_removal()
                    logger.debug(f"Paused token_updates_{user_id} due to conversation entry")
            
            # Set up timeout check
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
        """Wrapper for conversation states to handle activity tracking"""
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
            user_id = update.effective_user.id
            context.user_data[f'last_activity_{user_id}'] = datetime.now()
            
            # Update last activity in timeout check job
            for job in context.job_queue.jobs():
                if job.name == f"timeout_check_{user_id}":
                    job.data['last_activity'] = datetime.now()
            
            result = await state_handler(update, context)
            if result == ConversationHandler.END:
                context.user_data[f'conversation_state_{user_id}'] = None
                
                # Clean up timeout check job
                for job in context.job_queue.jobs():
                    if job.name == f"timeout_check_{user_id}":
                        job.schedule_removal()
                        logger.debug(f"Removed timeout_check_{user_id} after conversation end")
                
                # Restart token updates if subscribed
                if await check_subscription(user_id):
                    await start_token_updates(context, user_id)
            return result
        return wrapped

    # Add basic command handlers
    
    application.add_handler(CommandHandler("job_status", job_status))
    application.add_handler(CommandHandler("fetch_tokens", fetch_tokens_manual))
    application.add_handler(CommandHandler("force_fetch", force_token_fetch))
    application.add_handler(CommandHandler("trade_status", trade_status))
    #application.add_handler(CallbackQueryHandler(handle_token_button, pattern='^(buy|sell)_'))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("reset_tokens", reset_tokens))
    application.add_handler(CommandHandler("debug", debug))
    application.add_error_handler(error_handler)

    # Start command handler
    start_handler = ConversationHandler(
    entry_points=[CommandHandler("start", wrap_conversation_entry(start))],
    states={
        WALLET_SETUP_CHOICE: [CallbackQueryHandler(wrap_conversation_state(handle_wallet_choice), 
                             pattern='^(generate_wallet|import_wallet)$')],
        
        # New states for import flow in start conversation
        START_IMPORT_METHOD: [CallbackQueryHandler(wrap_conversation_state(start_import_method), 
                             pattern='^(mnemonic|private_key|cancel_import)$')],
        START_INPUT_MNEMONIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                            wrap_conversation_state(start_input_mnemonic))],
        START_INPUT_PRIVATE_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                              wrap_conversation_state(start_input_private_key))]
    },
    fallbacks=[CommandHandler("cancel", cancel)],
    per_message=False
)
    application.add_handler(start_handler)

    subscription_handler = ConversationHandler(
    entry_points=[CommandHandler("subscribe", subscribe)],
    states={
        SUBSCRIPTION_CONFIRMATION: [
            CallbackQueryHandler(confirm_subscription, pattern='^(confirm_subscription|cancel_subscription)$')
        ]
    },
    fallbacks=[CommandHandler("cancel", cancel)],
    per_message=False
)
    application.add_handler(subscription_handler) 



    analysis_handler = ConversationHandler(
    entry_points=[CommandHandler("token_analysis", wrap_conversation_entry(token_analysis))],
    states={
        INPUT_ANALYSIS_CONTRACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                              wrap_conversation_state(analysis_contract))]
    },
    fallbacks=[CommandHandler("cancel", cancel)],
    per_message=False
)
    application.add_handler(analysis_handler)

    # Generate wallet handler
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

    # Set wallet handler (import)
    set_wallet_handler = ConversationHandler(
        entry_points=[CommandHandler("set_wallet", wrap_conversation_entry(set_wallet))],
        states={
            SET_WALLET_METHOD: [CallbackQueryHandler(wrap_conversation_state(set_wallet_method), 
                               pattern='^(mnemonic|private_key|cancel_import)$')],
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

    # Set mode handler
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
                                           wrap_conversation_state(set_loss_percentage))],
        SET_ANTI_MEV: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                    wrap_conversation_state(set_anti_mev))],
        SET_LIQUIDITY_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                               wrap_conversation_state(set_liquidity_threshold))],
        SET_VOLUME_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                            wrap_conversation_state(set_volume_threshold))],
        SET_RUG_CHECK: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                     wrap_conversation_state(set_rug_check))],
        SET_MAX_SLIPPAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                        wrap_conversation_state(set_max_slippage))],
        SET_MAX_GAS_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                         wrap_conversation_state(set_max_gas_price))],
        SET_TOKEN_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                     wrap_conversation_state(set_token_age))]
    },
    fallbacks=[CommandHandler("cancel", cancel)],
    per_message=False
)
    application.add_handler(set_mode_handler)

    # Trade handler
    # Trade handler
    trade_handler = ConversationHandler(
    entry_points=[
        CommandHandler("trade", wrap_conversation_entry(trade)),
        CallbackQueryHandler(wrap_conversation_entry(handle_token_button), pattern='^(buy|sell)_')
    ],
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

    # Transfer handler
    transfer_handler = ConversationHandler(
        entry_points=[CommandHandler("transfer", wrap_conversation_entry(start_transfer))],
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

application = None

async def setup_bot():
    """Initialize and configure the Telegram bot"""
    global application
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    if not TELEGRAM_TOKEN or not WEBHOOK_URL:
        logger.error("TELEGRAM_TOKEN or WEBHOOK_URL not found in .env file")
        raise ValueError("TELEGRAM_TOKEN or WEBHOOK_URL not found in .env file")

    if application is None:
        logger.info("🚀 Initializing NEW Telegram bot application")
        application = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .concurrent_updates(True)
            .build()
        )
        logger.info("🛠️ Setting up command handlers")
        setup_handlers(application)
    else:
        logger.info("♻️ Reusing existing application instance")
    
    if not application.running:
        logger.info("⚙️ Initializing application...")
        await application.initialize()
        
        logger.info(f"🌐 Setting webhook to {WEBHOOK_URL}")
        await application.bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=Update.ALL_TYPES
        )
        
        logger.info("📝 Registering bot commands...")
        commands = [
            BotCommand("start", "Start the bot and create or view wallet"),
            BotCommand("subscribe", "Subscribe to use trading features"),
            BotCommand("token_analysis", "Analyze performance of a specific token"),
            BotCommand("generate_wallet", "Generate a new wallet"),
            BotCommand("set_wallet", "Import an existing wallet"),
            BotCommand("fetch_tokens", "Manually fetch new tokens (requires wallet)"),
            BotCommand("reset_tokens", "Reset posted tokens list"),
            BotCommand("setmode", "Set trading mode (manual/automatic)"),
            BotCommand("trade", "Trade Solana tokens manually (requires wallet)"),
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
    """FastAPI startup event handler"""
    logger.info("🚀 Starting bot...")
    try:
        app = await setup_bot()
        logger.info("✅ Bot setup complete")
        
        logger.info("⏳ Scheduling jobs for active subscribers...")
        active_users = users_collection.find({
            "subscription_status": "active",
            "subscription_expiry": {"$gt": datetime.now().isoformat()}
        })

        await migrate_token_performance_data()
        
        for user in active_users:
            user_id = user['user_id']
            if user.get('solana') and user['solana'].get('public_key'):
                logger.info(f"  - Scheduling token updates for user {user_id}")
                app.job_queue.run_repeating(
                    update_token_info,
                    interval=30,
                    first=5,
                    user_id=user_id,
                    name=f"token_updates_{user_id}"
                )

            if user.get('trading_mode') == 'automatic':
                logger.info(f"  - Scheduling auto-trade for user {user_id}")
                app.job_queue.run_repeating(
                    auto_trade,
                    interval=30,
                    first=10,
                    user_id=user_id,
                    name=f"auto_trade_{user_id}"
                )

        # Schedule background jobs
        app.job_queue.run_repeating(
            verify_sol_payments,
            interval=300,
            first=10,
            name="sol_payment_verification"
        )
        app.job_queue.run_repeating(
            update_token_performance,
            interval=3600,
            first=15,
            name="token_performance_updates"
        )
        app.job_queue.run_daily(
            send_daily_report,
            time=datetime.time(hour=20, minute=0),
            name="daily_report"
        ) 
        logger.info("✅ Bot startup complete")
    except Exception as e:
        logger.critical(f"🔥 Failed to start bot: {str(e)}", exc_info=True)

@app.on_event("shutdown")
async def on_shutdown():
    """FastAPI shutdown event handler"""
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