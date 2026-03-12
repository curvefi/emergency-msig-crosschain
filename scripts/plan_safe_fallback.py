from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from download_data import load_deployment_state, load_safe_networks
import plan_safe_replay as replay

VERSION = "v1.3.0"
FACTORY_NAMES = ("proxy_factory", "safe_proxy_factory")
SINGLETON_NAMES = ("gnosis_safe_l2", "safe_l2", "gnosis_safe", "safe")
MASK = "***"


def wanted(chain):
    only = {name.strip() for name in os.getenv("ONLY_CHAINS", "").split(",") if name.strip()}
    return not only or chain in only


def missing_chains():
    deployed = {entry["chain"] for entry in replay.read_json(replay.DATA / "curve-multisig.json")["chains"]}
    mainnets = sorted(load_deployment_state()["deployments"]["mainnets"])
    return [chain for chain in mainnets if chain not in deployed and wanted(chain)]


def v130(item):
    return {
        deployment["name"]: replay.deployment_address(deployment, ("Canonical contracts", "default"))
        for deployment in item.get("smartAccounts", [])
        if deployment.get("version") == VERSION
        and replay.deployment_address(deployment, ("Canonical contracts", "default"))
    }


def pick(mapping, names):
    for name in names:
        if mapping.get(name):
            return mapping[name]
    return None


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


def display(item):
    shown = dict(item)
    if "rpc_url" in shown:
        shown["rpc_url"] = mask_rpc_url(shown["rpc_url"])
    return shown


def assess(chain, config, item, manifest):
    chain_id = int(config.get("chain_id", item["chainId"]))
    try:
        w3, rpc_url, actual = replay.connect(chain, config, item, chain_id)
    except Exception as exc:
        return {"chain": chain, "chain_id": chain_id, "feasible": False, "reason": str(exc)}
    deployments = v130(item)
    factory = pick(deployments, FACTORY_NAMES)
    singleton = pick(deployments, SINGLETON_NAMES)
    if not factory:
        return {"chain": chain, "chain_id": actual, "feasible": False, "reason": "missing Safe v1.3.0 factory"}
    if not singleton:
        return {"chain": chain, "chain_id": actual, "feasible": False, "reason": "missing Safe v1.3.0 singleton"}
    result = {
        "chain": chain,
        "chain_id": actual,
        "rpc_url": rpc_url,
        "factory": factory,
        "singleton": singleton,
        "fallback_handler": manifest["fallback_handler"],
        "salt_nonce": manifest["salt_nonce"],
        "initializer": manifest["initializer"],
        "value": "0x0",
        "version": VERSION,
    }
    for label, address in (
        ("factory", factory),
        ("singleton", singleton),
        ("fallback_handler", manifest["fallback_handler"]),
    ):
        code, _ = replay.code_hash(w3, address)
        if not code:
            return result | {"feasible": False, "reason": f"missing {label} at {address}"}
    proxy_code = w3.eth.contract(address=factory, abi=replay.FACTORY_ABI).functions.proxyCreationCode().call()
    derived = manifest | {"factory": factory, "singleton": singleton}
    safe = replay.proxy_address(derived, proxy_code)
    code, _ = replay.code_hash(w3, safe)
    reason = "ready"
    if code:
        reason = "target address already occupied"
        try:
            contract = w3.eth.contract(address=safe, abi=replay.SAFE_ABI)
            owners = [replay.checksum(owner) for owner in contract.functions.getOwners().call()]
            threshold = contract.functions.getThreshold().call()
            if set(owners) == set(manifest["setup"]["owners"]) and len(owners) == len(manifest["setup"]["owners"]) and threshold == manifest["setup"]["threshold"]:
                reason = "target address already has matching Safe"
        except Exception:
            pass
    calldata = replay.Web3().eth.contract(abi=replay.FACTORY_ABI).encode_abi(
        "createProxyWithNonce",
        args=[singleton, bytes.fromhex(manifest["initializer"][2:]), int(manifest["salt_nonce"], 16)],
    )
    return result | {"feasible": not code, "reason": reason, "target_safe": safe, "calldata": calldata}


def build_report():
    manifest = replay.read_json(replay.DATA / "safe-replay.json")
    configs = replay.read_json(replay.CHAIN_CONFIGS)["mainnets"] if replay.CHAIN_CONFIGS.exists() else {}
    safe_networks = load_safe_networks()
    index = replay.index_networks(safe_networks)
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {}
        for chain in missing_chains():
            declared = int(configs.get(chain, {}).get("chain_id", 0)) or None
            item = replay.get_item(chain, index, safe_networks, declared)
            if not item:
                results.append({"chain": chain, "feasible": False, "reason": "missing Safe network metadata"})
                continue
            futures[pool.submit(assess, chain, configs.get(chain, {}), item, manifest)] = chain
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["chain"])
    feasible = [item["chain"] for item in results if item.get("feasible")]
    blocked = [f"{item['chain']}: {item['reason']}" for item in results if not item.get("feasible")]
    return {
        "source": {
            "salt_nonce": manifest["salt_nonce"],
            "initializer": manifest["initializer"],
            "owners": manifest["setup"]["owners"],
            "threshold": manifest["setup"]["threshold"],
        },
        "summary": {"total": len(results), "feasible": len(feasible), "blocked": len(blocked)},
        "results": results,
    }


def main():
    report = build_report()
    feasible = [item["chain"] for item in report["results"] if item.get("feasible")]
    blocked = [f"{item['chain']}: {item['reason']}" for item in report["results"] if not item.get("feasible")]
    print(f"Feasible ({len(feasible)}/{len(report['results'])}):")
    print("\n".join(feasible) or "-")
    print()
    print(f"Blocked ({len(blocked)}):")
    print("\n".join(blocked) or "-")
    print()
    print(json.dumps(report | {"results": [display(item) for item in report["results"]]}, indent=2))


if __name__ == "__main__":
    main()
