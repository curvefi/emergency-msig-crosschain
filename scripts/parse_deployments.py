from concurrent.futures import ThreadPoolExecutor, as_completed
import json

from download_data import load_deployment_state, load_safe_networks
import plan_safe_replay as replay

VERSION = "v1.3.0"
FACTORY_NAMES = ("proxy_factory", "safe_proxy_factory")
SINGLETON_NAMES = ("gnosis_safe_l2", "safe_l2", "gnosis_safe", "safe")
OUT = replay.DATA / "curve-multisig.json"


def v130(item):
    return {
        deployment["name"]: replay.deployment_addresses(deployment)
        for deployment in item.get("smartAccounts", [])
        if deployment.get("version") == VERSION and replay.deployment_addresses(deployment)
    }


def pick(mapping, names, labels):
    for name in names:
        if not mapping.get(name):
            continue
        for label in labels:
            if mapping[name].get(label):
                return mapping[name][label]
    return None


def assess(chain, config, item, manifest):
    chain_id = int(config.get("chain_id", item["chainId"]))
    try:
        w3, _, actual = replay.connect(chain, config, item, chain_id)
    except Exception as exc:
        return {"chain": chain, "found": False, "reason": str(exc)}
    deployments = v130(item)
    candidates = [(manifest["factory"], manifest["singleton"])]
    factory = pick(deployments, FACTORY_NAMES, ("Canonical contracts", "default"))
    singleton = pick(deployments, SINGLETON_NAMES, ("Canonical contracts", "default"))
    if factory and singleton:
        candidates.append((factory, singleton))
    expected = manifest["setup"]["owners"]
    for factory, singleton in dict.fromkeys(candidates):
        try:
            proxy_code = w3.eth.contract(address=factory, abi=replay.FACTORY_ABI).functions.proxyCreationCode().call()
        except Exception:
            continue
        address = replay.proxy_address(manifest | {"factory": factory, "singleton": singleton}, proxy_code)
        if not w3.eth.get_code(address):
            continue
        safe = w3.eth.contract(address=address, abi=replay.SAFE_ABI)
        try:
            owners = [replay.checksum(owner) for owner in safe.functions.getOwners().call()]
            threshold = safe.functions.getThreshold().call()
        except Exception:
            continue
        if set(owners) != set(expected) or len(owners) != len(expected) or threshold != manifest["setup"]["threshold"]:
            continue
        return {
            "chain": chain,
            "chain_id": actual,
            "found": True,
            "entry": {
                "chain": chain,
                "chain_id": actual,
                "address": address,
                "factory": factory,
                "singleton": singleton,
                "safe_client_url": f"https://safe-client.safe.global/v1/chains/{actual}/safes/{address}",
            },
        }
    reason = "safe not deployed"
    if len(candidates) == 1 and not any(deployments.get(name) for name in FACTORY_NAMES + SINGLETON_NAMES):
        reason = "missing Safe v1.3.0 factory/singleton"
    return {"chain": chain, "chain_id": actual, "found": False, "reason": reason}


def baseline_state(manifest, configs, safe_networks, index):
    baseline = manifest["baseline"]
    item = replay.get_item(baseline["chain"], index, safe_networks, baseline["chain_id"])
    if not item:
        raise ValueError("missing Safe network metadata for baseline Safe")
    w3, _, actual = replay.connect(baseline["chain"], configs.get(baseline["chain"], {}), item, baseline["chain_id"])
    state = replay.safe_state(w3, baseline["address"])
    expected = manifest["setup"]["owners"]
    if set(state["owners"]) != set(expected) or len(state["owners"]) != len(expected):
        raise ValueError("baseline Safe owner set mismatch")
    if state["threshold"] != manifest["setup"]["threshold"]:
        raise ValueError("baseline Safe threshold mismatch")
    return baseline | {
        "chain_id": actual,
        "version": state["version"],
        "owners": state["owners"],
        "threshold": state["threshold"],
        "safe_client_url": f"https://safe-client.safe.global/v1/chains/{actual}/safes/{state['address']}",
    }


def build():
    manifest = replay.read_json(replay.DATA / "safe-replay.json")
    configs = replay.read_json(replay.CHAIN_CONFIGS)["mainnets"] if replay.CHAIN_CONFIGS.exists() else {}
    safe_networks = load_safe_networks()
    index = replay.index_networks(safe_networks)
    baseline = baseline_state(manifest, configs, safe_networks, index)
    targets = sorted(load_deployment_state()["deployments"]["mainnets"])
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {}
        for chain in targets:
            declared = int(configs.get(chain, {}).get("chain_id", 0)) or None
            item = replay.get_item(chain, index, safe_networks, declared)
            if not item:
                results.append({"chain": chain, "found": False, "reason": "missing Safe network metadata"})
                continue
            futures[pool.submit(assess, chain, configs.get(chain, {}), item, manifest)] = chain
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["chain"])
    chains = [item["entry"] for item in results if item.get("found")]
    document = {
        "name": "curve-multisig",
        "safe_address": manifest["target_safe"],
        "version": manifest["source"]["version"],
        "master_copy": manifest["singleton"],
        "fallback_handler": manifest["fallback_handler"],
        "salt_nonce": manifest["salt_nonce"],
        "threshold": manifest["setup"]["threshold"],
        "owners": manifest["setup"]["owners"],
        "baseline_safe": baseline,
        "chains": chains,
    }
    return document, results


def main():
    document, results = build()
    OUT.write_text(json.dumps(document, indent=2) + "\n")
    found = [item["chain"] for item in results if item.get("found")]
    missing = [item["chain"] for item in results if item.get("reason") == "safe not deployed"]
    issues = [f"{item['chain']}: {item['reason']}" for item in results if not item.get("found") and item.get("reason") != "safe not deployed"]
    print(f"Wrote {OUT}")
    print(f"Found ({len(found)}/{len(results)}):")
    print("\n".join(found) or "-")
    print()
    print(f"Missing ({len(missing)}):")
    print("\n".join(missing) or "-")
    print()
    print(f"Issues ({len(issues)}):")
    print("\n".join(issues) or "-")


if __name__ == "__main__":
    main()
