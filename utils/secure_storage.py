"""Restricted local credential storage with an optional externally managed Fernet key.

Legacy plaintext JSON is migrated on first read. The key file provides defense
against casual file disclosure, not against an attacker who can read the entire
plugin_data directory. Set VA_CREDENTIAL_KEY in the service environment and
protect that secret separately for stronger at-rest separation.
"""
import json
import os
import tempfile

from cryptography.fernet import Fernet, InvalidToken

_HEADER = b"VAENC1:"
_KEY_NAME = ".credentials.key"
_ENV_KEY = "VA_CREDENTIAL_KEY"


class CredentialStoreError(RuntimeError):
    """Credential file or key is unavailable; do not silently reset state."""


def _directory(path: str) -> str:
    return os.path.dirname(os.path.abspath(path))


def _restrict_dir(folder: str) -> None:
    os.makedirs(folder, mode=0o700, exist_ok=True)
    if os.name == "posix":
        os.chmod(folder, 0o700)


def _fernet(path: str) -> Fernet:
    folder = _directory(path)
    value = os.environ.get(_ENV_KEY)
    if value:
        key = value.encode("ascii")
    else:
        key_path = os.path.join(folder, _KEY_NAME)
        _restrict_dir(folder)
        if not os.path.exists(key_path):
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as file:
                    file.write(Fernet.generate_key())
                    file.flush()
                    os.fsync(file.fileno())
        try:
            with open(key_path, "rb") as file:
                key = file.read().strip()
            if os.name == "posix":
                os.chmod(key_path, 0o600)
        except OSError as exc:
            raise CredentialStoreError("credential key unavailable") from exc
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise CredentialStoreError("invalid credential key") from exc


def write_secure_json(path: str, value) -> None:
    """Atomically replace a JSON file with authenticated encrypted content."""
    folder = _directory(path)
    _restrict_dir(folder)
    try:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        payload = _HEADER + _fernet(path).encrypt(data)
    except (TypeError, ValueError) as exc:
        raise CredentialStoreError("cannot serialize credential data") from exc
    name = None
    try:
        fd, name = tempfile.mkstemp(prefix=".credential-", dir=folder)
        with os.fdopen(fd, "wb") as file:
            if os.name == "posix":
                os.fchmod(file.fileno(), 0o600)
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
        name = None
    except OSError as exc:
        raise CredentialStoreError("cannot persist credentials") from exc
    finally:
        if name is not None:
            try:
                os.unlink(name)
            except OSError:
                pass


def read_secure_json(path: str, default=None):
    """Read encrypted data; encrypt legacy JSON in place, or fail closed."""
    try:
        with open(path, "rb") as file:
            raw = file.read()
    except FileNotFoundError:
        return default
    except OSError as exc:
        raise CredentialStoreError("cannot read credentials") from exc
    if not raw:
        raise CredentialStoreError("empty credential file")
    if raw.startswith(_HEADER):
        try:
            decoded = _fernet(path).decrypt(raw[len(_HEADER):])
            return json.loads(decoded.decode("utf-8"))
        except (InvalidToken, UnicodeError, ValueError, TypeError) as exc:
            raise CredentialStoreError("cannot decrypt credentials: missing/wrong key or damaged file") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise CredentialStoreError("malformed legacy credential JSON") from exc
    write_secure_json(path, value)
    return value
