# API-key handling per OWASP Secrets Management guidance:
#  - the key NEVER touches settings.json or any project file; it is stored
#    in the Windows Credential Manager (DPAPI-encrypted, per-user) via
#    `keyring` (WinVaultKeyring backend)
#  - masked input in the UI, never echoed, never logged
#  - format validation before storing; live validation is an explicit
#    user action (a minimal API call), not automatic
import re

import keyring

SERVICE = "HyperSpinClinic"
ACCOUNT = "anthropic_api_key"

_KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,}$")


def looks_valid(key: str) -> bool:
    return bool(_KEY_RE.match((key or "").strip()))


def store(key: str) -> None:
    key = (key or "").strip()
    if not looks_valid(key):
        raise ValueError("That does not look like an Anthropic API key (sk-ant-…).")
    keyring.set_password(SERVICE, ACCOUNT, key)


def load() -> "str | None":
    try:
        return keyring.get_password(SERVICE, ACCOUNT)
    except Exception:
        return None


def clear() -> None:
    try:
        keyring.delete_password(SERVICE, ACCOUNT)
    except Exception:
        pass


def is_stored() -> bool:
    return bool(load())


def test_key() -> "tuple[bool, str]":
    """Explicit user-triggered live check: one minimal API call."""
    key = load()
    if not key:
        return False, "No key stored."
    try:
        import anthropic
    except ImportError:
        return False, "The anthropic package is not installed."
    try:
        client = anthropic.Anthropic(api_key=key)
        client.models.list(limit=1)
        return True, "Key is valid — Anthropic API reachable."
    except anthropic.AuthenticationError:
        return False, "The API rejected this key (authentication failed)."
    except Exception as e:
        return False, f"Could not verify: {type(e).__name__}."


# ---- EmuMovies FTP credentials (same Credential Manager treatment) ----
EMU_USER = "emumovies_user"
EMU_PASS = "emumovies_pass"


def store_emumovies(user: str, password: str) -> None:
    user = (user or "").strip()
    if not user or not password:
        raise ValueError("Both username and password are required.")
    keyring.set_password(SERVICE, EMU_USER, user)
    keyring.set_password(SERVICE, EMU_PASS, password)


def load_emumovies() -> "tuple[str, str] | None":
    try:
        u = keyring.get_password(SERVICE, EMU_USER)
        p = keyring.get_password(SERVICE, EMU_PASS)
        return (u, p) if u and p else None
    except Exception:
        return None


def clear_emumovies() -> None:
    for acct in (EMU_USER, EMU_PASS):
        try:
            keyring.delete_password(SERVICE, acct)
        except Exception:
            pass


def emumovies_stored() -> bool:
    return load_emumovies() is not None
