import base64
import re

from cryptography.fernet import Fernet


def normalize_fernet_key(value: str) -> bytes:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("Fernet keys must use ASCII encoding") from error

    if re.fullmatch(rb"[0-9a-fA-F]{64}", encoded):
        encoded = base64.urlsafe_b64encode(bytes.fromhex(value))

    Fernet(encoded)
    return encoded
