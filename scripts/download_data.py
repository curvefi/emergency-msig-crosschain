import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SOURCES = {
    "safe-networks": (
        DATA / "safe-networks.json",
        "https://raw.githubusercontent.com/safe-global/safe-docs/main/components/SupportedNetworks/networks.json",
    ),
    "deployment-state": (
        DATA / "deployment_state.json",
        "https://raw.githubusercontent.com/curvefi/blockhash-oracle/main/scripts/deployment/deployment_state.json",
    ),
    "chain-configs": (
        DATA / "chains.json",
        "https://raw.githubusercontent.com/curvefi/blockhash-oracle/main/scripts/chain-parse/chains.json",
    ),
}


def load(name, refresh=False):
    path, url = SOURCES[name]
    if refresh or not path.exists():
        DATA.mkdir(exist_ok=True)
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = response.read()
        data = json.loads(payload)
        path.write_bytes(payload)
        return data
    return json.loads(path.read_text())


def load_safe_networks(refresh=False):
    return load("safe-networks", refresh)


def load_deployment_state(refresh=False):
    return load("deployment-state", refresh)


def load_chain_configs(refresh=False):
    return load("chain-configs", refresh)


def main():
    for name, (path, _) in SOURCES.items():
        load(name)
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
