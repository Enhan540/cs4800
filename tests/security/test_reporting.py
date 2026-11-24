import json

import pytest

from scripts.security_report import control_evidence, read_private_evidence

pytestmark = pytest.mark.security


def test_missing_private_evidence_never_establishes_a_pass(tmp_path):
    assert read_private_evidence(tmp_path) == {}
    assert control_evidence({}, "main", "a" * 40, "V1.1.1")[0] == "unverified"


def test_private_observations_are_bound_to_the_reviewed_commit():
    document = {"branches": {"main": {"commit": "a" * 40, "controls": {"V1.1.1": {"status": "passed", "evidence": "Synthetic test evidence"}}}}}
    assert control_evidence(document, "main", "a" * 40, "V1.1.1") == ("passed", "Synthetic test evidence")
    assert control_evidence(document, "main", "b" * 40, "V1.1.1")[0] == "unverified"


@pytest.mark.parametrize("item", [{}, {"status": "passed"}, {"status": "ignored", "evidence": "Synthetic"}, {"status": "passed", "evidence": " "}, "passed"])
def test_incomplete_or_unknown_evidence_cannot_pass(item):
    document = {"branches": {"main": {"commit": "a" * 40, "controls": {"V1.1.1": item}}}}
    assert control_evidence(document, "main", "a" * 40, "V1.1.1")[0] == "unverified"


def test_unsupported_private_evidence_fails_closed(tmp_path):
    (tmp_path / "asvs-evidence.json").write_text(json.dumps({"schema_version": 999}))
    with pytest.raises(ValueError):
        read_private_evidence(tmp_path)
