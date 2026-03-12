from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from download_data import load_safe_networks
from web3 import HTTPProvider, Web3
from web3.middleware import ExtraDataToPOAMiddleware

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CURVE = DATA / "curve-multisig.json"
SAFE_REPLAY = DATA / "safe-replay.json"
CHAIN_CONFIGS = ROOT.parent / "blockhash-oracle" / "scripts" / "chain-parse" / "chains.json"
ALIAS = {"hyperliquid": "HyperEVM", "mp1": "Corn", "plumephoenix": "Plume Mainnet"}
FIELDS = ("chainSlug", "shortName", "name", "chain", "title")
MASK = "***"
SAFE_ABI = [
    {"inputs": [], "name": "getOwners", "outputs": [{"type": "address[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getThreshold", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]


def read_json(path):
    return json.loads(path.read_text())


def checksum(value):
    return Web3.to_checksum_address(value)


def norm(value):
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def make_w3(url):
    w3 = Web3(HTTPProvider(url, request_kwargs={"timeout": 6}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def mask_rpc(url):
    split = urlsplit(url)
    path = "/".join(MASK if i and len(part) > 12 and part.replace("-", "").replace("_", "").isalnum() else part for i, part in enumerate(split.path.split("/")))
    query = urlencode([(k, MASK if "key" in k.lower() or "token" in k.lower() or len(v) > 12 else v) for k, v in parse_qsl(split.query, keep_blank_values=True)])
    return urlunsplit((split.scheme, split.netloc, path, query, split.fragment))


def config_rpcs(config):
    urls = []
    for field, env in (("ankr", "ANKR_API_KEY"), ("drpc", "DRPC_API_KEY"), ("public", None)):
        url = config.get(field)
        if not url:
            continue
        if "{}" in url:
            key = os.getenv(env) if env else None
            if not key:
                continue
            url = url.format(key)
        urls.append(url)
    return list(dict.fromkeys(urls))


def pick_rpcs(item):
    urls = []
    for rpc in item.get("rpc", []):
        url = rpc.get("url") if isinstance(rpc, dict) else rpc
        if isinstance(url, str) and url.startswith(("http://", "https://")) and "{" not in url:
            urls.append(url)
    return list(dict.fromkeys(urls))[:3]


def index_networks(items):
    return {
        norm(str(value)): item
        for item in items
        if not item.get("testnet")
        for field in FIELDS
        if (value := item.get(field))
    }


def get_item(chain, chain_id, networks, index):
    for item in networks:
        try:
            if int(item.get("chainId")) == int(chain_id):
                return item
        except (TypeError, ValueError):
            pass
    return index.get(norm(ALIAS.get(chain, chain)))


def connect(chain, chain_id, config, item):
    errors = []
    for url in list(dict.fromkeys(config_rpcs(config) + pick_rpcs(item)))[:6]:
        try:
            w3 = make_w3(url)
            if w3.eth.chain_id == chain_id:
                return w3, mask_rpc(url)
            errors.append(f"{mask_rpc(url)}: wrong chain id")
        except Exception as exc:
            errors.append(f"{mask_rpc(url)}: {exc}")
    raise RuntimeError("; ".join(errors[:3]) or "no usable RPC")


def assess(entry, expected_owners, expected_threshold, configs, networks, index):
    chain, chain_id, address = entry["chain"], entry["chain_id"], checksum(entry["address"])
    item = get_item(chain, chain_id, networks, index)
    if not item:
        return {"chain": chain, "chain_id": chain_id, "deployed": False, "valid": False, "reason": "missing Safe network metadata"}
    try:
        w3, rpc_url = connect(chain, chain_id, configs.get(chain, {}), item)
    except Exception as exc:
        return {"chain": chain, "chain_id": chain_id, "deployed": False, "valid": False, "reason": str(exc)}
    result = {"chain": chain, "chain_id": chain_id, "safe": address, "rpc_url": rpc_url}
    if not w3.eth.get_code(address):
        return result | {"deployed": False, "valid": False, "reason": "safe not deployed"}
    safe = w3.eth.contract(address=address, abi=SAFE_ABI)
    try:
        owners = [checksum(owner) for owner in safe.functions.getOwners().call()]
        threshold = safe.functions.getThreshold().call()
    except Exception as exc:
        return result | {"deployed": True, "valid": False, "reason": f"failed to read Safe state: {exc}"}
    owner_match = set(owners) == set(expected_owners) and len(owners) == len(expected_owners)
    threshold_match = threshold == expected_threshold
    reason = ", ".join(part for ok, part in ((owner_match, "owner set mismatch"), (threshold_match, "threshold mismatch")) if not ok) or "ok"
    return result | {
        "deployed": True,
        "valid": owner_match and threshold_match,
        "reason": reason,
        "owners": owners,
        "threshold": threshold,
    }


def assess_baseline(baseline, expected_owners, expected_threshold, configs, networks, index):
    result = {
        "name": baseline["name"],
        "chain": baseline["chain"],
        "chain_id": baseline["chain_id"],
        "address": checksum(baseline["address"]),
        "docs_url": baseline["docs_url"],
    }
    item = get_item(baseline["chain"], baseline["chain_id"], networks, index)
    if not item:
        return result | {"valid": False, "reason": "missing Safe network metadata for baseline Safe"}
    try:
        w3, rpc_url = connect(baseline["chain"], baseline["chain_id"], configs.get(baseline["chain"], {}), item)
    except Exception as exc:
        return result | {"valid": False, "reason": str(exc)}
    safe = w3.eth.contract(address=checksum(baseline["address"]), abi=SAFE_ABI)
    try:
        owners = [checksum(owner) for owner in safe.functions.getOwners().call()]
        threshold = safe.functions.getThreshold().call()
    except Exception as exc:
        return result | {"valid": False, "reason": f"failed to read baseline Safe state: {exc}"}
    owner_match = set(owners) == set(expected_owners) and len(owners) == len(expected_owners)
    threshold_match = threshold == expected_threshold
    reason = ", ".join(part for ok, part in ((owner_match, "owner set mismatch"), (threshold_match, "threshold mismatch")) if not ok) or "ok"
    return result | {
        "rpc_url": rpc_url,
        "valid": owner_match and threshold_match,
        "reason": reason,
        "owners": owners,
        "threshold": threshold,
    }


def main():
    manifest = read_json(CURVE)
    source = read_json(SAFE_REPLAY)
    expected_owners = [checksum(owner) for owner in manifest["owners"]]
    expected_threshold = manifest["threshold"]
    configs = read_json(CHAIN_CONFIGS).get("mainnets", {}) if CHAIN_CONFIGS.exists() else {}
    networks = load_safe_networks()
    index = index_networks(networks)
    baseline = assess_baseline(source["baseline"], expected_owners, expected_threshold, configs, networks, index)
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(assess, entry, expected_owners, expected_threshold, configs, networks, index): entry["chain"]
            for entry in manifest["chains"]
        }
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["chain"])
    valid = [item["chain"] for item in results if item["valid"]]
    missing = [item["chain"] for item in results if item["reason"] == "safe not deployed"]
    issues = [f"{item['chain']}: {item['reason']}" for item in results if item["reason"] not in {"ok", "safe not deployed"}]
    if not baseline["valid"]:
        issues.insert(0, f"baseline: {baseline['reason']}")
    print(f"Baseline ({baseline['name']}):")
    print(f"{baseline['address']} on {baseline['chain']} ({baseline['reason']})")
    print()
    print(f"Valid ({len(valid)}/{len(results)}):")
    print("\n".join(valid) or "-")
    print()
    print(f"Missing ({len(missing)}):")
    print("\n".join(missing) or "-")
    print()
    print(f"Issues ({len(issues)}):")
    print("\n".join(issues) or "-")
    print()
    print(json.dumps({"safe": manifest["safe_address"], "expected_owners": expected_owners, "expected_threshold": expected_threshold, "baseline": baseline, "summary": {"total": len(results), "valid": len(valid), "missing": len(missing), "issues": len(issues), "baseline_valid": baseline["valid"]}, "results": results}, indent=2))


if __name__ == "__main__":
    main()
