"""Field-level Fernet encryption for broker_guard."""
from cryptography.fernet import Fernet


def encrypt_field(plaintext: str, key: bytes) -> str:
    """Encrypt a plaintext field; return the Fernet token as a str."""
    return Fernet(key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_field(token: str, key: bytes) -> str:
    """Decrypt a Fernet token with the same key.

    Raises cryptography.fernet.InvalidToken on a wrong key or a tampered
    token -- it never silently returns garbage.
    """
    return Fernet(key).decrypt(token.encode("ascii")).decode("utf-8")
