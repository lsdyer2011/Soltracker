#!/usr/bin/env python3
"""
bot.py  —  one-sweep Solana tracer for GitHub Actions.

Runs ONCE per invocation (GitHub Actions calls it on a schedule), then exits.
State lives in state.json, which the workflow commits back to the repo so the
next run picks up where this one left off.

Everything secret comes from environment variables (GitHub Secrets):
    BOT_TOKEN      - your Telegram bot key
    CHAT_ID        - your Telegram chat id
    SOURCE_WALLET  - the wallet to watch for outbound transfers
    RPC_URL        - (optional) Solana RPC endpoint; defaults to the public one
"""

import os
import json
import time
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
SOURCE_WALLET = os.environ["SOURCE_WALLET"]
RPC_URL = os.environ.get("RPC_URL") or "https://api.mainnet-beta.solana.com"

STATE_FILE = "state.json"
WSOL_MINT = "So11111111111111111111111111111111111111112"
MIN_OUTBOUND_LAMPORTS = 1_000_000      # ignore sub-0.001 SOL
FRONTIER_TTL = 60 * 30                  # forget a traced wallet after 30 min idle

_session = requests.Session()
_rpc_id = 0

# ----------------------------- RPC + TELEGRAM -------------------------------

def rpc(method, params):
    global _rpc_id
    _rpc_id += 1
    for attempt in range(4):
        r = _session.post(RPC_URL, json={"jsonrpc": "2.0", "id": _rpc_id,
                                         "method": method, "params": params}, timeout=30)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        return data["result"]
    raise RuntimeError("rate-limited")


def tg(text):
    try:
        _session.get(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                     params={"chat_id": CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print("telegram failed:", e)
    print("ALERT:", text)


def new_signatures(wallet, until_sig):
    params = [wallet, {"limit": 1000}]
    if until_sig:
        params[1]["until"] = until_sig
    return rpc("getSignaturesForAddress", params)


def get_tx(sig):
    return rpc("getTransaction",
               [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])

# ----------------------------- PARSING --------------------------------------

def system_transfers(tx):
    msg = tx["transaction"]["message"]
    instrs = list(msg.get("instructions", []))
    for inner in (tx["meta"].get("innerInstructions") or []):
        instrs.extend(inner.get("instructions", []))
    for ix in instrs:
        if ix.get("program") == "system":
            p = ix.get("parsed") or {}
            if p.get("type") in ("transfer", "transferWithSeed"):
                info = p["info"]
                try:
                    yield info["source"], info["destination"], int(info["lamports"])
                except (KeyError, ValueError):
                    pass


def analyze(tx, wallet):
    outbound = []
    for src, dst, lamports in system_transfers(tx):
        if src == wallet and dst != wallet:
            outbound.append((dst, lamports))

    def by_mint(bals):
        d = {}
        for b in bals or []:
            if b.get("owner") == wallet:
                d[b["mint"]] = d.get(b["mint"], 0) + int(b["uiTokenAmount"]["amount"])
        return d

    pre = by_mint(tx["meta"].get("preTokenBalances"))
    post = by_mint(tx["meta"].get("postTokenBalances"))
    gained = [m for m in (set(pre) | set(post))
              if post.get(m, 0) - pre.get(m, 0) > 0 and m != WSOL_MINT]

    keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
    sol_left = False
    if wallet in keys:
        i = keys.index(wallet)
        sol_left = (tx["meta"]["postBalances"][i] - tx["meta"]["preBalances"][i]) < -10_000

    return {"outbound": outbound,
            "is_conversion": bool(gained) and sol_left,
            "gained_mints": gained}

# ----------------------------- STATE ----------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    sigs = new_signatures(SOURCE_WALLET, None)
    return {"source_last_sig": sigs[0]["signature"] if sigs else None,
            "frontier": {}, "visited": [SOURCE_WALLET], "found": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ----------------------------- ONE SWEEP ------------------------------------

def buyer(state, wallet, sig, mints, path):
    tg(f"🎯 BUYER FOUND\n{wallet}\nbought: {', '.join(mints)}\npath: {' -> '.join(path)}")
    state["found"].append({"wallet": wallet, "tx": sig, "mints": mints, "path": path})


def sweep():
    state = load_state()
    visited = set(state["visited"])
    now = time.time()

    # 1. TRIGGER: new outbound from the source wallet
    for s in reversed(new_signatures(SOURCE_WALLET, state["source_last_sig"])):
        tx = get_tx(s["signature"])
        if not tx or tx.get("meta") is None:
            continue
        state["source_last_sig"] = s["signature"]
        info = analyze(tx, SOURCE_WALLET)
        if info["is_conversion"]:
            buyer(state, SOURCE_WALLET, s["signature"], info["gained_mints"], [SOURCE_WALLET])
            continue
        for dst, lamports in info["outbound"]:
            if lamports < MIN_OUTBOUND_LAMPORTS or dst in visited:
                continue
            visited.add(dst)
            state["frontier"][dst] = {"last_sig": s["signature"],
                                      "path": [SOURCE_WALLET, dst], "first_seen": now}
            tg(f"🚀 Outbound detected\n{lamports/1e9:.3f} SOL -> {dst}\ntracing...")

    # 2. TRACE: advance each frontier wallet one step
    for wallet in list(state["frontier"].keys()):
        entry = state["frontier"][wallet]
        converted = False
        for s in reversed(new_signatures(wallet, entry["last_sig"])):
            tx = get_tx(s["signature"])
            if not tx or tx.get("meta") is None:
                continue
            entry["last_sig"] = s["signature"]
            info = analyze(tx, wallet)
            if info["is_conversion"]:
                buyer(state, wallet, s["signature"], info["gained_mints"], entry["path"])
                converted = True
                break
            for dst, lamports in info["outbound"]:
                if lamports < MIN_OUTBOUND_LAMPORTS or dst in visited:
                    continue
                visited.add(dst)
                state["frontier"][dst] = {"last_sig": s["signature"],
                                          "path": entry["path"] + [dst], "first_seen": now}
        if converted or (now - entry["first_seen"] > FRONTIER_TTL):
            state["frontier"].pop(wallet, None)

    state["visited"] = sorted(visited)
    save_state(state)
    print(f"sweep done. frontier={len(state['frontier'])} found={len(state['found'])}")


if __name__ == "__main__":
    sweep()
