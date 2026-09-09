"""db.py —— 用户 / 密码 / 会话。"""
import pytest

import db

pytestmark = pytest.mark.unit


def test_hash_password_is_salted_and_verifiable():
    salt, h = db.hash_password("hunter22")
    assert len(salt) == 32 and len(h) == 64
    assert db.verify_password("hunter22", salt, h)
    assert not db.verify_password("hunter23", salt, h)


def test_hash_password_same_input_different_salt_gives_different_hash():
    s1, h1 = db.hash_password("same")
    s2, h2 = db.hash_password("same")
    assert s1 != s2 and h1 != h2
    # 但给定 salt 后是确定性的
    assert db.hash_password("same", s1)[1] == h1


def test_create_and_fetch_user():
    uid = db.create_user("alice", "pw123456", "爱丽丝")
    u = db.get_user_by_id(uid)
    assert u["username"] == "alice"
    assert u["display_name"] == "爱丽丝"
    assert u["is_admin"] == 0
    assert db.get_user_by_username("alice")["id"] == uid
    assert db.get_user_by_username("nobody") is None


def test_create_user_display_name_defaults_to_username():
    uid = db.create_user("bob", "pw123456")
    assert db.get_user_by_id(uid)["display_name"] == "bob"


def test_username_unique_constraint():
    import sqlite3
    db.create_user("dup", "pw123456")
    with pytest.raises(sqlite3.IntegrityError):
        db.create_user("dup", "pw123456")


def test_verify_user():
    db.create_user("carol", "pw123456")
    assert db.verify_user("carol", "pw123456")["username"] == "carol"
    assert db.verify_user("carol", "wrong") is None
    assert db.verify_user("ghost", "pw123456") is None


def test_get_or_create_admin_is_idempotent():
    a1 = db.get_or_create_admin()
    a2 = db.get_or_create_admin()
    assert a1["id"] == a2["id"]
    assert a1["is_admin"] == 1
    assert a1["username"] == "admin"


def test_session_roundtrip_and_delete():
    uid = db.create_user("dave", "pw123456")
    token = db.create_session(uid)
    assert db.get_session_user(token)["id"] == uid
    db.delete_session(token)
    assert db.get_session_user(token) is None


def test_session_unknown_or_empty_token():
    assert db.get_session_user("") is None
    assert db.get_session_user(None) is None
    assert db.get_session_user("not-a-real-token") is None


def test_session_expired_token_rejected():
    """人工把 expires_at 拨到过去，验证过期会话不再放行。"""
    uid = db.create_user("erin", "pw123456")
    token = db.create_session(uid)
    conn = db.get_conn()
    conn.execute("UPDATE sessions SET expires_at=datetime('now','localtime','-1 hours') "
                 "WHERE token=?", (token,))
    conn.commit()
    conn.close()
    assert db.get_session_user(token) is None


def test_session_sliding_refresh_extends_expiry():
    uid = db.create_user("frank", "pw123456")
    token = db.create_session(uid)
    conn = db.get_conn()
    conn.execute("UPDATE sessions SET expires_at=datetime('now','localtime','+1 minutes') "
                 "WHERE token=?", (token,))
    conn.commit()
    before = conn.execute("SELECT expires_at FROM sessions WHERE token=?",
                          (token,)).fetchone()[0]
    conn.close()
    assert db.get_session_user(token) is not None       # 一次访问 = 一次续期
    conn = db.get_conn()
    after = conn.execute("SELECT expires_at FROM sessions WHERE token=?",
                         (token,)).fetchone()[0]
    conn.close()
    assert after > before


def test_set_last_trip():
    uid = db.create_user("grace", "pw123456")
    user = db.get_user_by_id(uid)
    tid = db.create_trip(user, "行程A", None, None)
    db.set_last_trip(uid, tid)
    assert db.get_user_by_id(uid)["last_trip_id"] == tid
    db.set_last_trip(uid, None)
    assert db.get_user_by_id(uid)["last_trip_id"] is None
