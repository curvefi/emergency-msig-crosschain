import base58
import json
import keyring
from cryptography.fernet import Fernet
from eth_account import Account

KEYCHAIN_SERVICE = "web3_credentials"
KEYCHAIN_USERNAME = "deployer"


def get_keyring_key() -> bytes:
    key = keyring.get_password(KEYCHAIN_SERVICE, f"{KEYCHAIN_USERNAME}_key")
    if not key:
        raise RuntimeError("missing keyring key for web3_credentials/deployer_key")
    return key.encode()


def decrypt_private_key(encrypted_blob: str, password: str) -> bytes:
    encrypted_data = base58.b58decode(encrypted_blob)
    decrypted_json = Fernet(get_keyring_key()).decrypt(encrypted_data)
    encrypted_key = json.loads(decrypted_json)
    return Account.decrypt(encrypted_key, password)


def get_web3_account(encrypted_blob: str, password: str):
    return Account.from_key(decrypt_private_key(encrypted_blob, password))

