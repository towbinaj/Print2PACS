"""Tests for Node validation and settings/destinations persistence."""

from __future__ import annotations

from print2pacs import config, paths
from print2pacs.model import Node, validate_aetitle


def test_aetitle_validation():
    assert validate_aetitle("PACS1") == ""
    assert validate_aetitle("") != ""
    assert validate_aetitle("A" * 17) != ""
    assert validate_aetitle("BAD\\AE") != ""
    assert validate_aetitle("", allow_empty=True) == ""


def test_node_validate_and_roundtrip():
    n = Node(name="PACS", aetitle="REMOTE", host="192.0.2.5", port=104)
    assert n.validate() == []
    assert Node.from_dict(n.to_dict()) == n
    assert n.calling("DEFAULT") == "DEFAULT"
    assert Node(name="x", aetitle="A", host="h", port=1,
                calling_aetitle="MINE").calling("DEFAULT") == "MINE"


def test_node_validate_flags_problems():
    bad = Node(name="", aetitle="", host="", port=99999)
    problems = bad.validate()
    assert len(problems) >= 3


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_cached_data_dir", tmp_path)
    s = config.load_settings()
    s.my_aetitle = "CUSTOMAE"
    s.default_series_number = 1234
    config.save_settings(s)
    again = config.load_settings()
    assert again.my_aetitle == "CUSTOMAE"
    assert again.default_series_number == 1234


def test_destinations_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_cached_data_dir", tmp_path)
    nodes = [Node(name="A", aetitle="AE1", host="192.0.2.1", port=104),
             Node(name="B", aetitle="AE2", host="192.0.2.2", port=11112)]
    config.save_destinations(nodes)
    loaded = config.load_destinations()
    assert [n.name for n in loaded] == ["A", "B"]
    assert loaded[1].port == 11112


def test_is_valid_uid():
    from print2pacs.model import is_valid_uid
    assert is_valid_uid("1.2.840.113619.2.55.3")
    assert not is_valid_uid("")
    assert not is_valid_uid("1.2.x")
    assert not is_valid_uid("1..2")
    assert not is_valid_uid("9" * 65)       # too long


def test_send_intent_validate_and_fingerprint():
    from print2pacs.model import SendIntent, StudyContext
    ctx = StudyContext(study_uid="1.2.3.4", patient_id="MRN1",
                       issuer_of_patient_id="HOSP-A", patient_name="DOE^J",
                       accession="ACC1")
    good = SendIntent.build(ctx, query_source="Q", destination="D",
                            content_signature="sig")
    assert good.validate() == []
    assert good.issuer_of_patient_id == "HOSP-A"

    bad = SendIntent.build(
        StudyContext(patient_id="", study_uid="nope", patient_name=""),
        query_source="Q", destination="D", content_signature="s")
    probs = bad.validate()
    assert any("Patient ID" in p for p in probs)
    assert any("Study Instance UID" in p for p in probs)
    assert any("name" in p for p in probs)

    # Destination or content change -> different fingerprint (invalidates confirm)
    assert (SendIntent.build(ctx, query_source="Q", destination="D2",
                             content_signature="sig").identity_fingerprint
            != good.identity_fingerprint)
    assert (SendIntent.build(ctx, query_source="Q", destination="D",
                             content_signature="sig2").identity_fingerprint
            != good.identity_fingerprint)
    # operator / timestamp do NOT change the fingerprint
    assert (SendIntent.build(ctx, query_source="Q", destination="D",
                             content_signature="sig", operator="x",
                             confirmed_at="t").identity_fingerprint
            == good.identity_fingerprint)


def test_destination_policy(tmp_path, monkeypatch):
    from print2pacs.model import Node
    pf = tmp_path / "destinations.policy.json"
    monkeypatch.setattr(config, "policy_path", lambda: pf)

    # No policy file: unmanaged, no approved-pair constraint.
    assert config.load_destination_policy() is None
    assert config.destinations_are_managed() is False
    assert config.pair_allowed("QR", "VNA") is None

    nodes = [Node(name="VNA", aetitle="VNA", host="127.0.0.1", port=104),
             Node(name="QR", aetitle="QR", host="127.0.0.1", port=105)]
    config.export_policy(nodes, pf, pairs=[("QR", "VNA")])

    assert config.destinations_are_managed() is True
    assert {n.name for n in config.load_destinations()} == {"VNA", "QR"}
    assert config.pair_allowed("QR", "VNA") is True       # approved
    assert config.pair_allowed("QR", "OTHER") is False    # not approved -> block
