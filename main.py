"""
Dual-loop monitor for Omen/Reality.eth oracle manipulation.

Loop 1 — Market Monitor (default 30m):
    Polls all unfinalized Pearl + QS markets for new answers from unknown addresses.

Loop 2 — Suspect Monitor (default 5m):
    Polls ALL on-chain activity from the known attacker address.

Both loops send Telegram alerts. Suspect activity is summarized by Grok AI.

Usage:
    uv run python main.py
    uv run python main.py --market-interval 1800 --suspect-interval 300
"""

import json
import logging
import os
import threading
import time
from collections import defaultdict

import requests as http_requests
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("monitor")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GNOSIS_RPC = os.getenv("GNOSIS_RPC", "https://rpc.gnosischain.com")
SUBGRAPH_API_KEY = os.getenv("SUBGRAPH_API_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_IDS = json.loads(os.getenv("TELEGRAM_CHAT_IDS", "[]"))
GROK_PROXY = os.getenv("GROK_PROXY", "")

MARKET_INTERVAL = int(os.getenv("MARKET_INTERVAL", "1800"))  # 30 min
SUSPECT_INTERVAL = int(os.getenv("SUSPECT_INTERVAL", "300"))  # 5 min

SUSPECT_ADDRESS = os.getenv(
    "SUSPECT_ADDRESS", "0xc5fD24b2974743896e1E94c47E99D3960C7d4c96"
).lower()

# Addresses we trust (won't trigger alerts)
DAVID = "0xEB2A22b27C7Ad5eeE424Fd90b376c745E60f914E".lower()
PEARL_CREATOR = "0xFfc8029154ECD55ABED15BD428bA596E7D23f557".lower()
QS_CREATOR = "0x89c5cc945dd550BcFfb72Fe42BfF002429F46Fec".lower()
WHITELISTED = {DAVID, PEARL_CREATOR, QS_CREATOR}

REALITY_ETH = "0x79e32aE03fb27B07C89c0c568F80287C01ca2E57"
WXDAI = "0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d"
OMEN_SUBGRAPH_ID = "9fUVQpFwzpdWS9bq5WkAnmKbNNcoBwatMR4yZq81pbbz"
BLOCKSCOUT = "https://gnosis.blockscout.com/api/v2"

SEP = "\u241f"
ANSWER_LABELS = {
    "0" * 64: "Yes",
    "0" * 63 + "1": "No",
    "f" * 64: "Invalid",
}

# Add custom whitelisted addresses from env
for addr in json.loads(os.getenv("WHITELISTED_ADDRESSES", "[]")):
    WHITELISTED.add(addr.lower())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post_retry(url, **kwargs):
    for attempt in range(3):
        try:
            r = http_requests.post(url, **kwargs)
            r.raise_for_status()
            return r
        except (http_requests.ConnectionError, http_requests.Timeout):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def _get_retry(url, **kwargs):
    for attempt in range(3):
        try:
            r = http_requests.get(url, **kwargs)
            r.raise_for_status()
            return r
        except (http_requests.ConnectionError, http_requests.Timeout):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


CACHE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cache"
)
CACHE_FILE = os.path.join(CACHE_DIR, "state.json")


def _load_cache():
    """Load persisted state from disk."""
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(data):
    """Persist state to disk."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.error("Failed to save cache: %s", e)


def decode_answer(answer_hex):
    if answer_hex is None:
        return "No answer"
    normalized = answer_hex.lower().replace("0x", "").zfill(64)
    return ANSWER_LABELS.get(normalized, f"0x{normalized[:8]}...")


def reality_url(question_id):
    return f"https://reality.eth.limo/app/#!/question/{REALITY_ETH}-{question_id}"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def send_telegram(message, parse_mode="Markdown"):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_IDS:
        log.warning("Telegram not configured — printing instead")
        print(message)
        return

    for chat_id in TELEGRAM_CHAT_IDS:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            r = http_requests.post(url, data=payload, timeout=10)
            if r.status_code != 200:
                log.error("Telegram send failed (%s): %s", chat_id, r.text)
        except Exception as e:
            log.error("Telegram send error (%s): %s", chat_id, e)


# ---------------------------------------------------------------------------
# Grok AI summary
# ---------------------------------------------------------------------------


def summarize_with_grok(activity_text):
    """Use Grok AI to summarize on-chain activity."""
    try:
        from core import Grok

        prompt = (
            "Known oracle manipulator on Omen/Gnosis Chain. They bet on markets "
            "then submit wrong Reality.eth answers to profit. We already know this.\n\n"
            "Below are their NEW transactions. For each tx, give ONE short line: "
            "what they did and why it matters (e.g. which market, bet size, "
            "resolution submitted). Skip boilerplate — no disclaimers, no "
            "recommendations, no explaining what Reality.eth is. Max 3 sentences "
            "total.\n\n"
            f"{activity_text}"
        )
        result = Grok("grok-3-fast", proxy=GROK_PROXY or None).start_convo(prompt)
        if result and result.get("response"):
            return result["response"]
        if result and result.get("error"):
            log.warning("Grok error: %s", str(result["error"])[:200])
            return None
    except ImportError:
        log.warning("Grok-Api not installed — run: uv add grok-api")
    except Exception as e:
        log.warning("Grok summary failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Subgraph: unfinalized markets
# ---------------------------------------------------------------------------

CREATORS = {PEARL_CREATOR: "Pearl", QS_CREATOR: "QS"}


def fetch_unfinalized_markets():
    """Fetch unfinalized/contestable markets from Pearl + QS creators."""
    if not SUBGRAPH_API_KEY:
        log.error("SUBGRAPH_API_KEY not set")
        return []

    url = f"https://gateway.thegraph.com/api/{SUBGRAPH_API_KEY}/subgraphs/id/{OMEN_SUBGRAPH_ID}"
    now = int(time.time())
    all_markets = []
    seen = set()

    for creator, label in CREATORS.items():
        base = f'creator: "{creator}", openingTimestamp_lt: {now}'
        for where in [
            f"{base}, answerFinalizedTimestamp: null",
            f"{base}, answerFinalizedTimestamp_gt: {now}",
        ]:
            skip = 0
            while True:
                query = f"""
                {{
                  fixedProductMarketMakers(
                    where: {{ {where} }}
                    first: 1000
                    skip: {skip}
                    orderBy: openingTimestamp
                    orderDirection: asc
                  ) {{
                    id
                    question {{
                      id title outcomes currentAnswer currentAnswerBond
                    }}
                    openingTimestamp currentAnswer currentAnswerBond timeout
                  }}
                }}
                """
                r = _post_retry(
                    url,
                    json={"query": query},
                    headers={"Content-Type": "application/json"},
                    timeout=90,
                )
                data = r.json()
                if "errors" in data:
                    log.error("Subgraph error: %s", data["errors"])
                    break
                batch = data.get("data", {}).get("fixedProductMarketMakers", [])
                for m in batch:
                    if m["id"] not in seen:
                        seen.add(m["id"])
                        m["_creator"] = label
                        all_markets.append(m)
                if len(batch) < 1000:
                    break
                skip += 1000

    return all_markets


# ---------------------------------------------------------------------------
# Betting cross-reference
# ---------------------------------------------------------------------------

_w3 = None


def _get_w3():
    global _w3
    if _w3 is None:
        _w3 = Web3(Web3.HTTPProvider(GNOSIS_RPC))
    return _w3


# wxDAI Transfer(address,address,uint256) topic
_TRANSFER_SIG = None


def _get_transfer_sig():
    global _TRANSFER_SIG
    if _TRANSFER_SIG is None:
        raw = _get_w3().keccak(
            text="Transfer(address,address,uint256)"
        ).hex()
        _TRANSFER_SIG = "0x" + raw if not raw.startswith("0x") else raw
    return _TRANSFER_SIG


def check_betting_on_market(answerer, fpmm_address):
    """Check if answerer has wxDAI transfers to/from an FPMM contract.

    Returns (bet_amount, returned_amount) in xDAI, or (0, 0) if none.
    """
    w3 = _get_w3()
    transfer_sig = _get_transfer_sig()

    addr_topic = "0x" + answerer.replace("0x", "").lower().zfill(64)
    fpmm_topic = "0x" + fpmm_address.replace("0x", "").lower().zfill(64)
    wxdai = Web3.to_checksum_address(WXDAI)

    # Look back ~30 days (~518400 blocks at 5s/block)
    latest = w3.eth.block_number
    from_block = max(latest - 518400, 0)

    try:
        # wxDAI from answerer -> FPMM (bets placed)
        logs_out = w3.eth.get_logs({
            "fromBlock": from_block,
            "toBlock": latest,
            "address": wxdai,
            "topics": [transfer_sig, addr_topic, fpmm_topic],
        })
        bet_total = sum(
            int(lg["data"].hex(), 16) / 1e18 for lg in logs_out
        )

        # wxDAI from FPMM -> answerer (returns/sells)
        logs_in = w3.eth.get_logs({
            "fromBlock": from_block,
            "toBlock": latest,
            "address": wxdai,
            "topics": [transfer_sig, fpmm_topic, addr_topic],
        })
        returned_total = sum(
            int(lg["data"].hex(), 16) / 1e18 for lg in logs_in
        )

        return bet_total, returned_total
    except Exception as e:
        log.warning("Betting check failed for %s: %s", answerer[:14], e)
        return 0.0, 0.0


def check_all_betting_activity(answerer):
    """Get total wxDAI betting activity for an address across all FPMMs.

    Returns dict with bet_count, total_bet, unique_markets.
    """
    w3 = _get_w3()
    transfer_sig = _get_transfer_sig()

    addr_topic = "0x" + answerer.replace("0x", "").lower().zfill(64)
    wxdai = Web3.to_checksum_address(WXDAI)

    latest = w3.eth.block_number
    from_block = max(latest - 518400, 0)  # ~30 days

    try:
        logs_out = w3.eth.get_logs({
            "fromBlock": from_block,
            "toBlock": latest,
            "address": wxdai,
            "topics": [transfer_sig, addr_topic],
        })

        by_dest = defaultdict(float)
        for lg in logs_out:
            dst = "0x" + lg["topics"][2].hex()[-40:]
            by_dest[dst] += int(lg["data"].hex(), 16) / 1e18

        total = sum(by_dest.values())
        return {
            "bet_count": len(logs_out),
            "total_bet": total,
            "unique_markets": len(by_dest),
            "by_market": by_dest,
        }
    except Exception as e:
        log.warning("Betting activity check failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Loop 1: Market monitor
# ---------------------------------------------------------------------------

# Load persisted state
_cache = _load_cache()
_last_seen_answers = _cache.get("last_seen_answers", {})


def market_monitor_tick():
    """Check all unfinalized markets for new answers from unknown addresses."""
    log.info("Market monitor: fetching unfinalized markets...")
    markets = fetch_unfinalized_markets()
    log.info("Market monitor: %d unfinalized markets", len(markets))

    if not markets:
        return

    # Collect answered questions
    question_ids = []
    market_by_qid = {}
    for m in markets:
        q = m.get("question") or {}
        qid = q.get("id", "")
        if not qid:
            continue
        bond = q.get("currentAnswerBond")
        if bond and int(bond) > 0:
            question_ids.append(qid)
            market_by_qid[qid] = m

    if not question_ids:
        log.info("Market monitor: no answered markets to check")
        return

    # Fetch current answerers from Reality.io subgraph
    realitio_subgraph = "E7ymrCnNcQdAAgLbdFWzGE5mvr5Mb5T9VfT43FqA7bNh"
    url = f"https://gateway.thegraph.com/api/{SUBGRAPH_API_KEY}/subgraphs/id/{realitio_subgraph}"
    answerer_map = {}

    for i in range(0, len(question_ids), 1000):
        batch = question_ids[i : i + 1000]
        ids_str = ", ".join(f'"{qid}"' for qid in batch)
        query = f"""
        {{
          questions(where: {{questionId_in: [{ids_str}]}}, first: 1000) {{
            questionId
            responses(orderBy: timestamp, orderDirection: asc) {{
              user answer bond timestamp
            }}
          }}
        }}
        """
        r = _post_retry(
            url,
            json={"query": query},
            headers={"Content-Type": "application/json"},
            timeout=90,
        )
        data = r.json()
        if "errors" in data:
            log.error("Reality subgraph error: %s", data["errors"])
            continue
        for q in data.get("data", {}).get("questions", []):
            responses = q.get("responses", [])
            if responses:
                latest = responses[-1]
                answerer_map[q["questionId"]] = {
                    "user": latest["user"].lower(),
                    "answer": latest["answer"],
                    "bond": latest["bond"],
                    "history": responses,
                }

    # Check for unknown answerers
    alerts = []
    for qid, resp in answerer_map.items():
        user = resp["user"]
        if user in WHITELISTED:
            continue

        m = market_by_qid.get(qid)
        if not m:
            continue

        q = m.get("question") or {}
        title = q.get("title", "").split(SEP)[0].strip()
        answer = decode_answer(resp["answer"])
        bond_xdai = int(resp["bond"]) / 1e18
        counter_bond = bond_xdai * 2
        creator = m.get("_creator", "?")

        # De-duplicate: skip if already alerted for this exact state
        state_key = (resp["answer"], resp["bond"])
        if _last_seen_answers.get(qid) == state_key:
            continue
        _last_seen_answers[qid] = state_key

        is_suspect = user == SUSPECT_ADDRESS
        severity = "SUSPECT" if is_suspect else "UNKNOWN"
        emoji = "\U0001f6a8" if is_suspect else "\u26a0\ufe0f"

        # Cross-reference: did the answerer bet on this market?
        fpmm_addr = m.get("id", "")
        bet_line = ""
        if fpmm_addr:
            bet_amt, ret_amt = check_betting_on_market(
                user, fpmm_addr
            )
            if bet_amt > 0:
                emoji = "\U0001f6a8"  # escalate
                severity = "BET+RESOLVE"
                bet_line = (
                    f"\n\U0001f4b0 *Also bet on this market!*"
                    f"\n  Invested: {bet_amt:.4f} xDAI"
                    f"  Returned: {ret_amt:.4f} xDAI"
                )

        # Build answer history
        history = resp.get("history", [])
        history_lines = ""
        if history:
            lines = []
            for h in history:
                h_user = h["user"].lower()
                h_ans = decode_answer(h["answer"])
                h_bond = int(h["bond"]) / 1e18
                if h_user == SUSPECT_ADDRESS:
                    who = "SUSPECT"
                elif h_user == DAVID:
                    who = "David"
                elif h_user in WHITELISTED:
                    who = "whitelisted"
                else:
                    who = h_user[:10] + "..."
                lines.append(
                    f"  {h_ans} ({h_bond:.4f}) by {who}"
                )
            history_lines = (
                "\n\n*Answer history:*\n"
                + "\n".join(lines)
            )

        alert = (
            f"{emoji} *{severity} answer on {creator} market*\n\n"
            f"*Q:* {title}\n"
            f"*Current answer:* {answer}\n"
            f"*Bond:* {bond_xdai:.4f} xDAI\n"
            f"*Counter-bond:* {counter_bond:.4f} xDAI\n"
            f"*Answerer:* `{user}`\n"
            f"[Reality.eth]({reality_url(qid)})"
            f"{bet_line}"
            f"{history_lines}"
        )
        alerts.append(alert)

    if alerts:
        log.warning("Market monitor: %d alerts", len(alerts))
        _save_cache({
            "last_seen_answers": _last_seen_answers,
            "last_suspect_tx_hash": _last_suspect_tx_hash,
        })
        for alert in alerts:
            send_telegram(alert)
    else:
        log.info("Market monitor: no new suspicious answers")


# ---------------------------------------------------------------------------
# Loop 2: Suspect monitor
# ---------------------------------------------------------------------------

_last_suspect_tx_hash = _cache.get("last_suspect_tx_hash")


def fetch_suspect_txs(address, limit=25):
    """Fetch recent transactions from Blockscout for an address."""
    url = f"{BLOCKSCOUT}/addresses/{address}/transactions"
    try:
        data = _get_retry(
            url, headers={"Accept": "application/json"}, timeout=15
        ).json()
        return data.get("items", [])[:limit]
    except Exception as e:
        log.error("Blockscout fetch failed: %s", e)
        return []


def format_tx_summary(txs):
    """Format transaction list into a readable summary for Grok."""
    lines = []
    for tx in txs:
        ts = tx.get("timestamp", "?")
        method = tx.get("method") or "transfer"
        to_info = tx.get("to") or {}
        to_addr = to_info.get("hash", "?")
        to_name = to_info.get("name")
        value_wei = int(tx.get("value", "0"))
        value_xdai = value_wei / 1e18
        status = tx.get("status", "?")
        tx_hash = tx.get("hash", "?")

        to_display = to_name or to_addr[:14] + "..."
        decoded = tx.get("decoded_input")
        params_str = ""
        if decoded and decoded.get("parameters"):
            params = {p["name"]: p["value"] for p in decoded["parameters"]}
            params_str = f" params={json.dumps(params, default=str)[:200]}"

        lines.append(
            f"  {ts} | {method}({status}) -> {to_display} | "
            f"{value_xdai:.4f} xDAI | {tx_hash[:18]}...{params_str}"
        )
    return "\n".join(lines)


def _find_fpmm_for_question(question_id):
    """Look up the FPMM contract address for a Reality.eth question ID."""
    if not SUBGRAPH_API_KEY:
        return None
    url = (
        f"https://gateway.thegraph.com/api/{SUBGRAPH_API_KEY}"
        f"/subgraphs/id/{OMEN_SUBGRAPH_ID}"
    )
    query = f"""
    {{
      fixedProductMarketMakers(
        where: {{question_: {{id: "{question_id}"}}}}
        first: 1
      ) {{ id }}
    }}
    """
    try:
        r = _post_retry(
            url,
            json={"query": query},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        data = r.json()
        markets = data.get("data", {}).get(
            "fixedProductMarketMakers", []
        )
        if markets:
            return markets[0]["id"]
    except Exception as e:
        log.warning("FPMM lookup failed: %s", e)
    return None


def _enrich_with_betting(new_txs):
    """For resolution txs, check if suspect also bet on those markets."""
    lines = []
    for tx in new_txs:
        method = tx.get("method") or ""
        if method not in ("submitAnswer", "resolve"):
            continue

        decoded = tx.get("decoded_input")
        if not decoded or not decoded.get("parameters"):
            continue

        params = {
            p["name"]: p["value"]
            for p in decoded["parameters"]
        }
        qid = params.get("question_id") or params.get("questionId")
        if not qid:
            continue

        # Find the FPMM for this question
        fpmm = _find_fpmm_for_question(qid)
        if not fpmm:
            continue

        bet_amt, ret_amt = check_betting_on_market(
            SUSPECT_ADDRESS, fpmm
        )
        if bet_amt > 0:
            lines.append(
                f"  Question {qid[:18]}...: "
                f"bet {bet_amt:.4f} xDAI, "
                f"returned {ret_amt:.4f} xDAI"
            )
        else:
            lines.append(
                f"  Question {qid[:18]}...: no bets found"
            )

    return "\n".join(lines)


def suspect_monitor_tick():
    """Check for any new activity from the suspect address."""
    global _last_suspect_tx_hash

    log.info("Suspect monitor: checking %s...", SUSPECT_ADDRESS[:14])
    txs = fetch_suspect_txs(SUSPECT_ADDRESS)

    if not txs:
        log.info("Suspect monitor: no transactions found")
        return

    latest_hash = txs[0].get("hash")

    if _last_suspect_tx_hash is None:
        # First run — record latest, don't alert on old txs
        _last_suspect_tx_hash = latest_hash
        _save_cache({
            "last_seen_answers": _last_seen_answers,
            "last_suspect_tx_hash": _last_suspect_tx_hash,
        })
        log.info(
            "Suspect monitor: initialized, latest tx=%s",
            latest_hash[:18] if latest_hash else "?",
        )
        return

    if latest_hash == _last_suspect_tx_hash:
        log.info("Suspect monitor: no new activity")
        return

    # Collect new txs since last seen
    new_txs = []
    for tx in txs:
        if tx.get("hash") == _last_suspect_tx_hash:
            break
        new_txs.append(tx)

    _last_suspect_tx_hash = latest_hash
    _save_cache({
        "last_seen_answers": _last_seen_answers,
        "last_suspect_tx_hash": _last_suspect_tx_hash,
    })

    if not new_txs:
        return

    log.warning("Suspect monitor: %d new transactions!", len(new_txs))

    # Format raw activity
    raw_summary = format_tx_summary(new_txs)

    # Enrich with betting data for any resolution txs
    betting_context = _enrich_with_betting(new_txs)

    # Try AI summary
    grok_input = (
        f"Address: {SUSPECT_ADDRESS}\n"
        f"New transactions ({len(new_txs)}):\n{raw_summary}"
    )
    if betting_context:
        grok_input += f"\n\nBetting activity on resolved markets:\n{betting_context}"

    ai_summary = summarize_with_grok(grok_input)

    # Build Telegram message
    header = (
        f"\U0001f50d *Suspect Activity* \u2014 {len(new_txs)} new tx(s)\n"
        f"`{SUSPECT_ADDRESS}`\n\n"
    )

    body = ""
    if ai_summary:
        body = f"*AI Summary:*\n{ai_summary}\n\n"

    # Compact raw tx list
    compact_lines = []
    for tx in new_txs[:10]:
        method = tx.get("method") or "transfer"
        to_info = tx.get("to") or {}
        to_name = to_info.get("name") or (to_info.get("hash", "")[:10] + "...")
        value_xdai = int(tx.get("value", "0")) / 1e18
        tx_hash = tx.get("hash", "")
        ts = tx.get("timestamp", "")[:19].replace("T", " ")
        line = f"\u2022 `{method}` \u2192 {to_name}"
        if value_xdai > 0:
            line += f" ({value_xdai:.3f} xDAI)"
        line += f" [{ts}](https://gnosisscan.io/tx/{tx_hash})"
        compact_lines.append(line)

    body += "\n".join(compact_lines)
    if len(new_txs) > 10:
        body += f"\n_...and {len(new_txs) - 10} more_"

    message = header + body
    # Telegram 4096 char limit
    if len(message) > 4000:
        message = message[:3997] + "..."

    send_telegram(message)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_loop(name, tick_fn, interval):
    """Run a monitoring function on a fixed interval."""
    while True:
        try:
            tick_fn()
        except Exception as e:
            log.exception("%s error: %s", name, e)
            try:
                send_telegram(f"\u2699\ufe0f *{name} error*\n`{str(e)[:500]}`")
            except Exception:
                pass
        time.sleep(interval)


def main():
    market_interval = MARKET_INTERVAL
    suspect_interval = SUSPECT_INTERVAL

    log.info("Starting attacker report monitor")
    log.info("  Market interval: %ds (%dm)", market_interval, market_interval // 60)
    log.info("  Suspect interval: %ds (%dm)", suspect_interval, suspect_interval // 60)
    log.info("  Suspect address: %s", SUSPECT_ADDRESS)
    log.info("  Whitelisted: %s", WHITELISTED)
    log.info("  Telegram configured: %s", bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_IDS))

    send_telegram(
        "\U0001f7e2 *Attacker Report Monitor started*\n"
        f"Market check: every {market_interval // 60}m\n"
        f"Suspect check: every {suspect_interval // 60}m\n"
        f"Suspect: `{SUSPECT_ADDRESS}`"
    )

    # Run suspect monitor in background thread
    suspect_thread = threading.Thread(
        target=run_loop,
        args=("Suspect monitor", suspect_monitor_tick, suspect_interval),
        daemon=True,
    )
    suspect_thread.start()

    # Run market monitor in main thread
    run_loop("Market monitor", market_monitor_tick, market_interval)


if __name__ == "__main__":
    main()
