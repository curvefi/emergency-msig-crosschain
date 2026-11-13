import json
import os
import sys
import time
from getpass import getpass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from eth_account import Account
from web3 import HTTPProvider, Web3

import plan_safe_replay as replay
from secure_key_utils import get_web3_account

BROADCAST_ENV = "BROADCAST"
BROADCAST_ENABLED = {"1", "true", "yes"}
MASK = "***"
SENTINEL = "0x0000000000000000000000000000000000000001"
GUARD_SLOT = 0x4A204F620C8C5CCDCA3FD54D003BADD85BA500436A431F0CBDA4F558C93C34C8
FALLBACK_SLOT = 0x6C9A6C4A39284E37ED1CF53D337577D14212A4870FB976A4366C693B939918D5
STATE_ABI = [
    {
        "inputs": [{"name": "start", "type": "address"}, {"name": "pageSize", "type": "uint256"}],
        "name": "getModulesPaginated",
        "outputs": [{"name": "array", "type": "address[]"}, {"name": "next", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "nonce",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def load_account():
    encrypted = os.getenv("ENCRYPTED_PK")
    if encrypted:
        password = os.getenv("WEB3_PASSWORD") or getpass("Key password: ")
        return get_web3_account(encrypted, password)
    private_key = os.getenv("PRIVATE_KEY") or os.getenv("WEB3_MAINNET_PK")
    if private_key:
        return Account.from_key(private_key)
    raise RuntimeError("set ENCRYPTED_PK (preferred) or PRIVATE_KEY/WEB3_MAINNET_PK")


def wanted(result):
    only = {chain.strip() for chain in os.getenv("ONLY_CHAINS", "").split(",") if chain.strip()}
    return result.get("feasible") and (not only or result["chain"] in only)


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
        return {
            "maxPriorityFeePerGas": priority,
            "maxFeePerGas": base * 2 + priority,
        }
    return {"gasPrice": w3.eth.gas_price}


def slot_address(w3, address, slot):
    return replay.checksum("0x" + bytes(w3.eth.get_storage_at(address, slot))[-20:].hex())


def verify_deployed(w3, result, setup):
    safe = result["target_safe"]
    state = replay.safe_state(w3, safe)
    extra = w3.eth.contract(address=safe, abi=STATE_ABI)
    checks = {
        "owners": state["owners"] == setup["owners"],
        "threshold": state["threshold"] == setup["threshold"],
        "singleton": slot_address(w3, safe, 0) == result["singleton"],
        "fallback_handler": slot_address(w3, safe, FALLBACK_SLOT) == result["fallback_handler"],
        "guard": slot_address(w3, safe, GUARD_SLOT) == replay.ZERO,
        "modules": extra.functions.getModulesPaginated(SENTINEL, 10).call()[0] == [],
        "nonce": extra.functions.nonce().call() == 0,
    }
    return {"verified": all(checks.values()), "checks": checks}


def send(result, account, setup):
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
    outcome = {
        "chain": result["chain"],
        "chain_id": result["chain_id"],
        "rpc_url": result["rpc_url"],
        "tx_hash": w3.to_hex(tx_hash),
        "status": receipt.status,
        "gas_used": receipt.gasUsed,
        "safe": result["target_safe"],
    }
    if receipt.status != 1:
        print(f"FAILED on {result['chain']}: transaction reverted")
        return outcome | {"verified": False}
    for attempt in range(3):
        try:
            outcome |= verify_deployed(w3, result, setup)
            outcome.pop("error", None)
            break
        except Exception as exc:  # a load-balanced RPC node can lag behind the receipt
            outcome |= {"verified": False, "error": f"state read failed: {exc}"}
            time.sleep(3)
    if not outcome["verified"]:
        print(f"WARNING on {result['chain']}: deployed Safe not verified: {outcome.get('checks') or outcome.get('error')}")
    return outcome


def main():
    report = replay.build_report()
    targets = [result for result in report["results"] if wanted(result)]
    print(f"Feasible targets ({len(targets)}):")
    print("\n".join(result["chain"] for result in targets) or "-")
    print()
    print(json.dumps({"summary": report["summary"], "targets": [display_result(x) for x in targets]}, indent=2))
    if os.getenv(BROADCAST_ENV, "").lower() not in BROADCAST_ENABLED:
        print(f"\nDry run only. Set {BROADCAST_ENV}=1 to send transactions.")
        return
    setup = replay.read_json(replay.DATA / "safe-replay.json")["setup"]
    account = load_account()
    print(f"\nDeployer: {account.address}")
    receipts = []
    for result in targets:
        shown = display_result(result)
        try:
            balance = replay.make_w3(result["rpc_url"]).eth.get_balance(account.address)
            balance = f"{Web3.from_wei(balance, 'ether')} native"
        except Exception as exc:
            balance = f"unknown ({exc})"
        print(
            f"\nChain: {shown['chain']}\n"
            f"RPC: {shown['rpc_url']}\n"
            f"Factory: {shown['factory']}\n"
            f"Safe: {shown['target_safe']}\n"
            f"Deployer balance: {balance}"
        )
        typed = input("Deploy? [y/N] ").strip().lower()
        if typed not in {"y", "yes"}:
            print(f"Skipping {result['chain']}.")
            continue
        print(f"Broadcasting on {result['chain']} via {shown['rpc_url']}")
        try:
            outcome = send(result, account, setup)
        except Exception as exc:
            print(f"FAILED on {result['chain']}: {exc}")
            outcome = {"chain": result["chain"], "chain_id": result["chain_id"], "verified": False, "error": str(exc)}
        receipts.append(display_result(outcome))
    print()
    print(json.dumps({"deployer": account.address, "receipts": receipts}, indent=2))
    failed = [receipt["chain"] for receipt in receipts if not receipt.get("verified")]
    if any(receipt.get("verified") for receipt in receipts):
        print(
            "\nNew Safes start with the March 2026 owners. Rotate them with the two swapOwner calls"
            " (nonce 0) before giving them any role."
        )
    if failed:
        print(f"\nNot deployed or not verified: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
