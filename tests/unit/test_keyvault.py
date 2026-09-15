"""keyvault：API Key 的加密存档。

这是项目里唯一一处自己拼的密码学构造（HMAC-SHA256 当 PRF 做 CTR + encrypt-then-MAC），
所以该验的都验一遍。
"""
import pytest

import keyvault

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def tmp_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(keyvault, "VAULT_FILE", tmp_path / "kv.txt")
    monkeypatch.setattr(keyvault, "_master_cache", None)
    yield


@pytest.mark.parametrize("text", [
    "tm_abcDEF-123_xyz", "", "中文也要能进去", "x" * 5000,
])
def test_roundtrip(text):
    assert keyvault.open_sealed(keyvault.seal(text)) == text


def test_same_plaintext_seals_differently():
    """每次换 nonce —— 否则一眼就能看出两把 Key 是不是同一个值。"""
    assert keyvault.seal("same") != keyvault.seal("same")


def test_master_key_file_is_created_0600(tmp_path):
    keyvault.master_key()
    assert keyvault.VAULT_FILE.exists()
    assert oct(keyvault.VAULT_FILE.stat().st_mode & 0o777) == "0o600"


def test_master_key_is_stable_across_reads(monkeypatch):
    first = keyvault.master_key()
    monkeypatch.setattr(keyvault, "_master_cache", None)   # 强制重新读文件
    assert keyvault.master_key() == first


def test_tampered_ciphertext_is_rejected():
    blob = keyvault.seal("tm_secret")
    raw = bytearray(__import__("base64").urlsafe_b64decode(blob))
    raw[20] ^= 0x01                                        # 动一个密文字节
    bad = __import__("base64").urlsafe_b64encode(bytes(raw)).decode()
    assert keyvault.open_sealed(bad) is None


def test_truncated_and_garbage_input_return_none():
    assert keyvault.open_sealed("") is None
    assert keyvault.open_sealed("!!!not base64!!!") is None
    assert keyvault.open_sealed("AAAA") is None


def test_wrong_master_key_cannot_open(monkeypatch, tmp_path):
    blob = keyvault.seal("tm_secret")
    monkeypatch.setattr(keyvault, "VAULT_FILE", tmp_path / "other.txt")
    monkeypatch.setattr(keyvault, "_master_cache", None)
    assert keyvault.open_sealed(blob) is None
