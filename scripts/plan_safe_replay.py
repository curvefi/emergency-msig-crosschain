from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

from web3 import HTTPProvider, Web3
from web3.middleware import ExtraDataToPOAMiddleware

from download_data import load_chain_configs, load_deployment_state, load_safe_networks

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ALIAS = {"hyperliquid": "HyperEVM", "mp1": "Corn", "plumephoenix": "Plume Mainnet"}
FIELDS = ("chainSlug", "shortName", "name", "chain", "title")
ZERO = "0x0000000000000000000000000000000000000000"
MULTICALL3_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "allowFailure", "type": "bool"},
                    {"name": "value", "type": "uint256"},
                    {"name": "callData", "type": "bytes"},
                ],
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "aggregate3Value",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    }
]
FACTORY_ABI = [
    {
        "inputs": [
            {"name": "_singleton", "type": "address"},
            {"name": "initializer", "type": "bytes"},
            {"name": "saltNonce", "type": "uint256"},
        ],
        "name": "createProxyWithNonce",
        "outputs": [{"name": "proxy", "type": "address"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "proxyCreationCode",
        "outputs": [{"name": "", "type": "bytes"}],
        "stateMutability": "pure",
        "type": "function",
    },
]
SAFE_ABI = [
    {
        "inputs": [
            {"name": "_owners", "type": "address[]"},
            {"name": "_threshold", "type": "uint256"},
            {"name": "to", "type": "address"},
            {"name": "data", "type": "bytes"},
            {"name": "fallbackHandler", "type": "address"},
            {"name": "paymentToken", "type": "address"},
            {"name": "payment", "type": "uint256"},
            {"name": "paymentReceiver", "type": "address"},
        ],
        "name": "setup",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "VERSION",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getOwners",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getThreshold",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]
norm = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())  # noqa


def read_json(path):
    return json.loads(path.read_text())


def make_w3(url, timeout=6):
    w3 = Web3(HTTPProvider(url, request_kwargs={"timeout": timeout}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def pick_rpcs(item):
    urls = []
    for rpc in item.get("rpc", []):
        url = rpc.get("url") if isinstance(rpc, dict) else rpc
        if isinstance(url, str) and url.startswith(("http://", "https://")) and "{" not in url:
            urls.append(url)
    return list(dict.fromkeys(urls))[:3]


def config_rpcs(config):
    urls = []
    for kind, env in (("ankr", "ANKR_API_KEY"), ("drpc", "DRPC_API_KEY"), ("public", None)):
        url = config.get(kind)
        if not url:
            continue
        if "{}" in url:
            key = os.getenv(env) if env else None
            if not key:
                continue
            url = url.format(key)
        urls.append(url)
    return list(dict.fromkeys(urls))


def connect(chain, config, safe_item, expected_chain_id):
    errors = []
    urls = list(dict.fromkeys(config_rpcs(config) + pick_rpcs(safe_item)))[:6]
    for url in urls:
        w3 = make_w3(url)
        try:
            chain_id = w3.eth.chain_id
            if chain_id != expected_chain_id:
                errors.append(f"{url}: chain id {chain_id} != {expected_chain_id}")
                continue
            return w3, url, chain_id
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("; ".join(errors[:3]) or "no usable RPC")


def index_networks(safe_networks):
    return {
        norm(str(value)): item
        for item in safe_networks
        if not item.get("testnet")
        for field in FIELDS
        if (value := item.get(field))
    }


def get_item(chain, index, safe_networks=None, chain_id=None):
    if safe_networks and chain_id is not None:
        for item in safe_networks:
            try:
                if int(item.get("chainId")) == int(chain_id):
                    return item
            except (TypeError, ValueError):
                pass
    return index.get(norm(ALIAS.get(chain, chain)))


def code_hash(w3, address):
    code = bytes(w3.eth.get_code(address))
    return code, (Web3.keccak(code).hex() if code else None)


def checksum(value):
    return Web3.to_checksum_address(value)


def deployment_addresses(deployment):
    found = {}
    if deployment.get("address"):
        found["default"] = checksum(deployment["address"])
    for label, address in deployment.get("addresses", []):
        if address:
            found[label] = checksum(address)
    return found


def deployment_address(deployment, preferred=("EIP-155 contracts", "Canonical contracts", "default")):
    found = deployment_addresses(deployment)
    for wanted in preferred:
        if found.get(wanted):
            return found[wanted]
    for address in found.values():
        return address
    return None


def decode_setup(initializer):
    _, args = Web3().eth.contract(abi=SAFE_ABI).decode_function_input(initializer)
    return {
        "owners": [checksum(owner) for owner in args["_owners"]],
        "threshold": args["_threshold"],
        "to": checksum(args["to"]),
        "data": Web3.to_hex(args["data"]),
        "fallback_handler": checksum(args["fallbackHandler"]),
        "payment_token": checksum(args["paymentToken"]),
        "payment": args["payment"],
        "payment_receiver": checksum(args["paymentReceiver"]),
    }


def safe_state(w3, address):
    safe = w3.eth.contract(address=checksum(address), abi=SAFE_ABI)
    return {
        "address": checksum(address),
        "version": safe.functions.VERSION().call(),
        "owners": [checksum(owner) for owner in safe.functions.getOwners().call()],
        "threshold": safe.functions.getThreshold().call(),
    }


def verify_source_tx(manifest):
    key = os.getenv("ETHERSCAN_API_KEY") or os.getenv("etherscan_api_key")
    if not key:
        return {"status": "skipped", "reason": "missing Etherscan API key"}
    params = urllib.parse.urlencode(
        {
            "chainid": manifest["source"]["chain_id"],
            "module": "proxy",
            "action": "eth_getTransactionByHash",
            "txhash": manifest["source"]["tx_hash"],
            "apikey": key,
        }
    )
    payload = json.loads(
        urllib.request.urlopen(f"https://api.etherscan.io/v2/api?{params}", timeout=20).read()
    )
    tx = payload["result"]
    _, outer = Web3().eth.contract(abi=MULTICALL3_ABI).decode_function_input(tx["input"])
    factory_call = next(
        call for call in outer["calls"] if checksum(call["target"]) == manifest["factory"]
    )
    _, inner = Web3().eth.contract(abi=FACTORY_ABI).decode_function_input(
        Web3.to_hex(factory_call["callData"])
    )
    if checksum(inner["_singleton"]) != manifest["singleton"]:
        raise ValueError("singleton mismatch in source tx")
    if Web3.to_hex(inner["initializer"]) != manifest["initializer"]:
        raise ValueError("initializer mismatch in source tx")
    if hex(inner["saltNonce"]) != manifest["salt_nonce"]:
        raise ValueError("salt nonce mismatch in source tx")
    expected_setup = manifest["setup"] | {"fallback_handler": manifest["fallback_handler"]}
    if decode_setup(manifest["initializer"]) != expected_setup:
        raise ValueError("initializer decode mismatch")
    return {
        "status": "verified",
        "from": checksum(tx["from"]),
        "outer_target": checksum(tx["to"]),
        "calls": len(outer["calls"]),
    }


def source_state(manifest, configs, safe_networks):
    safe_index = index_networks(safe_networks)
    source_item = get_item(
        manifest["source"]["chain"], safe_index, safe_networks, manifest["source"]["chain_id"]
    )
    w3, rpc_url, chain_id = connect(
        manifest["source"]["chain"], configs, source_item, manifest["source"]["chain_id"]
    )
    factory = w3.eth.contract(address=manifest["factory"], abi=FACTORY_ABI)
    creation_code = factory.functions.proxyCreationCode().call()
    state = safe_state(w3, manifest["target_safe"])
    version = state["version"]
    if not manifest["source"]["version"].startswith(version):
        raise ValueError("source Safe version mismatch")
    owners = state["owners"]
    threshold = state["threshold"]
    if owners != manifest["setup"]["owners"] or threshold != manifest["setup"]["threshold"]:
        raise ValueError("source Safe owner state mismatch")
    factory_code, factory_hash = code_hash(w3, manifest["factory"])
    singleton_code, singleton_hash = code_hash(w3, manifest["singleton"])
    handler_code, handler_hash = code_hash(w3, manifest["fallback_handler"])
    safe_code, safe_hash = code_hash(w3, manifest["target_safe"])
    if not all((factory_code, singleton_code, handler_code, safe_code)):
        raise ValueError("missing required source code")
    return {
        "rpc_url": rpc_url,
        "factory_hash": factory_hash,
        "singleton_hash": singleton_hash,
        "fallback_handler_hash": handler_hash,
        "safe_hash": safe_hash,
        "proxy_creation_code": creation_code,
    }


def proxy_address(manifest, proxy_creation_code):
    init = proxy_creation_code + int(manifest["singleton"], 16).to_bytes(32, "big")
    salt = Web3.solidity_keccak(
        ["bytes32", "uint256"],
        [Web3.keccak(hexstr=manifest["initializer"]), int(manifest["salt_nonce"], 16)],
    )
    raw = Web3.keccak(
        b"\xff"
        + bytes.fromhex(manifest["factory"][2:])
        + salt
        + Web3.keccak(init)
    )[-20:]
    return checksum(f"0x{raw.hex()}")


def assess(chain, config, item, manifest, source):
    declared = int(config.get("chain_id", item["chainId"]))
    try:
        w3, rpc_url, actual = connect(chain, config, item, declared)
    except Exception as exc:
        return {"chain": chain, "chain_id": declared, "feasible": False, "reason": str(exc)}
    result = {
        "chain": chain,
        "chain_id": actual,
        "rpc_url": rpc_url,
        "factory": manifest["factory"],
        "singleton": manifest["singleton"],
        "fallback_handler": manifest["fallback_handler"],
        "salt_nonce": manifest["salt_nonce"],
        "initializer": manifest["initializer"],
        "target_safe": manifest["target_safe"],
        "value": "0x0",
    }
    target_code, target_hash = code_hash(w3, manifest["target_safe"])
    if target_code:
        reason = "target address already occupied"
        if target_hash == source["safe_hash"]:
            reason = "target address already has Safe proxy runtime code"
        return result | {"feasible": False, "reason": reason}
    for label in ("factory", "singleton", "fallback_handler"):
        _, found = code_hash(w3, manifest[label])
        expected = source[f"{label}_hash"]
        if not found:
            return result | {"feasible": False, "reason": f"missing {label} at {manifest[label]}"}
        if found != expected:
            return result | {"feasible": False, "reason": f"{label} code hash mismatch"}
    expected_address = proxy_address(manifest, source["proxy_creation_code"])
    calldata = Web3().eth.contract(abi=FACTORY_ABI).encode_abi(
        "createProxyWithNonce",
        args=[
            manifest["singleton"],
            bytes.fromhex(manifest["initializer"][2:]),
            int(manifest["salt_nonce"], 16),
        ],
    )
    return result | {
        "feasible": expected_address == manifest["target_safe"],
        "reason": "ready" if expected_address == manifest["target_safe"] else "predicted address mismatch",
        "calldata": calldata,
        "expected_address": expected_address,
    }


def build_report():
    manifest = read_json(DATA / "safe-replay.json")
    configs = load_chain_configs().get("mainnets", {})
    safe_networks = load_safe_networks()
    targets = sorted(load_deployment_state()["deployments"]["mainnets"])
    verification = verify_source_tx(manifest)
    source = source_state(manifest, configs, safe_networks)
    results = []
    index = index_networks(safe_networks)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {}
        for chain in targets:
            declared = int(configs.get(chain, {}).get("chain_id", 0)) or None
            item = get_item(chain, index, safe_networks, declared)
            if not item:
                results.append(
                    {"chain": chain, "feasible": False, "reason": "missing Safe network metadata"}
                )
                continue
            futures[pool.submit(assess, chain, configs.get(chain, {}), item, manifest, source)] = chain
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda result: result["chain"])
    feasible = [result["chain"] for result in results if result.get("feasible")]
    blocked = [f"{result['chain']}: {result['reason']}" for result in results if not result.get("feasible")]
    return {
        "source_tx_verification": verification,
        "source": {
            "chain": manifest["source"]["chain"],
            "chain_id": manifest["source"]["chain_id"],
            "tx_hash": manifest["source"]["tx_hash"],
            "target_safe": manifest["target_safe"],
            "rpc_url": source["rpc_url"],
        },
        "summary": {"total": len(results), "feasible": len(feasible), "blocked": len(blocked)},
        "results": results,
    }


def main():
    report = build_report()
    results = report["results"]
    feasible = [result["chain"] for result in results if result.get("feasible")]
    blocked = [f"{result['chain']}: {result['reason']}" for result in results if not result.get("feasible")]
    verification = report["source_tx_verification"]
    print(f"Source tx verification: {verification['status']}")
    if verification.get("reason"):
        print(f"Reason: {verification['reason']}")
    print(f"Feasible ({len(feasible)}/{len(results)}):")
    print("\n".join(feasible) or "-")
    print()
    print(f"Blocked ({len(blocked)}):")
    print("\n".join(blocked) or "-")
    print()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
