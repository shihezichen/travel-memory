"""db.py —— 只读分享链接：单链接语义、密码、过期。"""
import pytest

import db

pytestmark = pytest.mark.unit


@pytest.fixture
def trip(make_user):
    u = make_user()
    return db.create_trip(u, "分享行程", "2026-05-01", "2026-05-02")


def test_create_share_returns_token_and_is_readable(trip):
    tok = db.create_share(trip)
    s = db.get_share(tok)
    assert s["trip_id"] == trip
    assert s["has_password"] is False
    assert s["expires_at"] is None          # 永久
    assert db.count_shares_for_trip(trip) == 1


def test_create_share_is_single_link_per_trip(trip):
    """同一行程重复创建应复用同一个 token，而不是堆一串链接。"""
    t1 = db.create_share(trip)
    t2 = db.create_share(trip)
    assert t1 == t2
    assert db.count_shares_for_trip(trip) == 1


def test_recreate_share_overwrites_password(trip):
    tok = db.create_share(trip, password="secret")
    assert db.get_share(tok)["has_password"] is True
    again = db.create_share(trip)            # 不带密码 = 取消密码
    assert again == tok
    assert db.get_share(tok)["has_password"] is False


def test_share_password_verification(trip):
    tok = db.create_share(trip, password="s3cret")
    assert db.verify_share_password(tok, "s3cret") is True
    assert db.verify_share_password(tok, "wrong") is False
    assert db.verify_share_password(tok, "") is False
    assert db.verify_share_password("no-such-token", "s3cret") is False


def test_share_without_password_accepts_anything(trip):
    tok = db.create_share(trip)
    assert db.verify_share_password(tok, "") is True
    assert db.verify_share_password(tok, "whatever") is True


def test_share_expiry(trip):
    tok = db.create_share(trip, expire_days=7)
    assert db.get_share(tok)["expires_at"] is not None
    conn = db.get_conn()
    conn.execute("UPDATE shares SET expires_at=datetime('now','localtime','-1 days') "
                 "WHERE token=?", (tok,))
    conn.commit()
    conn.close()
    assert db.get_share(tok) is None
    assert db.count_shares_for_trip(trip) == 0


def test_expired_share_is_garbage_collected_on_read(trip):
    tok = db.create_share(trip, expire_days=1)
    conn = db.get_conn()
    conn.execute("UPDATE shares SET expires_at=datetime('now','localtime','-1 days') "
                 "WHERE token=?", (tok,))
    conn.commit()
    conn.close()
    db.get_share(tok)                       # 触发清理
    conn = db.get_conn()
    left = conn.execute("SELECT COUNT(*) FROM shares WHERE token=?", (tok,)).fetchone()[0]
    conn.close()
    assert left == 0


def test_delete_shares_for_trip(trip):
    db.create_share(trip)
    db.delete_shares_for_trip(trip)
    assert db.count_shares_for_trip(trip) == 0


def test_delete_trip_cascades_shares(trip):
    tok = db.create_share(trip)
    db.delete_trip(trip)
    assert db.get_share(tok) is None
