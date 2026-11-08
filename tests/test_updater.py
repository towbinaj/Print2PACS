"""Tests for the self-update version logic and release lookup."""

from __future__ import annotations

import json

import pytest

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


def test_check_https_host_allowlist():
    updater._check_https_host("https://github.com/towbinaj/Print2PACS")
    updater._check_https_host("https://objects.githubusercontent.com/x")
    for bad in ("http://github.com/x",             # not https
                "https://evil.example.com/x",       # foreign host
                "https://github.com.evil.com/x"):   # look-alike host
        with pytest.raises(ValueError):
            updater._check_https_host(bad)


def test_download_rejects_missing_checksum(tmp_path):
    # Fail closed: no published SHA-256 means no install (no network needed).
    info = updater.UpdateInfo(
        version="9.9.9", tag="v9.9.9",
        url=("https://github.com/towbinaj/Print2PACS/releases/download/"
             "v9.9.9/Print2PACS.exe"),
        size=10, sha256_url=None, notes="")
    dest = tmp_path / "Print2PACS.exe"
    with pytest.raises(ValueError):
        updater.download(info, dest)
    assert not dest.exists()


def test_download_rejects_foreign_url(tmp_path):
    info = updater.UpdateInfo(
        version="9.9.9", tag="v9.9.9",
        url="https://evil.example.com/Print2PACS.exe",
        size=10, sha256_url="https://evil.example.com/Print2PACS.exe.sha256",
        notes="")
    with pytest.raises(ValueError):
        updater.download(info, tmp_path / "Print2PACS.exe")
