import base64
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pyotp
import pytest
from pymongo import ReturnDocument

from services import mfa_service as mfa

pytestmark = pytest.mark.security


def factor_fixture(secured):
    app, db, ids, password = secured
    seed = pyotp.random_base32()
    factor, codes = mfa.factor_document(app, seed, -1)
    db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"mfa": factor}})
    return app, db, ids, seed, codes, db.accounts.find_one({"_id": ids["owner"]})


def test_rfc6238_vector_and_strict_input_bounds():
    public_vector = base64.b32encode(b"12345678901234567890").decode()
    assert mfa.matching_step(public_vector, "287082", 59) == 1
    for code in (None, {}, 42, "12345", "1" * 81, "١" * 6, "<img src=x>"):
        with pytest.raises(mfa.MFAError):
            mfa.matching_step(public_vector, code, 59)


@pytest.mark.parametrize("other_time", [30, 90])
def test_totp_rejects_previous_and_future_time_steps(other_time):
    public_vector = base64.b32encode(b"12345678901234567890").decode()
    with pytest.raises(mfa.MFAError):
        mfa.matching_step(public_vector, pyotp.TOTP(public_vector).at(other_time), timestamp=60)


def test_seed_encryption_and_recovery_storage(secured):
    app, db, ids, seed, codes, account = factor_fixture(secured)
    assert mfa.decrypt_seed(app, account["mfa"]["seed_ciphertext"]) == seed
    stored = json.dumps(account["mfa"])
    assert seed not in stored and all(code not in stored for code in codes)
    assert len(set(codes)) == 10 and len(account["mfa"]["recovery_code_hashes"]) == 10
    app.secret_key += "changed"
    with pytest.raises(mfa.MFAError):
        mfa.decrypt_seed(app, account["mfa"]["seed_ciphertext"])
    # Recovery codes can still recover a factor if seed decryption is unavailable.
    assert mfa.proof(app, account, codes[0])[0]


@pytest.mark.parametrize("kind", ["totp", "recovery"])
def test_factor_proof_is_single_use_under_concurrency(secured, kind):
    app, db, ids, seed, codes, account = factor_fixture(secured)
    value = pyotp.TOTP(seed).at(60) if kind == "totp" else codes[0]
    query, effect = mfa.proof(app, account, value, timestamp=60)
    def consume(_):
        return db.accounts.find_one_and_update({"$and": [{"_id": ids["owner"]}, query]}, effect, return_document=ReturnDocument.AFTER)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(doc is not None for doc in pool.map(consume, range(12))) == 1
    refreshed = db.accounts.find_one({"_id": ids["owner"]})
    with pytest.raises(mfa.MFAError):
        mfa.proof(app, refreshed, value, timestamp=60)


def test_proof_preparation_never_mutates_and_is_bound_to_factor_epoch(secured):
    app, db, ids, seed, codes, account = factor_fixture(secured)
    before = copy.deepcopy(account)
    query, effect = mfa.proof(app, account, codes[0])
    assert db.accounts.find_one({"_id": ids["owner"]}) == before
    db.accounts.update_one({"_id": ids["owner"]}, {"$set": {"mfa.epoch": "new-factor"}})
    assert db.accounts.find_one_and_update({"$and": [{"_id": ids["owner"]}, query]}, effect) is None


def test_factor_replacement_preserves_consumed_step_without_path_conflicts(secured):
    app, db, ids, seed, codes, account = factor_fixture(secured)
    query, effect = mfa.proof(app, account, pyotp.TOTP(seed).at(60), timestamp=60)
    replacement = dict(account["mfa"], epoch="replaced")
    update = mfa.merge_proof_update({"$set": {"mfa": replacement}}, effect)
    assert update["$set"]["mfa"]["last_used_step"] == 2
    assert "mfa.last_used_step" not in update["$set"]
    assert mfa.merge_proof_update({"$unset": {"mfa": ""}}, effect) == {"$unset": {"mfa": ""}}
