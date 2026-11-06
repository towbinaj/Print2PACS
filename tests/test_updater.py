"""Tests for the self-update version logic and release lookup."""

from __future__ import annotations

import json

from print2pacs import updater


def test_version_compare():
    assert updater.is_newer("1.2.0", "1.1.9")
    assert updater.is_newer("v0.2.0", "0.1.9")
    assert updater.is_newer("1.0.1", "1.0.0")
    assert not updater.is_newer("0.1.0", "0.1.0")
    assert not updater.is_newer("1.0.0", "1.2.0")


def test_cannot_self_update_from_source():
    # Not a frozen exe under pytest.
    assert updater.can_self_update() is False


def test_check_latest_offline_returns_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(updater, "_get", boom)
    assert updater.check_latest() is None


def test_check_latest_finds_newer(monkeypatch):
    payload = {
        "tag_name": "v99.0.0",
        "body": "Big release",
        "assets": [
            {"name": "Print2PACS.exe", "size": 123,
             "browser_download_url": "https://example.org/Print2PACS.exe"},
            {"name": "Print2PACS.exe.sha256",
             "browser_download_url": "https://example.org/Print2PACS.exe.sha256"},
        ],
    }
    monkeypatch.setattr(updater, "_get",
                        lambda *a, **k: json.dumps(payload).encode())
    info = updater.check_latest()
    assert info is not None
    assert info.version == "99.0.0"
    assert info.url.endswith("Print2PACS.exe")
    assert info.sha256_url and info.sha256_url.endswith(".sha256")


def test_check_latest_same_version_returns_none(monkeypatch):
    payload = {"tag_name": f"v{updater.current_version()}", "assets": []}
    monkeypatch.setattr(updater, "_get",
                        lambda *a, **k: json.dumps(payload).encode())
    assert updater.check_latest() is None
