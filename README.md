# crosschain-multisig

Utilities for deploying, regenerating, and validating Curve multisigs across many EVM chains.

The signer set and threshold replicate the Curve Emergency DAO baseline multisig on Ethereum:
- docs: `https://docs.curve.finance/user/dao/emergency-dao/`
- baseline Safe: `0x467947EE34aF926cF1DCac093870f613C96B1E0c`

The repo currently tracks one multisig family:
- source replay Safe: `0x6d447e544D01a59cb0774763bf15526574CffFeD`
- fallback Moonbeam Safe: derived from the same initializer and salt, but with Moonbeam's canonical Safe `v1.3.0` factory/singleton

## What is in the repo

- [data/curve-multisig.json](data/curve-multisig.json)
  Generated manifest of deployed multisigs per chain. This is the main output artifact.
  - `owners` and `threshold` are the current signers, read live from the Ethereum Emergency DAO Safe (`baseline_safe`). Ethereum itself uses `baseline_safe` and is not listed under `chains`.
  - Each chain has a `status`: `synced` (same owner set and threshold as Ethereum), `outdated_owners` (owners or threshold differ; the chain's live `owners` and `threshold` are listed and a signer rotation is pending), or `unreachable` (the chain could not be read; the last known entry is kept, with `last_read` as the date of that reading when known).
  - Every copy is created with the March 2026 setup in `data/safe-replay.json`, which is what reproduces the address.
- [data/safe-replay.json](data/safe-replay.json)
  Source replay manifest: owners, threshold, fallback handler, singleton, salt, initializer, source tx.
- [data/extra-chains.json](data/extra-chains.json)
  Curve chains that blockhash-oracle does not list (Arc, Robinhood, Stable), with chain id and RPC from curve-core. The replay planner adds them to its targets.
- [scripts/download_data.py](scripts/download_data.py)
  Downloads the large upstream JSON inputs when missing.
- [scripts/parse_deployments.py](scripts/parse_deployments.py)
  Rebuilds `curve-multisig.json` from onchain state and marks each chain's signers as synced or outdated against the Curve Emergency DAO baseline Safe.
- [scripts/validate_simple.py](scripts/validate_simple.py)
  Validates deployed multisigs onchain by checking owners and threshold, and also checks the baseline Curve Emergency DAO Safe on Ethereum.
- [scripts/plan_safe_replay.py](scripts/plan_safe_replay.py)
  Finds chains where the original Safe can be replayed at the exact same address.
- [scripts/deploy_safe_replay.py](scripts/deploy_safe_replay.py)
  Broadcasts the exact-address replay deployment.
- [scripts/plan_safe_fallback.py](scripts/plan_safe_fallback.py)
  Finds missing chains where the same initializer and salt can be deployed with a different `v1.3.0` factory/singleton.
- [scripts/deploy_safe_fallback.py](scripts/deploy_safe_fallback.py)
  Broadcasts the fallback deployment.

## Data inputs

These large files are not meant to be committed:
- `data/chains.json`
- `data/safe-networks.json`
- `data/deployment_state.json`

They are auto-downloaded from:
- Safe docs: `https://raw.githubusercontent.com/safe-global/safe-docs/main/components/SupportedNetworks/networks.json`
- Curve blockhash-oracle: `https://raw.githubusercontent.com/curvefi/blockhash-oracle/main/scripts/deployment/deployment_state.json`
- Curve blockhash-oracle chain configs: `https://raw.githubusercontent.com/curvefi/blockhash-oracle/main/scripts/chain-parse/chains.json`

Fetch them explicitly:

```bash
uv run python scripts/download_data.py
```

Or just run any script that needs them. The script will download them if they are missing.

## Setup

```bash
uv sync
```

Optional RPC env vars:
- `ANKR_API_KEY`
- `DRPC_API_KEY`

Optional source-tx verification env var:
- `ETHERSCAN_API_KEY`

Optional deployer env vars:
- `ENCRYPTED_PK`
- `WEB3_PASSWORD`
- `PRIVATE_KEY`
- `WEB3_MAINNET_PK`

## Common commands

Rebuild the multisig manifest from chain state:

```bash
uv run python scripts/parse_deployments.py
```

Validate all entries in `curve-multisig.json` onchain:

```bash
uv run python scripts/validate_simple.py
```

See which chains support exact-address replay:

```bash
uv run python scripts/plan_safe_replay.py
```

Dry-run the exact replay deploy flow:

```bash
uv run python scripts/deploy_safe_replay.py
```

Broadcast exact replay deployments:

```bash
BROADCAST=1 uv run python scripts/deploy_safe_replay.py
```

See fallback targets for chains that cannot reuse the original factory/singleton:

```bash
uv run python scripts/plan_safe_fallback.py
```

Broadcast fallback deployments:

```bash
BROADCAST=1 uv run python scripts/deploy_safe_fallback.py
```

Limit deployment to specific chains:

```bash
ONLY_CHAINS=moonbeam BROADCAST=1 uv run python scripts/deploy_safe_fallback.py
```

Deploy the replay on the chains outside blockhash-oracle:

```bash
ONLY_CHAINS=arc,robinhood,stable BROADCAST=1 uv run python scripts/deploy_safe_replay.py
```

Both deploy scripts:
- default to dry-run
- ask for per-chain confirmation with `Deploy? [y/N]`
- print the submitted tx hash immediately

The replay deploy script also reads the new Safe back after each deployment and checks owners, threshold, singleton, fallback handler, guard, modules and nonce. A replayed Safe starts with the original March 2026 owners, so rotate it to the current signers before giving it any role.

## Current model

The code supports two deployment modes:

1. Exact replay
   Reuse the original `v1.3.0` EIP-155 factory, singleton, initializer, and salt to land at the same Safe address across chains.

2. Fallback replay
   Reuse the same initializer and salt, but switch to the chain's canonical Safe `v1.3.0` factory/singleton when exact replay is impossible.

`parse_deployments.py` understands both patterns and writes the actual per-chain address, factory, and singleton into `curve-multisig.json`.

## RPC metadata

The scripts use Curve's chain config JSON to prefer `ankr`, `drpc`, and public RPC endpoints. That file is downloaded into `data/chains.json` by `scripts/download_data.py`.

## Security notes

- The repo does not store private keys.
- The encrypted-key workflow in `secure_key_utils.py` expects the same local keychain setup used in the blockhash-oracle repo.
- Do not commit downloaded provider JSONs if they contain third-party RPC keys.
