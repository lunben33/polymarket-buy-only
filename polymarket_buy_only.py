"""
Polymarket BTC 15分钟市场 - 轻量只买入脚本 (v3)
- 固定买入 shares（默认 2）
- 触发条件：市场价 ≥ 0.8
- 买入价 = Ask + offset（无上限）
- 每个 token 只买一次
- 自动 claim 已结算奖励
- 依赖：web3, py_clob_client, requests, python-dotenv
"""

import os
import re
import time
import json
import requests
from datetime import datetime, timezone, timedelta
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, ApiCreds
from dotenv import load_dotenv
import logging

load_dotenv()

# ==================== 配置 ====================
PRIVATE_KEY = os.getenv("POLYMARKET_PK")
if not PRIVATE_KEY:
    raise SystemExit("请在 .env 中配置 POLYMARKET_PK")

FIXED_SHARES = 2.0          # 每次买入的 shares 数量
TARGET_PRICE = 0.80         # 触发买入的最低市场价
BUY_OFFSET = 0.01            # 在 Ask 上加多少
CHECK_INTERVAL = 0.5        # 扫描间隔（秒）
AUTO_CLAIM = True
CLAIM_INTERVAL = 300        # claim 检查间隔（秒）

# ==================== 合约 & API ====================
RPC = "https://polygon-rpc.com"
CHAIN_ID = 137
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
MARKETS_API = "https://gamma-api.polymarket.com/markets"
CLOB_HOST = "https://clob.polymarket.com"

w3 = Web3(Web3.HTTPProvider(RPC))
ctf = w3.eth.contract(address=CTF, abi=[
    {"name": "balanceOf", "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"name": "redeemPositions", "inputs": [{"type": "bytes32"}, {"type": "uint256[]"}], "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"name": "payoutDenominator", "inputs": [{"type": "bytes32"}], "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"name": "payoutNumerators", "inputs": [{"type": "bytes32"}, {"type": "uint256"}], "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"}
])
usdc = w3.eth.contract(address=USDC, abi=[
    {"name": "balanceOf", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"}
])
adapter = w3.eth.contract(address=NEG_RISK_ADAPTER, abi=[
    {"name": "redeemPositions", "inputs": [{"type": "bytes32"}, {"type": "uint256[]"}], "outputs": [], "stateMutability": "nonpayable", "type": "function"}
])

# ==================== 日志 ====================
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger()

# ==================== 全局状态 ====================
eoa = w3.eth.account.from_key(PRIVATE_KEY).address
proxy_wallet = None
client = None
bought = set()           # 已买入的 token_id
positions = {}           # token_id -> 持仓信息
total_profit = 0.0

# ==================== 初始化 ====================
def init():
    global proxy_wallet, client
    # 获取 proxy wallet（从 data-api）
    r = requests.get("https://data-api.polymarket.com/activity", params={"user": eoa, "limit": 1})
    if r.ok and r.json():
        proxy_wallet = Web3.to_checksum_address(r.json()[0]["proxyWallet"])
    else:
        raise SystemExit("无法获取 proxy wallet，请先在 Polymarket 有交易记录")

    # 初始化 CLOB 客户端
    key = os.getenv("POLYMARKET_API_KEY")
    secret = os.getenv("POLYMARKET_API_SECRET")
    phrase = os.getenv("POLYMARKET_API_PASSPHRASE")
    if not all([key, secret, phrase]):
        temp = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
        creds = temp.create_or_derive_api_creds()
        print("\n请添加到 .env：")
        print(f"POLYMARKET_API_KEY={creds.api_key}")
        print(f"POLYMARKET_API_SECRET={creds.api_secret}")
        print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}\n")
        raise SystemExit("API 凭证已生成，请配置后重启")
    client = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                        creds=ApiCreds(key, secret, phrase), funder=proxy_wallet, signature_type=2)

    log.info(f"EOA: {eoa}")
    log.info(f"Proxy: {proxy_wallet}")
    set_allowances()

def set_allowances():
    spenders = ["0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E", NEG_RISK_ADAPTER]
    for s in spenders:
        if usdc.functions.allowance(proxy_wallet, s).call() < 1e18:
            tx = usdc.functions.approve(s, 2**256-1).build_transaction({
                'from': eoa, 'nonce': w3.eth.get_transaction_count(eoa), 'gasPrice': w3.eth.gas_price
            })
            signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            w3.eth.send_raw_transaction(signed.raw_transaction)
            w3.eth.wait_for_transaction_receipt(signed.hash, timeout=120)
            log.info(f"USDC 已授权 {s[:8]}...")

# ==================== 市场获取 ====================
def get_15m_btc_markets():
    now = datetime.now(timezone.utc)
    params = {
        "active": True, "closed": False, "limit": 200,
        "end_date_min": now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        "end_date_max": (now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    }
    r = requests.get(MARKETS_API, params=params, timeout=10)
    if not r.ok: return []
    markets = []
    for m in r.json():
        q = m["question"].lower()
        if "bitcoin" not in q and "btc" not in q: continue
        if not re.search(r"\d{1,2}:\d{2}[AP]M\s*-\s*\d{1,2}:\d{2}[AP]M", m["question"]): continue
        markets.append(m)
    return markets

# ==================== 价格 & 买入 ====================
def get_price(token_id):
    try:
        book = client.get_order_book(token_id)
        ask = float(book.asks[-1].price) if book.asks else None
        bid = float(book.bids[-1].price) if book.bids else None
        price = (ask + bid)/2 if ask and bid else (ask or bid)
        return price, ask, bid
    except:
        return None, None, None

def buy(token_id, outcome, ask, market):
    if token_id in bought: return
    price = ask + BUY_OFFSET
    price = round(price, 2)
    cost = price * FIXED_SHARES
    if usdc.functions.balanceOf(proxy_wallet).call() / 1e6 < cost:
        log.warning("USDC 余额不足")
        return

    try:
        order = client.create_order(OrderArgs(token_id, price, FIXED_SHARES, "BUY"))
        resp = client.post_order(order)
        bought.add(token_id)
        positions[token_id] = {
            "outcome": outcome, "price": price, "shares": FIXED_SHARES,
            "condition_id": market["conditionId"], "market_id": market["id"]
        }
        log.info(f"买入 {outcome} @ ${price} × {FIXED_SHARES} = ${cost:.2f}")
    except Exception as e:
        log.error(f"买入失败: {e}")

# ==================== Claim 奖励 ====================
def claim_rewards():
    global total_profit
    for token_id in list(positions):
        pos = positions[token_id]
        price, _, _ = get_price(token_id)
        if price != "SETTLED": continue

        condition = Web3.to_bytes(hexstr=pos["condition_id"])
        denom = ctf.functions.payoutDenominator(condition).call()
        if denom == 0: continue

        payout0 = ctf.functions.payoutNumerators(condition, 0).call()
        payout1 = ctf.functions.payoutNumerators(condition, 1).call()
        outcome_idx = 0 if "up" in pos["outcome"].lower() or "yes" in pos["outcome"].lower() else 1
        payout = payout0 if outcome_idx == 0 else payout1
        if payout == 0:
            cost = pos["price"] * pos["shares"]
            total_profit -= cost
            log.info(f"输了: -${cost:.2f}")
            del positions[token_id]
            continue

        balance = ctf.functions.balanceOf(proxy_wallet, int(token_id)).call()
        if balance == 0: 
            del positions[token_id]
            continue

        amounts = [0, 0]
        amounts[outcome_idx] = balance
        tx = adapter.functions.redeemPositions(condition, amounts).build_transaction({
            'from': eoa, 'nonce': w3.eth.get_transaction_count(eoa), 'gasPrice': w3.eth.gas_price
        })
        signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        hash_ = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(hash_, timeout=120)

        shares = balance / 1e6
        revenue = (payout / denom) * shares
        cost = pos["price"] * pos["shares"]
        profit = revenue - cost
        total_profit += profit
        log.info(f"Claim 成功! +${profit:+.2f} (成本 ${cost:.2f})")
        del positions[token_id]

# ==================== 主循环 ====================
def main():
    init()
    last_claim = time.time()
    log.info("启动 BTC 15M 只买入机器人")

    while True:
        try:
            markets = get_15m_btc_markets()
            for m in markets:
                tids = json.loads(m.get("clobTokenIds", "[]"))
                outs = json.loads(m.get("outcomes", "[]"))
                for tid, out in zip(tids, outs):
                    if tid in bought: continue
                    price, ask, _ = get_price(tid)
                    if not price or price == "SETTLED" or price < TARGET_PRICE: continue
                    buy(tid, out, ask, m)

            if AUTO_CLAIM and time.time() - last_claim > CLAIM_INTERVAL:
                claim_rewards()
                last_claim = time.time()

            time.sleep(CHECK_INTERVAL + 0.1 * (hash(eoa) % 3))  # 防限流
        except Exception as e:
            log.error(f"循环错误: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
