"""Optional TOTP factors with encrypted seeds and atomic, single-use proofs."""

import base64
import hashlib
import re
import secrets
import time

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class MFAError(ValueError):
    """A deliberately generic verification failure, without secret values."""


def enabled(account):
    value = account.get("mfa") or {}
    if not isinstance(value, dict):
        raise MFAError("Verification is unavailable")
    return value.get("enabled") is True


def cipher(app):
    root = app.secret_key.encode() if isinstance(app.secret_key, str) else app.secret_key
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"GroupPlanner/MFA/seeds/v1").derive(root)
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_seed(app, value):
    return cipher(app).encrypt(value.encode("ascii")).decode("ascii")


def decrypt_seed(app, value):
    try:
        return cipher(app).decrypt(value.encode("ascii")).decode("ascii")
    except (InvalidToken, UnicodeError, AttributeError) as exc:
        raise MFAError("Verification is unavailable") from exc


def recovery_hash(value):
    normalized = value.replace("-", "").replace(" ", "").upper() if isinstance(value, str) else ""
    if not re.fullmatch(r"[0-9A-F]{32}", normalized, flags=re.ASCII):
        raise MFAError("Invalid verification code")
    return hashlib.sha256(b"GroupPlanner/MFA/recovery/v1\0" + normalized.encode("ascii")).hexdigest()


def new_recovery_codes():
    codes = ["-".join(raw[i:i + 8] for i in range(0, 32, 8)) for raw in (secrets.token_hex(16).upper() for _ in range(10))]
    return codes, [recovery_hash(value) for value in codes]


def matching_step(seed, code, timestamp=None):
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code, flags=re.ASCII):
        raise MFAError("Invalid verification code")
    try:
        return pyotp.TOTP(seed).verify_and_get_timecode(code, for_time=otp_timestamp() if timestamp is None else timestamp, valid_window=0)
    except (pyotp.exceptions.OTPVerificationError, ValueError) as exc:
        raise MFAError("Invalid verification code") from exc


def otp_timestamp():
    return time.time()


def factor_document(app, seed, step):
    codes, digests = new_recovery_codes()
    return {"enabled": True, "epoch": secrets.token_hex(16), "seed_ciphertext": encrypt_seed(app, seed), "last_used_step": step, "recovery_code_hashes": digests}, codes


def proof(app, account, value, *, timestamp=None):
    """Return a filter/update pair; the caller applies both in one account CAS."""
    if not enabled(account):
        return {"mfa.enabled": {"$ne": True}}, {}
    factor = account["mfa"]
    if not isinstance(value, str) or len(value) > 80:
        raise MFAError("Invalid verification code")
    if re.fullmatch(r"[0-9]{6}", value, flags=re.ASCII):
        step = matching_step(decrypt_seed(app, factor.get("seed_ciphertext")), value, timestamp)
        if step <= factor.get("last_used_step", -1):
            raise MFAError("Invalid or already used verification code")
        return {"mfa.enabled": True, "mfa.epoch": factor["epoch"], "mfa.last_used_step": {"$lt": step}}, {"$set": {"mfa.last_used_step": step}}
    digest = recovery_hash(value)
    if not any(secrets.compare_digest(digest, item) for item in factor.get("recovery_code_hashes", [])):
        raise MFAError("Invalid or already used verification code")
    return {"mfa.enabled": True, "mfa.epoch": factor["epoch"], "mfa.recovery_code_hashes": digest}, {"$pull": {"mfa.recovery_code_hashes": digest}}


def merge_proof_update(update, effect):
    result = {operator: dict(fields) for operator, fields in update.items()}
    replaces_factor = "mfa" in result.get("$set", {}) or "mfa" in result.get("$unset", {})
    for operator, fields in effect.items():
        if replaces_factor:
            # Replacing/removing the factor also invalidates old recovery codes.
            if operator == "$set" and "mfa.last_used_step" in fields and "mfa" in result.get("$set", {}):
                factor = dict(result["$set"]["mfa"])
                factor["last_used_step"] = max(factor.get("last_used_step", -1), fields["mfa.last_used_step"])
                result["$set"]["mfa"] = factor
            continue
        result.setdefault(operator, {}).update(fields)
    return result
