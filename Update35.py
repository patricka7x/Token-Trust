import logging
import requests
import re
import time
import os
import json
import asyncio
import math
from decimal import Decimal
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, CallbackContext
from datetime import datetime, timedelta
import sqlite3
import stripe
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn
from threading import Thread

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY")
BASESCAN_API_KEY = os.getenv("BASESCAN_API_KEY")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")
STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL", "https://example.com/thanks-premium-activates-soon")
STRIPE_CANCEL_URL = os.getenv("STRIPE_CANCEL_URL", "https://example.com/cancel")
DEV_USER_ID = int(os.getenv("DEV_USER_ID", 0))
FALLBACK_PREMIUM_LINK = "https://buy.stripe.com/your-static-link"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(name)

stripe.api_key = STRIPE_SECRET_KEY

conn = sqlite3.connect('tokentrust.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, is_premium BOOLEAN DEFAULT FALSE, subscription_id TEXT, expiry_date DATETIME, total_scans INTEGER DEFAULT 0)''')
c.execute('''CREATE TABLE IF NOT EXISTS alerts (user_id INTEGER, addr TEXT, set_value TEXT, percent REAL, direction TEXT, name TEXT, alert_type TEXT, PRIMARY KEY (user_id, addr))''')
conn.commit()

api_app = FastAPI()

@api_app.get("/")
@api_app.get("/health")
async def root():
return {"status": "OK"}

@api_app.post("/webhook")
async def webhook(request: Request):
payload = await request.body()
sig_header = request.headers.get('stripe-signature')
try:
event = stripe.Webhook.construct_event(
payload, sig_header, STRIPE_WEBHOOK_SECRET
)
except ValueError as e:
logger.error(f"Webhook payload invalid: {e}")
raise HTTPException(status_code=400, detail="Invalid payload")
except stripe.error.SignatureVerificationError as e:
logger.error(f"Webhook signature verification failed: {e}")
raise HTTPException(status_code=400, detail="Invalid signature")

if event['type'] == 'checkout.session.completed':
session = event['data']['object']
user_id = int(session['client_reference_id'])
sub_id = session['subscription']
expiry = datetime.now() + timedelta(days=30)
c.execute("UPDATE users SET is_premium = TRUE, subscription_id = ?, expiry_date = ? WHERE user_id = ?",
(sub_id, expiry.strftime('%Y-%m-%d %H:%M:%S'), user_id))
conn.commit()
logger.info(f"Premium activated for user {user_id} via checkout completed (sub: {sub_id})")

elif event['type'] == 'invoice.payment_succeeded':
invoice = event['data']['object']
sub_id = invoice['subscription']
c.execute("SELECT user_id, expiry_date FROM users WHERE subscription_id = ?", (sub_id,))
row = c.fetchone()
if row:
user_id, exp = row
new_expiry = datetime.strptime(exp, '%Y-%m-%d %H:%M:%S') + timedelta(days=30)
c.execute("UPDATE users SET expiry_date = ? WHERE user_id = ?", (new_expiry.strftime('%Y-%m-%d %H:%M:%S'), user_id))
conn.commit()
logger.info(f"Subscription renewed for user {user_id} (sub: {sub_id})")

elif event['type'] == 'customer.subscription.deleted':
sub = event['data']['object']
sub_id = sub['id']
c.execute("UPDATE users SET is_premium = FALSE, subscription_id = NULL, expiry_date = NULL WHERE subscription_id = ?", (sub_id,))
conn.commit()
logger.info(f"Subscription canceled for sub {sub_id}")

return JSONResponse({'status': 'success'})

def is_premium(user_id):
c.execute("SELECT is_premium, expiry_date, subscription_id FROM users WHERE user_id = ?", (user_id,))
row = c.fetchone()
if row:
is_p, exp, sub_id = row
if is_p and exp and datetime.strptime(exp, '%Y-%m-%d %H:%M:%S') > datetime.now():
return True
if sub_id:
try:
sub = stripe.Subscription.retrieve(sub_id)
if sub.status == 'active':
new_expiry = datetime.now() + timedelta(days=30)
c.execute("UPDATE users SET expiry_date = ?, is_premium = TRUE WHERE user_id = ?", (new_expiry.strftime('%Y-%m-%d %H:%M:%S'), user_id))
conn.commit()
logger.info(f"Premium reinstated via poll for user {user_id}")
return True
elif sub.status in ['canceled', 'incomplete_expired']:
c.execute("UPDATE users SET is_premium = FALSE, subscription_id = NULL, expiry_date = NULL WHERE user_id = ?", (user_id,))
conn.commit()
except Exception as e:
logger.error(f"Stripe check failed for {user_id}: {e}")
c.execute("UPDATE users SET is_premium = FALSE WHERE user_id = ?", (user_id,))
conn.commit()
return False

def can_scan(user_id):
if is_premium(user_id):
return True
c.execute("SELECT total_scans FROM users WHERE user_id = ?", (user_id,))
row = c.fetchone()
if not row:
c.execute("INSERT INTO users (user_id, total_scans) VALUES (?, 0)", (user_id,))
conn.commit()
return True
total_scans = row[0]
if total_scans < 3:
c.execute("UPDATE users SET total_scans = total_scans + 1 WHERE user_id = ?", (user_id,))
conn.commit()
return True
return False

def create_checkout_session(user_id):
try:
session = stripe.checkout.Session.create(
payment_method_types=['card'],
line_items=[{
'price': STRIPE_PRICE_ID,
'quantity': 1,
}],
mode='subscription',
success_url=STRIPE_SUCCESS_URL,
cancel_url=STRIPE_CANCEL_URL,
client_reference_id=str(user_id),
metadata={'user_id': str(user_id)}
)
logger.info(f"Checkout created for {user_id}: {session.url}")
return session.url
except Exception as e:
logger.error(f"Checkout error for {user_id}: {e}")
return None

subscript_map = str.maketrans('0123456789', '₀₁₂₃₄₅₆₇₈₉')

def format_price(price_str):
if price_str == "0":
return "$0"
try:
p = Decimal(price_str)
if p >= Decimal('0.001') or p <= 0:
return f"${p:.4f}"
order = math.floor(math.log10(float(p)))
e = -order
mant = p / Decimal(10)**order
mant_str = f"{mant:.4f}"[:5].replace('.', '')
subscript_num = str(e - 1).translate(subscript_map)
return f"$0.0{subscript_num}{mant_str}"
except Exception as e:
logger.warning(f"Price format error: {e}")
return f"${price_str}"

def format_large_number(num_str):
if num_str in ("0", "Unknown"):
return "Unknown"
try:
n = float(num_str)
if n >= 1e12:
return f"{n / 1e12:.1f} trillion"
elif n >= 1e9:
return f"{n / 1e9:.1f} billion"
elif n >= 1e6:
return f"{n / 1e6:.1f} million"
elif n >= 1e3:
return f"{n / 1e3:.1f} thousand"
else:
return f"{int(n)}"
except Exception as e:
logger.warning(f"Supply format error: {e}")
return num_str

def get_current_price(addr):
ds = dexscreener_data(addr)
if ds:
return ds.get("priceUsd", "0")
return "0"

def get_current_liquidity(addr):
ds = dexscreener_data(addr)
if ds:
return ds.get("liquidity", "0")
return "0"

def is_solana_address(addr):
return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", addr))

def detect_admin_controls(source_code: str) -> list:
risky = ["mint(", "pause(", "unpause(", "upgradeTo(", "setOwner(", "renounceOwnership(", "transferOwnership(", "blacklist(", "whitelist(", "setMaxTxAmount(", "burn("]
found = []
src = source_code.lower()
for f in risky:
if f in src:
found.append(f.rstrip('('))
return found

def fetch_audit_info(addr):
return {"audited": False, "audit_url": None, "audit_notes": "No audit info found."}

def get_launch_date(addr, chain):
if chain not in ["eth", "bsc", "base"]:
return None, None, None
api_bases = {"eth": "api.etherscan.io", "bsc": "api.bscscan.com", "base": "api.basescan.org"}
api_keys = {"eth": ETHERSCAN_API_KEY, "bsc": BSCSCAN_API_KEY, "base": BASESCAN_API_KEY or ETHERSCAN_API_KEY}
base = api_bases.get(chain)
api_key = api_keys.get(chain)
if not base or not api_key:
return None, None, None
url = f"https://{base}/api?module=account&action=txlist&address={addr}&startblock=0&endblock=99999999&page=1&offset=1&sort=asc&apikey={api_key}"
try:
r = requests.get(url, timeout=10).json()
if r["status"] == "1" and r["result"]:
tx = r["result"][0]
timestamp = int(tx["timeStamp"])
age_seconds = time.time() - timestamp
age_days = int(age_seconds / 86400)
age_hours = int((age_seconds % 86400) / 3600) if age_days == 0 else None
launch_date = time.strftime("%B %d, %Y", time.localtime(timestamp))
return launch_date, age_days, age_hours
except Exception as e:
logger.warning(f"Launch date fetch error: {e}")
return None, None, None

def etherscan_data(addr, chain):
api = ETHERSCAN_API_KEY if chain == "eth" else BSCSCAN_API_KEY if chain == "bsc" else BASESCAN_API_KEY or ETHERSCAN_API_KEY
base = "https://api.etherscan.io/api" if chain == "eth" else "https://api.bscscan.com/api" if chain == "bsc" else "https://api.basescan.org/api"
try:
r = requests.get(f"{base}?module=contract&action=getsourcecode&address={addr}&apikey={api}", timeout=10).json()
if r.get("status") == "1" and r.get("result"):
info = r["result"][0]
verified = info.get("SourceCode") not in (None, "", "Contract source code not verified")
return {
"verified": verified,
"name": info.get("ContractName", "Unknown"),
"symbol": "N/A",
"source_code": info.get("SourceCode", "")
}
except Exception as e:
logger.warning(f"{chain.capitalize()}scan error: {e}")
return {}

def solana_data(addr):
if not HELIUS_API_KEY:
logger.error("Missing HELIUS_API_KEY in environment.")
return {}
try:
headers = {"Content-Type": "application/json"}
body = {"jsonrpc":"2.0","id":1,"method":"getAsset","params":{"id":addr}}
r = requests.post(f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}", json=body, headers=headers, timeout=10).json()
res = r.get("result", {})
content = res.get("content", {})
metadata = content.get("metadata", {})
description = None
socials = []
image = None
json_uri = content.get("json_uri")
if json_uri:
try:
offchain = requests.get(json_uri, timeout=5).json()
description = offchain.get("description")
image = offchain.get("image")
twitter = offchain.get("twitter") or offchain.get("extensions", {}).get("twitter")
if twitter:
socials.append({"type": "twitter", "url": twitter})
telegram = offchain.get("telegram") or offchain.get("extensions", {}).get("telegram")
if telegram:
socials.append({"type": "telegram", "url": telegram})
website = offchain.get("website") or offchain.get("extensions", {}).get("website")
if website:
socials.append({"type": "website", "url": website})
offchain_socials = offchain.get("socials", [])
if isinstance(offchain_socials, list):
socials.extend(offchain_socials)
elif isinstance(offchain_socials, dict):
for typ, url in offchain_socials.items():
socials.append({"type": typ, "url": url})
except Exception as e:
logger.warning(f"Offchain metadata fetch error: {e}")

supply_body = {"jsonrpc":"2.0","id":1,"method":"getTokenSupply","params":{"mint":addr}}
supply_r = requests.post(f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}", json=supply_body, headers=headers, timeout=10).json()
total_supply = supply_r.get("result", {}).get("value", {}).get("amount", "0")

largest_body = {"jsonrpc":"2.0","id":1,"method":"getTokenLargestAccounts","params":{"mint":addr}}
largest_r = requests.post(f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}", json=largest_body, headers=headers, timeout=10).json()
largest = largest_r.get("result", {}).get("value", [])
holders = []
if total_supply != "0":
total = float(total_supply)
for acc in largest[:5]:
amount = float(acc.get("amount", "0"))
pct = (amount / total * 100) if total > 0 else 0
holders.append({"address": acc.get("address", "Unknown"), "percent": pct})

return {
"name": metadata.get("name", "Unknown"),
"symbol": metadata.get("symbol", "N/A"),
"verified": bool(content),
"source_code": "",
"description": description,
"socials": socials,
"total_supply": total_supply,
"image": image,
"mint_authority": res.get("mint_authority"),
"freeze_authority": res.get("freeze_authority"),
"holders": holders
}
except Exception as e:
logger.warning(f"Helius error: {e}")
return {}

def dexscreener_data(addr):
try:
r = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={addr}", timeout=10).json()
pairs = r.get("pairs", [])
if pairs:
p = pairs[0]
info = p.get("info", {})
vol = p.get("volume", {}).get("h24") or "0"
liq = p.get("liquidity", {}).get("usd") or "0"
fdv = p.get("fdv") or "0"
cca = p.get("pairCreatedAt") or 0
age_seconds = time.time() - (int(cca)/1000) if cca else None
age_days = int(age_seconds / 86400) if age_seconds else None
age_hours = int((age_seconds % 86400) / 3600) if age_seconds and age_days == 0 else None
price_change_24h = p.get("priceChange", {}).get("h24") or 0
return {
"volume": str(vol),
"liquidity": str(liq),
"fdv": str(fdv),
"age_days": age_days,
"age_hours": age_hours,
"chart": p.get("url"),
"chainId": p.get("chainId", "").lower(),
"priceUsd": p.get("priceUsd", "0"),
"socials": info.get("socials", []),
"websites": info.get("websites", []),
"image": info.get("imageUrl"),
"price_change_24h": price_change_24h,
"baseToken": p.get("baseToken", {})
}
except Exception as e:
logger.warning(f"DexScreener error: {e}")
return None

def goplus_data(addr, chain):
chain_ids = {"eth": "1", "bsc": "56", "sol": "solana", "base": "8453"}
cid = chain_ids.get(chain)
if not cid:
logger.warning(f"Unsupported chain '{chain}' for GoPlus API")
return None

base_url = "https://api.gopluslabs.io/api/v1/token_security"
if chain == "sol":
url = f"{base_url}/solana?contract_addresses={addr}"
else:
url = f"{base_url}/{cid}?contract_addresses={addr}"

try:
response = requests.get(url, timeout=10)
response.raise_for_status()
data = response.json()

if "result" not in data:
logger.warning(f"GoPlus API returned unexpected response: {data}")
return None

token_data = data.get("result", {}).get(addr.lower())
if not token_data:
logger.warning(f"No data found for address {addr} on chain {chain}")
return None

holders = token_data.get("holders", [])
max_pct = 0
if holders and isinstance(holders, list):
for h in holders:
pct = h.get("percent", 0)
if isinstance(pct, (str, float)):
max_pct = max(max_pct, float(pct))
else:
max_pct = float(token_data.get("creator_percent", "0")) or float(token_data.get("owner_percentage", 0))
token_data["max_holder_percent"] = max_pct * 100

token_data["is_honeypot"] = token_data.get("is_honeypot") == "1" or token_data.get("cannot_sell_all") == "1"
token_data["buy_tax"] = float(token_data.get("buy_tax", 0)) * 100 if token_data.get("buy_tax") else 0
token_data["sell_tax"] = float(token_data.get("sell_tax", "0")) * 100 if token_data.get("sell_tax") else 0
token_data["is_proxy"] = token_data.get("is_proxy") == "1"
token_data["liquidity_locked"] = token_data.get("lp_locked") == "1" or float(token_data.get("locked_percentage", "0")) > 0
token_data["locked_percentage"] = float(token_data.get("locked_percentage", "0"))
token_data["ownership_renounced"] = (
token_data.get("owner_address") in (None, "", "0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead")
or token_data.get("is_mintable") == "0"
)
token_data["holder_count"] = int(token_data.get("holder_count", 0))
token_data["lp_holder_count"] = len(token_data.get("lp_holders", []))
token_data["total_supply"] = token_data.get("total_supply", "0")
token_data["is_mintable"] = token_data.get("is_mintable") == "1"
token_data["transfer_pausable"] = token_data.get("transfer_pausable") == "1"
token_data["has_blacklist"] = token_data.get("is_blacklisted") == "1"
token_data["has_whitelist"] = token_data.get("is_whitelisted") == "1"
token_data["is_anti_whale"] = token_data.get("is_anti_whale") == "1"

if chain == "sol":
token_data["ownership_renounced"] = token_data.get("mint_authority") is None and token_data.get("freeze_authority") is None
token_data["buy_tax"] = 0
token_data["sell_tax"] = 0
token_data["total_supply"] = token_data.get("supply", "0")
token_data["top_10_holder_rate"] = float(token_data.get("top_10_holder_rate", "0"))

return token_data

except requests.exceptions.HTTPError as e:
logger.warning(f"GoPlus API HTTP error: {e}, status code: {response.status_code}")
return None
except requests.exceptions.RequestException as e:
logger.warning(f"GoPlus API request failed: {e}")
return None
except ValueError as e:
logger.warning(f"GoPlus API response parsing error: {e}")
return None
except Exception as e:
logger.warning(f"Unexpected error in GoPlus API: {e}")
return None

def get_risk_label(score):
if score <= 20:
return "✅ Very Low"
if score <= 40:
return "🟢 Low"
if score <= 60:
return "🟡 Medium"
if score <= 80:
return "🟠 High"
return "🔴 Very High"

def coingecko_description(addr, chain="eth"):
try:
addr = addr.lower()
platform = {"eth": "ethereum", "bsc": "binance-smart-chain", "base": "base"}.get(chain)
if not platform:
return None
url = f"https://api.coingecko.com/api/v3/coins/{platform}/contract/{addr}"
data = requests.get(url, timeout=10).json()
if "error" in data:
return None
desc = data.get("description", {}).get("en", "").strip()
tags = data.get("categories") or []
mcap = data.get("market_data", {}).get("market_cap", {}).get("usd", 0)
image = data.get("image", {}).get("large") or data.get("image", {}).get("small")
price_change_24h = data.get("market_data", {}).get("price_change_percentage_24h", 0)
price_change_7d = data.get("market_data", {}).get("price_change_percentage_7d", 0)
return {
"description": desc,
"tags": tags,
"name": data.get("name"),
"symbol": data.get("symbol"),
"market_cap": mcap,
"image": image,
"links": data.get("links", {}),
"price_change_24h": price_change_24h,
"price_change_7d": price_change_7d
}
except Exception as e:
logger.warning(f"CoinGecko error: {e}")
return None

def meme_context(name, symbol):
name = name.lower()
if "pepe" in name:
return "Pepe is a meme frog that originated on 4chan and became a symbol in crypto culture."
if "doge" in name or "shib" in name:
return "Doge and Shiba Inu are dog-based memes and icons of meme coins."
if "elon" in name:
return "This token may reference Elon Musk, who often influences meme coins."
return None

def extract_description_from_website(url):
try:
r = requests.get(url, timeout=10)
if r.status_code != 200:
return None
paragraphs = re.findall(r'<p[^>]>(.?)

', r.text, re.DOTALL) desc = ' '.join([p.strip() for p in paragraphs if len(p.strip()) > 50]) if desc: return desc[:2000] except Exception as e: logger.warning(f"Website description fetch error for {url}: {e}") return None
def get_socials(addr, chain):
try:
ds = dexscreener_data(addr)
cg = coingecko_description(addr, chain) if chain != 'sol' else None
ed_socials = []
if chain == 'sol':
ed = solana_data(addr)
ed_socials = ed.get("socials", [])

socials_dict = {}
if ds and ds.get("socials"):
for s in ds["socials"]:
typ = s.get("type", "").lower()
url = s.get("url", "").split('?')[0].rstrip('/')
if not url or not typ:
continue
if typ == "x":
typ = "twitter"
if "tiktok.com" in url:
typ = "tiktok"
elif "discord.com" in url or "discord.gg" in url:
typ = "discord"
elif "reddit.com" in url:
typ = "reddit"
socials_dict[typ] = url

if ds and ds.get("websites"):
for w in ds["websites"]:
url = w.get("url", "").split('?')[0].rstrip('/')
if url and "website" not in socials_dict:
socials_dict["website"] = url

if cg and cg.get("links"):
links = cg["links"]
homepage = links.get("homepage", [])
if homepage and homepage[0]:
url = homepage[0].split('?')[0].rstrip('/')
if url and "website" not in socials_dict:
socials_dict["website"] = url
twitter = links.get("twitter_screen_name")
if twitter and "twitter" not in socials_dict:
url = f"https://twitter.com/{twitter}".rstrip('/')
socials_dict["twitter"] = url
telegram = links.get("telegram_channel_identifier")
if telegram and "telegram" not in socials_dict:
url = f"https://t.me/{telegram}".rstrip('/')
socials_dict["telegram"] = url
discord = links.get("chat_url", [])
if discord and discord[0] and 'discord' in discord[0].lower() and "discord" not in socials_dict:
url = discord[0].split('?')[0].rstrip('/')
socials_dict["discord"] = url
reddit = links.get("reddit_url")
if reddit and "reddit" not in socials_dict:
url = reddit.split('?')[0].rstrip('/')
socials_dict["reddit"] = url

for s in ed_socials:
typ = s.get("type", "").lower()
url = s.get("url", "").split('?')[0].rstrip('/')
if not url or not typ:
continue
if typ == "x":
typ = "twitter"
if "tiktok.com" in url:
typ = "tiktok"
elif "discord.com" in url or "discord.gg" in url:
typ = "discord"
elif "reddit.com" in url:
typ = "reddit"
if typ not in socials_dict:
socials_dict[typ] = url

valid_socials = []
for typ, url in socials_dict.items():
if url and re.match(r'^https?://(?:[a-zA-Z0-9-]+.)+[a-zA-Z]{2,}(/.*)?$', url):
valid_socials.append({"type": typ, "url": url})
else:
logger.warning(f"Invalid or malformed URL skipped: {typ} = {url}")

return valid_socials
except Exception as e:
logger.error(f"Error in get_socials for address {addr} on chain {chain}: {e}")
return []

async def analyze_token(update: Update, context: ContextTypes.DEFAULT_TYPE, addr: str):
user_id = update.message.from_user.id
if not can_scan(user_id):
checkout_url = create_checkout_session(user_id)
if checkout_url:
await update.message.reply_text(
f"You've used your 3 free scans. Upgrade to premium for unlimited scans and price/liquidity alerts! Subscribe here (Activates in minutes!)",
parse_mode="Markdown"
)
else:
await update.message.reply_text(
f"You've used your 3 free scans. Upgrade to premium for unlimited scans and price/liquidity alerts! Subscribe here (Error generating dynamic link - check logs for details.)",
parse_mode="Markdown"
)
return

ds = dexscreener_data(addr)
chain = None
if ds:
chain_map = {"ethereum": "eth", "binance": "bsc", "solana": "sol", "base": "base"}
chain = chain_map.get(ds.get("chainId"))
if not chain:
chain = "sol" if is_solana_address(addr) else "eth"

ed = {}
if chain in ["eth", "bsc", "base"]:
ed = etherscan_data(addr, chain)
elif chain == "sol":
ed = solana_data(addr)
else:
await update.message.reply_text("Unsupported chain detected. Try ETH, BSC, Base, or SOL tokens!")
return

gp = goplus_data(addr, chain)
cg = coingecko_description(addr, chain) if chain != "sol" else None

if not any([ed, ds]):
await update.message.reply_text("Hmm, I couldn't find info for this token. Maybe it's new or a rare one? Please try another address! 😊")
return

audit = fetch_audit_info(addr)
admin_ctrls = detect_admin_controls(ed.get("source_code", "") if ed.get("verified") and chain != "sol" else "")

if gp:
if gp["is_mintable"]:
admin_ctrls.append("mintable")
if gp["transfer_pausable"]:
admin_ctrls.append("pausable")
if gp["has_blacklist"]:
admin_ctrls.append("blacklist")
if gp["has_whitelist"]:
admin_ctrls.append("whitelist")
if gp["is_anti_whale"]:
admin_ctrls.append("anti-whale")

name = ed.get("name", "Unknown")
symbol = ed.get("symbol", "N/A")

if name == "Unknown" and ds and "baseToken" in ds:
name = ds["baseToken"].get("name", "Unknown")
symbol = ds["baseToken"].get("symbol", "N/A")
verified = ed.get("verified", False)
if cg:
name = cg.get("name", name)
symbol = cg.get("symbol", symbol)

market_cap = cg.get("market_cap", 0) if cg else 0
if not market_cap and ds:
market_cap = float(ds.get("fdv", "0"))
if not market_cap:
market_cap = float(ds.get("liquidity", "0"))

description = (cg.get("description") if cg else None) or (ed.get("description") if chain == "sol" else None) or meme_context(name, symbol)
if not description:
description = f"This is {name} ({symbol}), a token on the {chain.upper()} blockchain. No detailed description available."

socials = get_socials(addr, chain)
website = next((s['url'] for s in socials if s['type'] == 'website'), None)
if len(description) < 100 and website:
extra_desc = extract_description_from_website(website)
if extra_desc:
description += "\n\nAdditional details from official site: " + extra_desc

has_meaningful_description = (
description and not description.startswith(f"This is {name} ({symbol}), a token on the {chain.upper()} blockchain. No detailed description available.")
)

whale_pct = gp.get("max_holder_percent", 0) if gp else 0
owner_change = gp.get("owner_change_balance", "0") == "1" if gp else False
volume = float(ds.get("volume", "0")) if ds else 0
liquidity = float(ds.get("liquidity", "0")) if ds else 0

launch_date, age_days_exact, age_hours_exact = get_launch_date(addr, chain)
age_days = age_days_exact or ds.get("age_days")
age_hours = age_hours_exact or ds.get("age_hours")
if age_days == 0 and age_hours is not None:
launch_str = f"Launched approximately {age_hours} hours ago"
else:
launch_str = f"Launched: {launch_date} ({age_days} days ago)" if launch_date else f"Launched approximately {age_days} days ago" if age_days is not None else "Launch date unknown"

is_honeypot = gp.get("is_honeypot", False) if gp else False
buy_tax = gp.get("buy_tax", 0) if gp else 0
sell_tax = gp.get("sell_tax", 0) if gp else 0
is_proxy = gp.get("is_proxy", False) if gp else False
liquidity_locked = gp.get("liquidity_locked", False) if gp else False
locked_pct = gp.get("locked_percentage", 0) if gp else 0
ownership_renounced = gp.get("ownership_renounced", False) if gp else (ed.get("mint_authority") is None and ed.get("freeze_authority") is None if chain == "sol" else False)
holder_count = gp.get("holder_count", "Unknown") if gp else "Unknown"
lp_holder_count = gp.get("lp_holder_count", 0) if gp else 0
total_supply = gp.get("total_supply", "0") if gp else ed.get("total_supply", "0")
total_supply_formatted = format_large_number(total_supply)
transfer_pausable = gp.get("transfer_pausable", False) if gp else False
has_blacklist = gp.get("has_blacklist", False) if gp else False
is_anti_whale = gp.get("is_anti_whale", False) if gp else False

is_mintable = (gp.get("is_mintable") == "1") if gp and "is_mintable" in gp else (ed.get("mint_authority") is not None) if chain == "sol" else False

has_holders_data = bool(gp and (chain == "sol" and "top_10_holder_rate" in gp or gp.get("holders"))) or (chain == "sol" and ed.get("holders"))

score = 0
green_flags = []
red_flags = []
negative_flags = []

if not verified:
score += 20
red_flags.append("Contract not verified on the blockchain.")
negative_flags.append("an unverified contract")
else:
green_flags.append("Contract verified on the blockchain.")

if liquidity >= 500:
green_flags.append(f"Liquidity: ${liquidity:,.0f}.")
else:
score += 20
red_flags.append(f"Liquidity low: ${liquidity:,.0f}.")
negative_flags.append("low liquidity")

if age_days is None or age_days < 7:
score += 15
red_flags.append(f"Very new token: {launch_str} (high risk of volatility or rugs).")
negative_flags.append("a very new token")
else:
green_flags.append(launch_str)

if whale_pct <= 30:
green_flags.append("No major wallet dominance.")
else:
score += 15
red_flags.append(f"Warning: one wallet holds {whale_pct:.1f}% of supply.")
negative_flags.append("large whale holdings")

if owner_change:
score += 15
red_flags.append("Owner can change balances.")
negative_flags.append("owner can change balances")
else:
green_flags.append("Owner cannot change balances.")

if market_cap and market_cap >= 1_000_000:
green_flags.append(f"Market cap: ${market_cap:,.0f}.")
elif market_cap:
score += 10
red_flags.append(f"Market cap low: ${market_cap:,.0f}.")
negative_flags.append("low market cap")

if audit.get("audited"):
green_flags.append("Security audit found.")
else:
score += 20
red_flags.append("No official security audit found.")
negative_flags.append("no security audit")

if admin_ctrls:
score += 20
red_flags.append(f"Risky admin functions: {', '.join(admin_ctrls)}.")
negative_flags.append("risky admin functions")
else:
green_flags.append("No risky admin functions found.")

if is_honeypot:
score += 30
red_flags.append("Honeypot detected: may not be sellable.")
negative_flags.append("honeypot risk")
else:
green_flags.append("No honeypot risks found.")

tax_info = ""
if buy_tax == 0 and sell_tax == 0:
green_flags.append(f"Taxes: Buy {buy_tax:.1f}%, Sell {sell_tax:.1f}%.")
elif buy_tax > 5 or sell_tax > 5:
score += 10
red_flags.append(f"High taxes: Buy {buy_tax:.1f}%, Sell {sell_tax:.1f}%.")
negative_flags.append("high taxes")
elif buy_tax > 0 or sell_tax > 0:
tax_info = f"Taxes: Buy {buy_tax:.1f}%, Sell {sell_tax:.1f}%."

if is_proxy:
score += 15
red_flags.append("Proxy contract: upgradable.")
negative_flags.append("a proxy contract")
else:
green_flags.append("Not a proxy contract.")

if liquidity_locked:
green_flags.append(f"Liquidity locked ({locked_pct:.2f}%). LP Holders: {lp_holder_count}")
elif gp:
score += 15
red_flags.append(f"Liquidity not locked ({locked_pct:.2f}%). LP Holders: {lp_holder_count}")
negative_flags.append("unlocked liquidity")

if lp_holder_count < 2 and chain != "sol" and gp:
score += 10
red_flags.append("Concentrated LP holders.")
negative_flags.append("concentrated LP holders")

if ownership_renounced:
green_flags.append("Ownership renounced.")
else:
score += 15
red_flags.append("Ownership not renounced.")
negative_flags.append("ownership not renounced")

if isinstance(holder_count, int) and holder_count > 0:
if holder_count >= 100:
green_flags.append(f"Holder count: {holder_count}.")
else:
score += 10
red_flags.append(f"Low holder count: {holder_count}.")
negative_flags.append("low holder count")

if is_mintable:
score += 10
red_flags.append("Token mintable: supply can increase (unlimited max supply).")
negative_flags.append("mintable supply")
else:
green_flags.append(f"Not mintable: fixed supply of {total_supply_formatted} tokens.")

if transfer_pausable:
score += 10
red_flags.append("Transfers pausable.")
negative_flags.append("pausable transfers")
else:
green_flags.append("Transfers not pausable.")

if has_blacklist:
score += 15
red_flags.append("Has blacklist function.")
negative_flags.append("a blacklist function")
else:
green_flags.append("No blacklist function.")

if is_anti_whale:
green_flags.append("Anti-whale mechanisms in place.")

score = min(score, 100)
label = get_risk_label(score)

current_price = ds.get("priceUsd", "0") if ds else "0"
formatted_price = format_price(current_price)

msg = f"{name} ({symbol}):\n\n"
msg += f"Current Price: {formatted_price}\n"
msg += f"24h Volume: ${volume:,.0f}\n"
if tax_info:
msg += f"{tax_info}\n"
msg += "\nHere's what I found:\n"
msg += "\nGreen Flags: \n" + "\n".join(f"- {g}" for g in green_flags) + "\n" if green_flags else "\nNo green flags to highlight.\n"
msg += "\nRed Flags: \n" + "\n".join(f"- {r}" for r in red_flags) + "\n" if red_flags else "\nNo red flags found.\n"

msg += f"\nRisk Score: {score}/100 - {label}\n"

msg += "\nWhat does this mean?\n\n"
if score <= 40:
msg += "This token presents a low risk profile. It could be a reasonable addition to your portfolio if it aligns with your investment goals, though I recommend starting with a small position and using alerts to monitor for any unexpected changes."
elif score <= 60:
msg += "This token has a medium risk profile - proceed with caution and consider small positions while monitoring via alerts."
else:
msg += "This token shows a higher risk level - it's best to approach with caution and avoid significant investment until these risks are mitigated; consider setting alerts to track price movements or liquidity changes."

explorer_bases = {"eth": "etherscan.io", "bsc": "bscscan.com", "sol": "solscan.io", "base": "basescan.org"}
explorer_names = {"eth": "Etherscan", "bsc": "BscScan", "sol": "Solscan", "base": "Basescan"}
explorer_url = f"https://{explorer_bases.get(chain, 'etherscan.io')}/token/{addr}"
explorer_name = explorer_names.get(chain, "Etherscan")
keyboard = []
if ds and ds.get("chart"):
keyboard.append(InlineKeyboardButton("View Chart", url=ds["chart"]))
if has_meaningful_description:
keyboard.append(InlineKeyboardButton("Token Description", callback_data=f"about_{addr}{chain}"))
if has_holders_data:
keyboard.append(InlineKeyboardButton("Top Holders", callback_data=f"holders{addr}{chain}"))
keyboard.append(InlineKeyboardButton("Set Alerts", callback_data=f"alert{addr}{chain}"))
if socials:
keyboard.append(InlineKeyboardButton("Social Links/Website", callback_data=f"social{addr}_{chain}"))
keyboard.append(InlineKeyboardButton(f"Explore on {explorer_name}", url=explorer_url))
markup = InlineKeyboardMarkup([keyboard[i:i+2] for i in range(0, len(keyboard), 2)]) if keyboard else None

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024

async def send_split_text(text, reply_to=None, limit=TEXT_LIMIT, markup=None):
parts = []
current = text
while len(current) > limit:
split_at = current.rfind('\n', 0, limit)
if split_at == -1:
split_at = limit
parts.append(current[:split_at])
current = current[split_at:]
parts.append(current)

sent_msg = None
for i, part in enumerate(parts):
reply_markup_to_use = markup if i == len(parts) - 1 else None
if i == 0 and reply_to:
sent_msg = await reply_to.reply_text(part, parse_mode="Markdown", reply_markup=reply_markup_to_use)
else:
sent_msg = await update.message.reply_text(part, parse_mode="Markdown", reply_markup=reply_markup_to_use)
return sent_msg

image_url = cg.get("image") if cg else ds.get("image") if ds else ed.get("image") if chain == "sol" else None
if image_url:
if len(msg) <= CAPTION_LIMIT:
try:
await update.message.reply_photo(photo=image_url, caption=msg, parse_mode="Markdown", reply_markup=markup)
return
except Exception as e:
logger.warning(f"Failed to send photo with caption: {e}")

try:
photo_msg = await update.message.reply_photo(photo=image_url)
await send_split_text(msg, reply_to=photo_msg, markup=markup)
except Exception as e:
logger.warning(f"Failed to send photo: {e}")
await send_split_text(msg, markup=markup)
else:
await send_split_text(msg, markup=markup)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
query = update.callback_query
await query.answer()

data = query.data

if data.startswith('about_'):
parts = data.split('_')
if len(parts) != 3:
await query.message.reply_text("Error fetching token description. Try again!")
return
addr = parts[1]
chain = parts[2]
ed = etherscan_data(addr, chain) if chain in ["eth", "bsc", "base"] else solana_data(addr) if chain == "sol" else {}
cg = coingecko_description(addr, chain) if chain != "sol" else None

name = ed.get("name", "Unknown")
symbol = ed.get("symbol", "N/A")
if cg:
name = cg.get("name", name)
symbol = cg.get("symbol", symbol)

description = (cg.get("description") if cg else ed.get("description")) or meme_context(name, symbol) or f"This is {name} ({symbol}), a token on the {chain.upper()} blockchain. No detailed description available."

socials = get_socials(addr, chain)
website = next((s['url'] for s in socials if s['type'] == 'website'), None)
if len(description) < 100 and website:
extra_desc = extract_description_from_website(website)
if extra_desc:
description += "\n\nAdditional details from official site: " + extra_desc

if description.startswith(f"This is {name} ({symbol}), a token on the {chain.upper()} blockchain. No detailed description available."):
await query.message.reply_text("Limited token description available for this new token—set alerts to track updates or check socials!", reply_markup=InlineKeyboardMarkup([[
InlineKeyboardButton("Set Alerts", callback_data=f"alert_{addr}{chain}"),
InlineKeyboardButton("Social Links/Website", callback_data=f"social{addr}_{chain}")
]]))
return

paragraphs = description.split('\n\n')
formatted_desc = ''
char_count = 0
for para in paragraphs:
para = para.strip()
if not para:
continue
sentences = re.split(r'(?<!\w.\w.)(?<![A-Z][a-z].)(?<=.|?|!)\s', para)
para_text = ''
for sentence in sentences:
if char_count + len(sentence) + len(para_text) > 800:
if para_text:
formatted_desc += para_text.rstrip() + '\n\n'
break
para_text += sentence + ' '
else:
formatted_desc += para_text.rstrip() + '\n\n'
char_count += len(para_text)

is_meme = False
if cg and cg.get("tags"):
is_meme = any("meme" in t.lower() for t in cg["tags"])
elif meme_context(name, symbol):
is_meme = True
elif "meme" in formatted_desc.lower():
is_meme = True
token_type = "Meme Coin" if is_meme else "Token"

about_msg = f"Token Description for {name} ({symbol}) - {token_type}:\n\n{formatted_desc.rstrip()}"
await query.message.reply_text(about_msg, parse_mode="HTML")

elif data.startswith('holders_'):
parts = data.split('_')
if len(parts) != 3:
await query.message.reply_text("Error fetching holders info. Try again!")
return
addr = parts[1]
chain = parts[2]
top_holders_str = ""
if chain == "sol":
ed = solana_data(addr)
holders_list = ed.get("holders", [])
if holders_list:
top_holders_str = "Top Holders: \n\n" + "\n".join(f"- #{i+1}: {h['percent']:.2f}% ({h['address'][:6]}...{h['address'][-4:]})" for i, h in enumerate(holders_list))
else:
gp = goplus_data(addr, chain)
if gp and "top_10_holder_rate" in gp:
top_holders_str = f"Top 10 Holders: {gp['top_10_holder_rate'] * 100:.2f}% of supply"
else:
top_holders_str = "No detailed holder data available."
else:
gp = goplus_data(addr, chain)
if gp and gp.get("holders"):
holders_list = sorted(gp["holders"], key=lambda x: float(x.get("percent", 0)), reverse=True)[:5]
top_holders_str = "Top Holders: \n\n" + "\n".join(f"- #{i+1}: {float(h['percent'])*100:.2f}% ({h['address'][:6]}...{h['address'][-4:]})" for i, h in enumerate(holders_list))
else:
top_holders_str = "No detailed holder data available."
holders_msg = f"🏆 {top_holders_str}"
await query.message.reply_text(holders_msg, parse_mode="Markdown")

elif data.startswith('alert_'):
user_id = query.from_user.id
if not is_premium(user_id):
await query.message.reply_text("Alerts are a premium feature - upgrade for unlimited scans + alerts! /premium")
return
parts = data.split('')
if len(parts) != 3:
await query.message.reply_text("Error setting up alert. Try again!")
return
addr = parts[1]
chain = parts[2]
sub_keyboard = [
InlineKeyboardButton("Price Alerts", callback_data=f"price_alerts{addr}{chain}"),
InlineKeyboardButton("Liquidity Alerts", callback_data=f"liq_alerts{addr}_{chain}")
]
sub_markup = InlineKeyboardMarkup([[sub_keyboard[0]], [sub_keyboard[1]]])
await query.message.reply_text("Choose Alert Type:", reply_markup=sub_markup, parse_mode="Markdown")

elif data.startswith('price_alerts_'):
user_id = query.from_user.id
if not is_premium(user_id):
await query.message.reply_text("Alerts are a premium feature - upgrade for unlimited scans + alerts! /premium")
return
parts = data.split('')
if len(parts) != 4:
await query.message.reply_text("Error setting up price alerts. Try again!")
return
addr = parts[2]
chain = parts[3]
sub_keyboard = [
InlineKeyboardButton("Price Increase Alert", callback_data=f"price_inc{addr}{chain}"),
InlineKeyboardButton("Price Decrease Alert", callback_data=f"price_dec{addr}_{chain}")
]
sub_markup = InlineKeyboardMarkup([[sub_keyboard[0], sub_keyboard[1]]])
await query.message.reply_text("Choose Price Alert Type:", reply_markup=sub_markup, parse_mode="Markdown")

elif data.startswith('liq_alerts_'):
user_id = query.from_user.id
if not is_premium(user_id):
await query.message.reply_text("Alerts are a premium feature - upgrade for unlimited scans + alerts! /premium")
return
parts = data.split('')
if len(parts) != 4:
await query.message.reply_text("Error setting up liquidity alerts. Try again!")
return
addr = parts[2]
chain = parts[3]
sub_keyboard = [
InlineKeyboardButton("Liquidity Increase Alert", callback_data=f"liq_inc{addr}{chain}"),
InlineKeyboardButton("Liquidity Decrease Alert", callback_data=f"liq_dec{addr}_{chain}")
]
sub_markup = InlineKeyboardMarkup([[sub_keyboard[0], sub_keyboard[1]]])
await query.message.reply_text("Choose Liquidity Alert Type:", reply_markup=sub_markup, parse_mode="Markdown")

elif data.startswith('price_inc_') or data.startswith('price_dec_'):
user_id = query.from_user.id
if not is_premium(user_id):
await query.message.reply_text("Alerts are a premium feature - upgrade for unlimited scans + alerts! /premium")
return
direction = 'increase' if data.startswith('price_inc_') else 'decrease'
alert_type = 'price'
parts = data.split('_')
addr = parts[2]
chain = parts[3]
ed = etherscan_data(addr, chain) if chain in ["eth", "bsc", "base"] else solana_data(addr) if chain == "sol" else {}
cg = coingecko_description(addr, chain) if chain != "sol" else None
name = ed.get("name", "Unknown")
if cg and cg.get("name"):
name = cg["name"]
await query.message.reply_text(f"Enter % change for {name} Price {direction.capitalize()} alert:")
context.user_data['alert_setup'] = {'addr': addr, 'chain': chain, 'name': name, 'direction': direction, 'alert_type': alert_type}

elif data.startswith('liq_inc_') or data.startswith('liq_dec_'):
user_id = query.from_user.id
if not is_premium(user_id):
await query.message.reply_text("Alerts are a premium feature - upgrade for unlimited scans + alerts! /premium")
return
direction = 'increase' if data.startswith('liq_inc_') else 'decrease'
alert_type = 'liquidity'
parts = data.split('_')
addr = parts[2]
chain = parts[3]
ed = etherscan_data(addr, chain) if chain in ["eth", "bsc", "base"] else solana_data(addr) if chain == "sol" else {}
cg = coingecko_description(addr, chain) if chain != "sol" else None
name = ed.get("name", "Unknown")
if cg and cg.get("name"):
name = cg["name"]
await query.message.reply_text(f"Enter % change for {name} liquidity {direction} alert:")
context.user_data['alert_setup'] = {'addr': addr, 'chain': chain, 'name': name, 'direction': direction, 'alert_type': alert_type}

elif data.startswith('social_'):
parts = data.split('_')
if len(parts) != 3:
await query.message.reply_text("Error fetching social links. Try again!")
return
addr = parts[1]
chain = parts[2]
socials = get_socials(addr, chain)
if not socials:
await query.message.reply_text("No social links or website found for this token.")
return
social_msg = "🌐 Social Links/Website:\n\n"
for s in socials:
typ = s["type"].capitalize()
url = s["url"]
social_msg += f"{typ} - {url}\n"
await query.message.reply_text(social_msg, parse_mode="HTML", disable_web_page_preview=True)

async def handle_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
if 'alert_setup' in context.user_data:
percent_str = update.message.text.strip()
try:
percent = float(percent_str)
user_id = update.message.from_user.id
addr = context.user_data['alert_setup']['addr']
name = context.user_data['alert_setup']['name']
direction = context.user_data['alert_setup']['direction']
alert_type = context.user_data['alert_setup'].get('alert_type', 'price')
value_name = alert_type
if alert_type == 'price':
current = get_current_price(addr)
formatted_current = format_price(current)
else:
current = get_current_liquidity(addr)
formatted_current = f"${Decimal(current):,.0f}"
if current == "0":
await update.message.reply_text(f"Couldn't fetch current {value_name} - try again later.")
del context.user_data['alert_setup']
return
c.execute('''INSERT OR REPLACE INTO alerts (user_id, addr, set_value, percent, direction, name, alert_type) VALUES (?, ?, ?, ?, ?, ?, ?)''',
(user_id, addr, current, percent, direction, name, alert_type))
conn.commit()
alert_subject = "price " if alert_type == 'price' else "liquidity "
await update.message.reply_text(f"{direction.capitalize()} alert set for {name} {alert_subject}at {percent}% from current ({formatted_current})! We'll notify you if it exceeds that.")
del context.user_data['alert_setup']
logger.info(f"Alert set for user {user_id}")
except ValueError:
await update.message.reply_text("Invalid % - try again (e.g., 10).")
return
return

text = update.message.text.strip()
m = re.search(r"(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})", text)
if not m:
await update.message.reply_text("❌ Please send a valid contract address.")
return
await analyze_token(update, context, m.group(0))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(
"Hello and welcome to Token Trust. Please send a CA (Contract Address) to scan any crypto token."
)

async def reset_scans(update: Update, context: ContextTypes.DEFAULT_TYPE):
user_id = update.message.from_user.id
if user_id != DEV_USER_ID:
await update.message.reply_text("This command is for developers only.")
return
c.execute("UPDATE users SET total_scans = 0 WHERE user_id = ?", (user_id,))
conn.commit()
await update.message.reply_text("Your scan count has been reset to 0 for testing.")

async def premium_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
user_id = update.message.from_user.id
if is_premium(user_id):
c.execute("SELECT expiry_date FROM users WHERE user_id = ?", (user_id,))
exp = c.fetchone()[0]
await update.message.reply_text(f"You're premium! Expires: {exp}. Enjoy unlimited scans and alerts.")
else:
await update.message.reply_text("Not premium yet—checking Stripe now...")
is_premium(user_id)
if is_premium(user_id):
await update.message.reply_text("Premium activated! Enjoy unlimited scans and alerts.")
else:
await update.message.reply_text("No active subscription found. Upgrade via the link!")

async def sync_stripe_subs(context: CallbackContext):
try:
subs = stripe.Subscription.list(limit=100, status='all')
for sub in subs.auto_paging_iter():
if 'metadata' in sub and 'user_id' in sub.metadata:
user_id = int(sub.metadata['user_id'])
c.execute("SELECT subscription_id FROM users WHERE user_id = ?", (user_id,))
row = c.fetchone()
if sub.status == 'active':
if not row or row[0] != sub.id:
expiry = datetime.now() + timedelta(days=30)
c.execute("UPDATE users SET is_premium = TRUE, subscription_id = ?, expiry_date = ? WHERE user_id = ?",
(sub.id, expiry.strftime('%Y-%m-%d %H:%M:%S'), user_id))
conn.commit()
logger.info(f"Activated premium via poll for user {user_id} (sub: {sub.id})")
await context.bot.send_message(chat_id=user_id, text="Your premium is now active! Unlimited scans and alerts unlocked. 🚀")
elif sub.status in ['canceled', 'incomplete_expired']:
if row and row[0] == sub.id:
c.execute("UPDATE users SET is_premium = FALSE, subscription_id = NULL, expiry_date = NULL WHERE user_id = ?", (user_id,))
conn.commit()
logger.info(f"Revoked premium via poll for user {user_id} (sub: {sub.id})")
except Exception as e:
logger.error(f"Stripe poll error: {e}")

async def alert_check(context: CallbackContext):
c.execute("SELECT * FROM alerts")
rows = c.fetchall()
alerts = {}
for row in rows:
user_id, addr, set_value, percent, direction, name, alert_type = row
if user_id not in alerts:
alerts[user_id] = {}
alerts[user_id][addr] = {'set_value': set_value, 'percent': percent, 'direction': direction, 'name': name, 'alert_type': alert_type}

need_price = set()
need_liq = set()
for user_alerts in alerts.values():
for addr, al in user_alerts.items():
at = al['alert_type']
if at == 'price':
need_price.add(addr)
else:
need_liq.add(addr)

prices = {}
liquidities = {}
for addr in need_price:
current = get_current_price(addr)
if current != "0":
prices[addr] = current
for addr in need_liq:
current = get_current_liquidity(addr)
if current != "0":
liquidities[addr] = current

to_delete = []
for user_id, user_alerts in alerts.items():
for addr, alert in user_alerts.items():
alert_type = alert['alert_type']
current = prices.get(addr) if alert_type == 'price' else liquidities.get(addr)
if current is None:
continue
set_value = alert['set_value']
percent = alert['percent']
direction = alert['direction']
threshold = Decimal(set_value) * Decimal(1 + percent / 100) if direction == 'increase' else Decimal(set_value) * Decimal(1 - percent / 100)
current_dec = Decimal(current)
if (direction == 'increase' and current_dec >= threshold) or (direction == 'decrease' and current_dec <= threshold):
verb = "increased" if direction == 'increase' else "decreased"
if alert_type == 'price':
formatted = format_price(current)
msg = f"ALERT: {alert['name']} price has {verb} by {percent}%! Current: {formatted}"
else:
formatted = f"${Decimal(current):,.0f}"
msg = f"ALERT: {alert['name']} liquidity has {verb} by {percent}%! Current: {formatted}"
await context.bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")
to_delete.append((user_id, addr))

for user_id, addr in to_delete:
c.execute("DELETE FROM alerts WHERE user_id = ? AND addr = ?", (user_id, addr))
conn.commit()

try:
thread = Thread(target=uvicorn.run, kwargs={'app': api_app, 'host': '0.0.0.0', 'port': 8000})
thread.start()
logger.info("FastAPI webhook started on port 8000.")
except Exception as e:
logger.error(f"Failed to start FastAPI: {e}")

app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("reset", reset_scans))
app.add_handler(CommandHandler("premium_status", premium_status))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_token))
app.add_handler(CallbackQueryHandler(handle_callback))
app.job_queue.run_repeating(alert_check, interval=60, first=0)
app.job_queue.run_repeating(sync_stripe_subs, interval=300, first=10)
logger.info("Bot is running...")
app.run_polling()