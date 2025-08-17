#!/usr/bin/env python3
from eth_account import Account
Account.enable_unaudited_hdwallet_features()
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
SUBSCRIPTION_SOL_AMOUNT = 0.01
PBKDF2_ROUNDS = 2048
GMGN_API_HOST = 'https://gmgn.ai'
DEXSCREENER_NEW_TOKENS_API = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN_API = "https://api.dexscreener.com/tokens/v1/solana/{token_address}"

# Bot states for conversation
(WALLET_SETUP_CHOICE, SET_TRADING_MODE, SET_AUTO_BUY_AMOUNT, SET_SELL_PERCENTAGE, SET_LOSS_PERCENTAGE, 
 SELECT_TOKEN, BUY_AMOUNT, CONFIRM_TRADE, TRANSFER_TOKEN, TRANSFER_AMOUNT, TRANSFER_ADDRESS,
 CONFIRM_NEW_WALLET, SET_WALLET_METHOD, INPUT_MNEMONIC, INPUT_PRIVATE_KEY, CONFIRM_SET_WALLET,
 SELECT_TOKEN_ACTION, SELL_AMOUNT, INPUT_CONTRACT,
 # New states for start flow
 START_IMPORT_METHOD, START_INPUT_MNEMONIC, START_INPUT_PRIVATE_KEY,SUBSCRIPTION_CONFIRMATION,INPUT_ANALYSIS_CONTRACT) = range(24)

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
                30,
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
            30,
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
            30,
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
    """Record initial token metrics when first posted"""
    contract_address = token['contract_address']
    
    # Check if already recorded
    if token_performance_collection.find_one({'contract_address': contract_address}):
        return
    
    # Record initial metrics
    token_performance_collection.insert_one({
        'contract_address': contract_address,
        'name': token.get('name', ''),
        'symbol': token.get('symbol', ''),
        'first_posted_at': datetime.now(),
        'initial_price': token['price_usd'],
        'initial_liquidity': token['liquidity'],
        'initial_market_cap': token.get('market_cap', 0),
        'all_time_high': token['price_usd'],
        'all_time_low': token['price_usd'],
        'performance_updates': []
    })
    logger.info(f"Recorded initial performance for {contract_address}")


async def update_token_performance(context: ContextTypes.DEFAULT_TYPE):
    """Periodically update token performance metrics"""
    logger.info("🔄 Updating token performance metrics...")
    
    # Get all tokens tracked
    tokens = token_performance_collection.find()
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        for token in tokens:
            contract_address = token['contract_address']
            try:
                # Fetch current token data
                current_data = await fetch_token_by_contract(contract_address)
                if not current_data:
                    continue
                
                # Calculate performance metrics
                current_price = current_data['price_usd']
                price_change = ((current_price - token['initial_price']) / token['initial_price']) * 100
                
                # Update all-time high/low
                new_ath = max(token['all_time_high'], current_price)
                new_atl = min(token['all_time_low'], current_price)
                
                # Add performance update
                update_data = {
                    'timestamp': datetime.now(),
                    'price': current_price,
                    'liquidity': current_data['liquidity'],
                    'market_cap': current_data.get('market_cap', 0),
                    'price_change_percent': price_change
                }
                
                # Update database
                token_performance_collection.update_one(
                    {'_id': token['_id']},
                    {
                        '$set': {
                            'all_time_high': new_ath,
                            'all_time_low': new_atl
                        },
                        '$push': {
                            'performance_updates': {
                                '$each': [update_data],
                                '$slice': -1000  # Keep last 1000 updates
                            }
                        }
                    }
                )
                
            except Exception as e:
                logger.error(f"Error updating performance for {contract_address}: {str(e)}")


async def token_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate token performance analysis"""
    user_id = update.effective_user.id
    log_user_action(user_id, "TOKEN_ANALYSIS_REQUEST")
    
    await update.message.reply_text(
        "🔍 Enter the token contract address for analysis:\n"
        "(e.g., 4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R)"
    )
    return INPUT_ANALYSIS_CONTRACT

async def analysis_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle token contract input for analysis"""
    user_id = update.effective_user.id
    contract_address = update.message.text.strip()
    log_user_action(user_id, "ANALYSIS_CONTRACT_INPUT", contract_address)
    
    # Fetch performance data
    token_data = token_performance_collection.find_one(
        {'contract_address': contract_address}
    )
    
    if not token_data:
        await update.message.reply_text(
            "❌ This token hasn't been tracked by the bot. "
            "Only tokens posted through the bot can be analyzed."
        )
        return ConversationHandler.END
    
    # Fetch current price
    current_token = await fetch_token_by_contract(contract_address)
    if not current_token:
        await update.message.reply_text("❌ Failed to fetch current token data.")
        return ConversationHandler.END
    
    # Calculate performance metrics
    current_price = current_token['price_usd']
    initial_price = token_data['initial_price']
    price_change = ((current_price - initial_price) / initial_price) * 100
    
    # Determine performance tier
    multiplier = current_price / initial_price
    if multiplier >= 10:
        performance = "10X+ 🚀🌕"
    elif multiplier >= 5:
        performance = "5X+ 🔥"
    elif multiplier >= 2:
        performance = "2X+ ⬆️"
    elif multiplier >= 1:
        performance = "1X ➡️"
    else:
        loss_percent = (1 - multiplier) * 100
        performance = f"DOWN {loss_percent:.1f}% ⚠️"

    # Calculate time since first post
    time_posted = token_data['first_posted_at']
    time_elapsed = datetime.now() - time_posted
    days_elapsed = time_elapsed.days
    hours_elapsed = time_elapsed.seconds // 3600
    
    # Format message
    message = (
        f"📊 *Token Performance Analysis*\n\n"
        f"🔹 *Token:* {token_data['name']} ({token_data['symbol']})\n"
        f"🔹 *Contract:* `{contract_address}`\n\n"
        f"⏱️ *Tracked Since:* {time_posted.strftime('%Y-%m-%d %H:%M')} "
        f"({days_elapsed}d {hours_elapsed}h ago)\n\n"
        f"💰 *Price Performance*\n"
        f"  - Initial: ${initial_price:.8f}\n"
        f"  - Current: ${current_price:.8f}\n"
        f"  - Change: {price_change:+.2f}% ({performance})\n"
        f"  - All-Time High: ${token_data['all_time_high']:.8f}\n"
        f"  - All-Time Low: ${token_data['all_time_low']:.8f}\n\n"
        f"💧 *Liquidity*\n"
        f"  - Initial: ${token_data['initial_liquidity']:,.2f}\n"
        f"  - Current: ${current_token['liquidity']:,.2f}\n\n"
        f"📈 *Market Cap*\n"
        f"  - Initial: ${token_data.get('initial_market_cap', 0):,.2f}\n"
        f"  - Current: ${current_token.get('market_cap', 0):,.2f}\n\n"
        f"[View Chart]({current_token['dexscreener_url']})"
    )
    
    await update.message.reply_text(
        message, 
        parse_mode='Markdown', 
        disable_web_page_preview=True
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
        
        # Create transaction
        txn = Transaction()
        txn.add(
            transfer(
                TransferParams(
                    from_pubkey=keypair.pubkey(),
                    to_pubkey=to_pubkey,
                    lamports=amount_lamports
                )
            )
        )
        
        # Set required fields
        txn.recent_blockhash = recent_blockhash
        txn.fee_payer = keypair.pubkey()
        
        # Sign transaction
        txn.sign(keypair)
        
        # Send transaction with proper options
        tx_hash = await solana_client.send_raw_transaction(
            txn.serialize(),
            opts=TxOpts(skip_confirmation=False, preflight_commitment="confirmed")
        )
        
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
        
        user = users_collection.find_one({'user_id': user_id})
        await update.message.reply_text(
            f"✅ *Automatic trading settings saved:*\n\n"
            f"🔹 Auto-buy amount: {user['auto_buy_amount']} SOL\n"
            f"🔹 Sell at: {user['sell_percentage']}% profit\n"
            f"🔹 Stop-loss at: {user['loss_percentage']}% loss\n\n"
            f"The bot will now automatically trade based on these settings.",
            parse_mode='Markdown'
        )
        
        if user['trading_mode'] == 'automatic':
            context.job_queue.run_repeating(
                auto_trade, 
                interval=5, 
                first=0, 
                user_id=user_id,
                name=f"auto_trade_{user_id}"
            )
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ Invalid percentage. Please enter a number.")
        return SET_LOSS_PERCENTAGE

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
    """Fetch token details by contract address from DexScreener"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            if not contract_address:
                logger.error("Contract address is empty")
                return None
                
            logger.info(f"Fetching token by contract: {contract_address}")
            token_url = DEXSCREENER_TOKEN_API.format(token_address=contract_address)
            logger.debug(f"Calling DexScreener API: {token_url}")
            response = await client.get(token_url, headers=headers)
            
            if response.status_code != 200:
                logger.error(f"Token API failed: {response.status_code} - {response.text}")
                return None
            
            data = response.json()
            logger.debug(f"API response: {json.dumps(data, indent=2)[:500]}...")
            
            if not isinstance(data, list) or not data:
                logger.error(f"Unexpected response format: {type(data)}")
                return None
            
            pair = next((p for p in data if p.get('chainId') == 'solana'), None)
            if not pair:
                logger.error(f"No Solana pairs found for token {contract_address}")
                return None
                
            base_token = pair.get('baseToken', {})
            quote_token = pair.get('quoteToken', {})
            
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
            
        available = portfolio[contract_address]['amount']
        await query.message.reply_text(
            f"💸 *Sell Order for {token['name']} ({token['symbol']})*\n\n"
            f"Available: {available:.4f} SOL worth\n"
            f"Enter amount to sell in SOL:",
            parse_mode='Markdown'
        )
        return SELL_AMOUNT

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
    """Handle sell amount input with balance check and trade execution"""
    user_id = update.effective_user.id
    try:
        amount = float(update.message.text)
        if amount <= 0:
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
        if amount > token_data['amount']:
            await update.message.reply_text(
                f"❌ Insufficient token balance. Available: {token_data['amount']:.4f} SOL worth\n"
                f"You requested to sell {amount:.4f} SOL worth."
            )
            return SELL_AMOUNT
        
        # Execute trade immediately
        await update.message.reply_text(f"⏳ Executing sell order for {amount:.4f} SOL worth of {token['name']}...")
        success = await execute_trade(user_id, token['contract_address'], amount, 'sell', 'solana', token)
        
        if success:
            # Update portfolio
            new_amount = token_data['amount'] - amount
            
            if new_amount <= 0.001:  # Consider dust amounts as zero
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$unset': {f'portfolio.{token["contract_address"]}': ""}}
                )
                log_user_action(user_id, "POSITION_CLOSED", f"Sold all {token['name']}")
            else:
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$set': {f'portfolio.{token["contract_address"]}.amount': new_amount}}
                )
            
            # Calculate profit/loss
            buy_price = token_data['buy_price']
            current_price = token['price_usd']
            price_change = ((current_price - buy_price) / buy_price) * 100
            profit_loss = "profit" if price_change >= 0 else "loss"
            
            log_user_action(user_id, "TRADE_EXECUTED", f"Sold {amount} SOL worth of {token['name']}")
            await update.message.reply_text(
                f"✅ Successfully sold {amount:.4f} SOL worth of {token['name']} at ${token['price_usd']:.6f}.\n"
                f"📈 This trade resulted in a {abs(price_change):.2f}% {profit_loss}.\n"
                f"📊 You now hold {new_amount:.4f} SOL worth of {token['symbol']}."
            )
        else:
            log_user_action(user_id, "TRADE_FAILED", f"Sell {amount} SOL of {token['name']}", level="error")
            await update.message.reply_text("❌ Trade failed. Please try again later.")
        
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
    """Show wallet balances"""
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
        for contract, details in portfolio.items():
            message += (
                f"🔸 {details['name']} ({details['symbol']}): "
                f"{details['amount']:.4f} SOL worth (bought at ${details['buy_price']:.6f})\n"
            )
    
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
    """Execute a trade on the specified chain using GMGN API"""
    logger.info(f"🏁 Starting {action} trade for {amount} SOL of {contract_address}")
    
    if chain != 'solana':
        logger.error(f"Trading not supported for {chain} yet")
        return False
    
    user = users_collection.find_one({'user_id': user_id})
    if not user:
        logger.error(f"No user found for user_id {user_id}")
        return False
    
    try:
        # Decrypt and load wallet
        decrypted_user = await decrypt_user_wallet(user_id, user)
        solana_private_key = decrypted_user['solana']['private_key']
        if not solana_private_key or solana_private_key == "[Decryption Failed]":
            logger.error(f"Failed to decrypt Solana private key for user {user_id}")
            return False
        
        keypair = Keypair.from_bytes(base58.b58decode(solana_private_key))
        from_address = str(keypair.pubkey())
        
        # Set token_in and token_out based on trade action
        if action == 'buy':
            token_in = 'So11111111111111111111111111111111111111112'  # SOL
            token_out = contract_address
            in_amount = int(amount * 1_000_000_000)  # Convert SOL to lamports
            swap_mode = 'ExactIn'
        else:  # Sell
            token_in = contract_address
            token_out = 'So11111111111111111111111111111111111111112'  # SOL
            out_amount = int(amount * 1_000_000_000)  # Convert SOL to lamports
            swap_mode = 'ExactOut'
        
        # Prepare API request
        quote_url = f"{GMGN_API_HOST}/defi/router/v1/sol/tx/get_swap_route"
        params = {
            'token_in_address': token_in,
            'token_out_address': token_out,
            'from_address': from_address,
            'slippage': '1.0',  # 1% slippage
            'swap_mode': swap_mode
        }
        
        # Add amount based on trade type
        if action == 'buy':
            params['in_amount'] = str(in_amount)
        else:
            params['out_amount'] = str(out_amount)
        
        logger.debug(f"🔄 GMGN API params: {params}")
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Get swap quote
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
            
            # Extract transaction details
            swap_transaction = route['data']['raw_tx']['swapTransaction']
            last_valid_block_height = route['data']['raw_tx']['lastValidBlockHeight']
            
            # Deserialize and sign transaction
            swap_transaction_buf = base64.b64decode(swap_transaction)
            transaction = VersionedTransaction.deserialize(swap_transaction_buf)
            transaction.sign([keypair])
            signed_tx = base64.b64encode(transaction.serialize()).decode('utf-8')
            
            # Submit transaction
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
            
            # Wait for confirmation
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

async def auto_trade(context: ContextTypes.DEFAULT_TYPE, user_id: int, token: dict):
    """Handle automatic trading based on user settings"""
    logger.info(f"🤖 Auto-trading for user {user_id} - {token['name']}")
    user = users_collection.find_one({'user_id': user_id})
    if not await check_subscription(user_id):
        return
    
    # Check if we already hold this token
    if token['contract_address'] in user['portfolio']:
        buy_price = user['portfolio'][token['contract_address']]['buy_price']
        current_price = token['price_usd']
        price_change = ((current_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0
        
        # Check if we should sell for profit
        if price_change >= user['sell_percentage']:
            success = await execute_trade(
                user_id, 
                token['contract_address'], 
                user['portfolio'][token['contract_address']]['amount'], 
                'sell', 
                'solana'
            )
            if success:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🤖 Sold {token['name']} at {price_change:.2f}% profit!"
                )
                log_user_action(user_id, "AUTO_SELL_PROFIT", 
                              f"{token['name']} at {price_change:.2f}% profit")
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$unset': {f'portfolio.{token["contract_address"]}': ""}}
                )
        
        # Check if we should sell to stop loss
        elif price_change <= -user['loss_percentage']:
            success = await execute_trade(
                user_id, 
                token['contract_address'], 
                user['portfolio'][token['contract_address']]['amount'], 
                'sell', 
                'solana'
            )
            if success:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🤖 Stopped loss for {token['name']} at {price_change:.2f}% loss."
                )
                log_user_action(user_id, "AUTO_SELL_LOSS", 
                              f"{token['name']} at {price_change:.2f}% loss")
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$unset': {f'portfolio.{token["contract_address"]}': ""}}
                )
        return

    # Check if we should buy this token
    balance = await check_balance(user_id, 'solana')
    if balance < user['auto_buy_amount']:
        logger.debug(f"Insufficient balance for auto-buy for user {user_id}: {balance} SOL available")
        return

    success = await execute_trade(
        user_id, 
        token['contract_address'], 
        user['auto_buy_amount'], 
        'buy', 
        'solana'
    )
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
        log_user_action(user_id, "AUTO_BUY", 
                       f"{user['auto_buy_amount']} SOL of {token['name']}")

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
    application.add_handler(CallbackQueryHandler(handle_token_button, pattern='^(buy|sell)_'))
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
                                               wrap_conversation_state(set_loss_percentage))]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    application.add_handler(set_mode_handler)

    # Trade handler
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
        
        for user in active_users:
            user_id = user['user_id']
            if user.get('solana') and user['solana'].get('public_key'):
                logger.info(f"  - Scheduling job for user {user_id}")
                app.job_queue.run_repeating(
                    update_token_info,
                    interval=30,
                    first=5,
                    user_id=user_id,
                    name=f"token_updates_{user_id}"
                )
                app.job_queue.run_repeating(
    verify_sol_payments,
    interval=300,  # every 5 minutes
    first=10,
    name="sol_payment_verification"
)
                app.job_queue.run_repeating(
    update_token_performance,
    interval=3600,  # Update hourly
    first=10,
    name="token_performance_updates"
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