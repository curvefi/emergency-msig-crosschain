import json
import os
from getpass import getpass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from eth_account import Account

import plan_safe_fallback as fallback
import plan_safe_replay as replay
from secure_key_utils import get_web3_account

BROADCAST_ENV = "BROADCAST"
BROADCAST_ENABLED = {"1", "true", "yes"}
MASK = "***"


def load_account():
    encrypted = os.getenv("ENCRYPTED_PK")
    if encrypted:
        password = os.getenv("WEB3_PASSWORD") or getpass("Key password: ")
        return get_web3_account(encrypted, password)
    private_key = os.getenv("PRIVATE_KEY") or os.getenv("WEB3_MAINNET_PK")
    if private_key:
        return Account.from_key(private_key)
    raise RuntimeError("set ENCRYPTED_PK (preferred) or PRIVATE_KEY/WEB3_MAINNET_PK")


def mask_rpc_url(url):
    split = urlsplit(url)
    path = "/".join(
        MASK if i and len(part) > 12 and part.replace("-", "").replace("_", "").isalnum() else part
        for i, part in enumerate(split.path.split("/"))
    )
    query = urlencode(
        [
            (
                key,
                MASK
                if "key" in key.lower() or "token" in key.lower() or len(value) > 12
                else value,
            )
            for key, value in parse_qsl(split.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((split.scheme, split.netloc, path, query, split.fragment))


def display_result(result):
    shown = dict(result)
    if "rpc_url" in shown:
        shown["rpc_url"] = mask_rpc_url(shown["rpc_url"])
    return shown


def fee_fields(w3):
    latest = w3.eth.get_block("latest")
    if latest.get("baseFeePerGas") is not None:
        try:
            priority = w3.eth.max_priority_fee
        except Exception:
            priority = w3.to_wei(1, "gwei")
        base = latest["baseFeePerGas"]
        return {"maxPriorityFeePerGas": priority, "maxFeePerGas": base * 2 + priority}
    return {"gasPrice": w3.eth.gas_price}


def send(result, account):
    w3 = replay.make_w3(result["rpc_url"], timeout=20)
    tx = {
        "from": account.address,
        "to": result["factory"],
        "data": result["calldata"],
        "value": 0,
        "chainId": result["chain_id"],
        "nonce": w3.eth.get_transaction_count(account.address),
        **fee_fields(w3),
    }
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"Tx: {w3.to_hex(tx_hash)}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    return {
        "chain": result["chain"],
        "chain_id": result["chain_id"],
        "rpc_url": result["rpc_url"],
        "tx_hash": w3.to_hex(tx_hash),
        "status": receipt.status,
        "gas_used": receipt.gasUsed,
        "safe": result["target_safe"],
    }


def main():
    report = fallback.build_report()
    targets = [result for result in report["results"] if result.get("feasible")]
    print(f"Feasible targets ({len(targets)}):")
    print("\n".join(result["chain"] for result in targets) or "-")
    print()
    print(json.dumps({"summary": report["summary"], "targets": [display_result(x) for x in targets]}, indent=2))
    if os.getenv(BROADCAST_ENV, "").lower() not in BROADCAST_ENABLED:
        print(f"\nDry run only. Set {BROADCAST_ENV}=1 to send transactions.")
        return
    account = load_account()
    print(f"\nDeployer: {account.address}")
    receipts = []
    for result in targets:
        shown = display_result(result)
        print(
            f"\nChain: {shown['chain']}\n"
            f"RPC: {shown['rpc_url']}\n"
            f"Factory: {shown['factory']}\n"
            f"Singleton: {shown['singleton']}\n"
            f"Safe: {shown['target_safe']}"
        )
        typed = input("Deploy? [y/N] ").strip().lower()
        if typed not in {"y", "yes"}:
            print(f"Skipping {result['chain']}.")
            continue
        print(f"Broadcasting on {result['chain']} via {shown['rpc_url']}")
        receipts.append(display_result(send(result, account)))
    print()
    print(json.dumps({"deployer": account.address, "receipts": receipts}, indent=2))


if __name__ == "__main__":
    main()
