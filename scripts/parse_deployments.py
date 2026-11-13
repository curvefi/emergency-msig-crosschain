from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
import json

from download_data import load_chain_configs, load_deployment_state, load_safe_networks
import plan_safe_replay as replay

VERSION = "v1.3.0"
FACTORY_NAMES = ("proxy_factory", "safe_proxy_factory")
SINGLETON_NAMES = ("gnosis_safe_l2", "safe_l2", "gnosis_safe", "safe")
OUT = replay.DATA / "curve-multisig.json"
ENTRY_KEYS = ("chain", "chain_id", "address", "factory", "singleton", "status", "owners", "threshold", "last_read")


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


def same_signers(state, current):
    return (
        set(state["owners"]) == set(current["owners"])
        and len(state["owners"]) == len(current["owners"])
        and state["threshold"] == current["threshold"]
    )


def candidates(w3, item, manifest, known):
    """Known address first, then addresses derived from the replay and canonical factories."""
    found, errors = [], []
    if known:
        found.append((known["address"], known["factory"], known["singleton"]))
    pairs = [(manifest["factory"], manifest["singleton"])]
    deployments = v130(item)
    factory = pick(deployments, FACTORY_NAMES, ("Canonical contracts", "default"))
    singleton = pick(deployments, SINGLETON_NAMES, ("Canonical contracts", "default"))
    if factory and singleton:
        pairs.append((factory, singleton))
    for factory, singleton in dict.fromkeys(pairs):
        if not w3.eth.get_code(factory):
            continue
        try:
            proxy_code = w3.eth.contract(address=factory, abi=replay.FACTORY_ABI).functions.proxyCreationCode().call()
        except Exception as exc:
            errors.append(f"factory {factory}: {exc}")
            continue
        # The address commits to the initializer, so whatever lives there was created from the March setup.
        address = replay.proxy_address(manifest | {"factory": factory, "singleton": singleton}, proxy_code)
        found.append((address, factory, singleton))
    return list(dict.fromkeys(found)), errors


def assess(chain, config, item, manifest, current, known):
    chain_id = int(config.get("chain_id", item["chainId"]))
    try:
        w3, _, actual = replay.connect(chain, config, item, chain_id)
        options, errors = candidates(w3, item, manifest, known)
        for address, factory, singleton in options:
            if not w3.eth.get_code(address):
                continue
            state = replay.safe_state(w3, address)
            entry = {
                "chain": chain,
                "chain_id": actual,
                "address": address,
                "factory": factory,
                "singleton": singleton,
                "status": "synced" if same_signers(state, current) else "outdated_owners",
            }
            if entry["status"] != "synced":
                entry |= {"owners": state["owners"], "threshold": state["threshold"]}
            return {"chain": chain, "found": True, "entry": entry}
    except Exception as exc:
        return {"chain": chain, "found": False, "reason": f"unreachable: {exc}"}
    if errors:
        return {"chain": chain, "found": False, "reason": f"unreachable: {'; '.join(errors)}"}
    return {"chain": chain, "found": False, "reason": "safe not deployed"}


def baseline_state(manifest, configs, safe_networks, index):
    baseline = manifest["baseline"]
    item = replay.get_item(baseline["chain"], index, safe_networks, baseline["chain_id"])
    if not item:
        raise ValueError("missing Safe network metadata for baseline Safe")
    w3, _, actual = replay.connect(baseline["chain"], configs.get(baseline["chain"], {}), item, baseline["chain_id"])
    state = replay.safe_state(w3, baseline["address"])
    return baseline | {
        "chain_id": actual,
        "version": state["version"],
        "owners": state["owners"],
        "threshold": state["threshold"],
    }


def build():
    manifest = replay.read_json(replay.DATA / "safe-replay.json")
    extra = replay.load_extra_chains()
    configs = extra | load_chain_configs().get("mainnets", {})
    safe_networks = load_safe_networks()
    index = replay.index_networks(safe_networks)
    baseline = baseline_state(manifest, configs, safe_networks, index)
    current = {"owners": baseline["owners"], "threshold": baseline["threshold"]}
    previous_doc = replay.read_json(OUT) if OUT.exists() else {}
    # Ethereum is covered by baseline_safe; the unused copy of the replay Safe there is not listed.
    previous = {
        entry["chain"]: entry
        for entry in previous_doc.get("chains", [])
        if entry.get("chain_id") != baseline["chain_id"]
    }
    targets = set(load_deployment_state()["deployments"]["mainnets"]) | set(extra) | set(previous)
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {}
        for chain in sorted(targets):
            declared = int(configs.get(chain, {}).get("chain_id", 0)) or previous.get(chain, {}).get("chain_id")
            if declared == baseline["chain_id"]:
                continue
            item = replay.get_item(chain, index, safe_networks, declared)
            if not item and declared:
                item = {"chainId": declared, "rpc": []}
            if not item:
                results.append({"chain": chain, "found": False, "reason": "unreachable: no chain id or network metadata"})
                continue
            config = configs.get(chain, {"chain_id": declared})
            futures[pool.submit(assess, chain, config, item, manifest, current, previous.get(chain))] = chain
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["chain"])
    chains = []
    for result in results:
        if result.get("found"):
            chains.append(result["entry"])
        elif result["chain"] in previous:
            # Never drop a recorded Safe because it could not be read this time; keep the last observation.
            kept = {key: value for key, value in previous[result["chain"]].items() if key in ENTRY_KEYS}
            if kept.get("status") == "synced" and "owners" in previous_doc:
                # A synced entry's signers were the previous top-level ones; write them out before they change.
                kept |= {"owners": previous_doc["owners"], "threshold": previous_doc["threshold"]}
            last_read = kept.get("last_read") if kept.get("status") == "unreachable" else previous_doc.get("checked_at")
            chains.append(kept | {"status": "unreachable", "last_read": last_read})
    document = {
        "name": "curve-multisig",
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%d"),
        "safe_address": manifest["target_safe"],
        "version": manifest["source"]["version"],
        "master_copy": manifest["singleton"],
        "fallback_handler": manifest["fallback_handler"],
        "salt_nonce": manifest["salt_nonce"],
        "threshold": current["threshold"],
        "owners": current["owners"],
        "baseline_safe": baseline,
        "chains": chains,
    }
    return document, results


def main():
    document, results = build()
    OUT.write_text(json.dumps(document, indent=2) + "\n")
    by_status = {}
    for entry in document["chains"]:
        by_status.setdefault(entry["status"], []).append(entry["chain"])
    issues = [f"{item['chain']}: {item['reason']}" for item in results if not item.get("found")]
    print(f"Wrote {OUT}")
    for status in ("synced", "outdated_owners", "unreachable"):
        names = by_status.get(status, [])
        print(f"{status} ({len(names)}): {', '.join(names) or '-'}")
    print()
    print(f"Issues ({len(issues)}):")
    print("\n".join(issues) or "-")


if __name__ == "__main__":
    main()
