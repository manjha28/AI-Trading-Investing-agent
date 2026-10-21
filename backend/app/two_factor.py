import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import datetime
from urllib.parse import quote

from cryptography.fernet import Fernet

from app.fernet_keys import normalize_fernet_key


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def encrypt_secret(secret: str, key: str) -> str:
    return Fernet(normalize_fernet_key(key)).encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_secret(encrypted_secret: str, key: str) -> str:
    return Fernet(normalize_fernet_key(key)).decrypt(encrypted_secret.encode("ascii")).decode("ascii")


def provisioning_uri(secret: str, account: str) -> str:
    label = quote(f"Trading Copilot:{account}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer=Trading%20Copilot"
        "&algorithm=SHA1&digits=6&period=30"
    )


def matching_counter(
    secret: str,
    code: str,
    now: datetime | None = None,
    window: int = 1,
) -> int | None:
    try:
        key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    except (ValueError, TypeError):
        return None
    timestamp = now.timestamp() if now is not None else time.time()
    current_counter = int(timestamp // 30)
    for counter in range(max(0, current_counter - window), current_counter + window + 1):
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        value = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
        if hmac.compare_digest(f"{value:06d}", code):
            return counter
    return None


def code_for_counter(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{value:06d}"
