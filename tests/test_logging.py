"""Tests for PHI redaction in logging (R11)."""

from __future__ import annotations

import logging

from print2pacs import logging_setup


def test_redact_phi_scrubs_dicom_uids():
    assert logging_setup.redact_phi(
        "stored to study 1.2.840.10008.1.2.3.4") == "stored to study <uid>"
    # plain numbers and version-like values are left alone
    assert logging_setup.redact_phi("converted 5 pages") == "converted 5 pages"
    assert logging_setup.redact_phi("level 4.1 v1.0.8") == "level 4.1 v1.0.8"


def test_redaction_filter_rewrites_record():
    f = logging_setup._PHIRedactionFilter()
    rec = logging.LogRecord(
        "print2pacs.test", logging.INFO, __file__, 1,
        "series %s ready", ("1.2.3.4.5.6.7.8.9",), None)
    assert f.filter(rec) is True
    assert "<uid>" in rec.getMessage()
    assert "1.2.3.4.5.6.7.8.9" not in rec.getMessage()
