"""旅行记忆系统 — 数据层 (SQLite) + 多用户"""
import datetime
import hashlib
import hmac
import json
import secrets
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    display_name TEXT,
    is_admin INTEGER DEFAULT 0,
    last_trip_id INTEGER,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    description TEXT,
    status TEXT DEFAULT 'planning',            -- 状态机: planning/traveling/completed/archived
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    start_date TEXT,
    end_date TEXT,
    description TEXT,
    route_mode TEXT DEFAULT 'drive',
    is_waypoint INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_points_trip_date ON points(trip_id, start_date);

CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    filename TEXT NOT NULL,
    thumb_filename TEXT,
    exif_time TEXT,
    exif_lat REAL,
    exif_lng REAL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_media_point ON media(point_id);

-- 视频清晰度档位。一个 media 一行一档（720p/480p…），原画档不在这里
-- （原画就是 media.filename 本身，由接口合成出来，永远可用）。
-- status: pending 排队中 / running 转码中 / done 可播 / failed 失败（可重试）
-- 失败的档位不影响播放：前端永远能回退到原画。
CREATE TABLE IF NOT EXISTS media_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    filename TEXT,
    width INTEGER,
    height INTEGER,
    bitrate INTEGER,
    filesize INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(media_id, label)
);
CREATE INDEX IF NOT EXISTS idx_variants_media ON media_variants(media_id);

CREATE TABLE IF NOT EXISTS shares (
    token TEXT PRIMARY KEY,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    password_hash TEXT,
    salt TEXT,
    expires_at TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS route_cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

-- AI 生成内容落库（P1-2: 总结/路书/明信片/日记持久化，可追溯 + 复用）
CREATE TABLE IF NOT EXISTS generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                        -- trip_summary/route_guide/postcard/diary
    style TEXT DEFAULT '旅行者视角',
    language TEXT DEFAULT 'zh',
    text TEXT NOT NULL,
    tokens_used INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_generations_trip ON generations(trip_id, kind);

-- ===== 领域 API 层新增（2026-08-24）=====

-- API Key（对外 agent 认证，与用户登录 session 分离）
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    name TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scope TEXT NOT NULL DEFAULT 'read',       -- read/write/share/admin 逗号分隔
    expires_at TEXT,
    last_used_at TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    revoked INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);

-- 审计日志（谁/何时/调了什么/是否成功/token 消耗）
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_id INTEGER REFERENCES api_keys(id) ON DELETE SET NULL,
    user_id INTEGER,
    action TEXT NOT NULL,
    method TEXT,
    path TEXT,
    scope TEXT,
    status TEXT,                              -- ok/error
    detail TEXT,
    tokens_used INTEGER DEFAULT 0,
    latency_ms INTEGER,
    client_request_id TEXT,
    client_ip TEXT,                           -- 请求来自哪儿（见 routers/deps.client_ip）
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);

-- 用户长期偏好画像（联想层沉淀）
CREATE TABLE IF NOT EXISTS user_prefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pref_key TEXT NOT NULL,
    pref_value TEXT,
    strength REAL DEFAULT 0.5,
    evidence_count INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(user_id, pref_key)
);
CREATE INDEX IF NOT EXISTS idx_prefs_user ON user_prefs(user_id);

-- 点级情绪/场景元数据（N-7）：感官记忆重建的地基，稀疏可增量更新
CREATE TABLE IF NOT EXISTS point_meta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    mood TEXT,                                 -- 情绪标签（如「宁静」「兴奋」「怀旧」）
    weather TEXT,                              -- 天气（如「晴」「海边微风」）
    scene TEXT,                                -- 场景（如「海边日落」「古城街巷」）
    time_of_day TEXT,                          -- 时段（如「黄昏」「清晨」）
    season TEXT,                               -- 季节（规则可算，LLM 确认）
    meta_json TEXT,                            -- 扩展感官字段（气味/声音/温度等，JSON）
    source TEXT DEFAULT 'llm',                 -- rule / llm
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(point_id)
);
CREATE INDEX IF NOT EXISTS idx_point_meta_point ON point_meta(point_id);

-- 幂等键（P0-2）：写操作 client_request_id 去重，防远程 agent 网络重试重复建点/建行程
CREATE TABLE IF NOT EXISTS idempotency_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_id INTEGER,
    client_request_id TEXT NOT NULL,
    action TEXT,
    status TEXT DEFAULT 'completed',          -- completed / pending
    result TEXT,                               -- JSON：首次执行的响应结果（幂等命中时原样返回）
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(api_key_id, client_request_id)
);
CREATE INDEX IF NOT EXISTS idx_idem_key ON idempotency_keys(api_key_id, client_request_id);

-- 断点续传会话。大文件被切成分片逐个上传，这张表记「传到哪了」，
-- 使浏览器刷新/断网之后还能接着传（见 app.py 的 /api/upload-sessions/* 一组接口）。
-- fingerprint 是客户端算的文件指纹（首若干 MB + 大小 + 名字 + lastModified 的哈希），
-- 同一个 (point_id, owner_id, fingerprint) 只会有一个活跃会话 —— 重复「创建」直接复用。
CREATE TABLE IF NOT EXISTS upload_sessions (
    session_id TEXT PRIMARY KEY,
    point_id INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    filename TEXT NOT NULL,
    filesize INTEGER NOT NULL,
    chunk_size INTEGER NOT NULL,
    total_chunks INTEGER NOT NULL,
    mtype TEXT,                                -- photo / video（按扩展名判定，建会话时就定死）
    status TEXT DEFAULT 'uploading',           -- uploading / completed / aborted
    media_id INTEGER,                          -- 合并成功后写入，便于幂等重复 complete
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_upload_sessions_lookup
    ON upload_sessions(point_id, owner_id, fingerprint, status);
CREATE INDEX IF NOT EXISTS idx_upload_sessions_updated ON upload_sessions(updated_at);

-- 已收到的分片。刻意用「一行一个分片」而不是在 upload_sessions 里存一个 JSON 位图：
-- 同一个文件的多个分片是并发上传的，JSON 列会退化成读-改-写，丢更新是必然的。
-- 拆成独立行之后每个分片各写各的，靠主键做幂等（重传同一片 = INSERT OR REPLACE）。
-- OSS 直传之后的「回源存档」队列。
-- 直传的文件只在 OSS 上，本机一个字节都没有；缩略图、EXIF、转码档位都还得靠本地
-- 那份原件，备份更是只能存在本地。所以每传成功一个对象就在这里排一条，
-- 后台线程（oss_backfill.py）慢慢把它拉回 uploads/ 再补完这些收尾动作。
-- 状态存库而不是只存内存：进程重启后没拉完的必须还能接着拉。
CREATE TABLE IF NOT EXISTS oss_backfill (
    media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    oss_key TEXT NOT NULL,
    filesize INTEGER,
    mtype TEXT,
    status TEXT DEFAULT 'pending',             -- pending / running / done / error
    attempts INTEGER DEFAULT 0,
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_oss_backfill_status ON oss_backfill(status, updated_at);

CREATE TABLE IF NOT EXISTS upload_chunks (
    session_id TEXT NOT NULL REFERENCES upload_sessions(session_id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (session_id, idx)
);
"""

# 会话有效期（天）
SESSION_HOURS = 3  # 登录会话时长：3 小时无活动自动下线（滑动过期）
# 滑动续期的写节流：expires_at 距「满血」还差不到这么多分钟，就不重写。
# 打开一个行程页要打 7 个 API，每个都续期一次等于 7 次写事务，纯属浪费。
SESSION_RENEW_SKEW_MINUTES = 5


def get_conn():
    # timeout 是「撞上写锁时等多久」，默认 5s。WAL 下几乎撞不上，留 15s 是给
    # checkpoint / 备份快照这类偶发的长写操作兜底，免得高并发下直接抛 database is locked。
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # synchronous 是连接级 pragma（不像 journal_mode 会持久化到库文件），所以每次都要设。
    # WAL + NORMAL 下写事务不再每次 fsync，掉电最多丢最近几个事务、不会损坏库。
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db():
    conn = get_conn()
    # WAL：读写不再互斥，写锁只在提交瞬间持有。这是并发下最大的一处收益，
    # journal_mode 是库文件的持久属性，设一次就一直生效，所以只在这里做。
    # 副产物是 data.db-wal / data.db-shm 两个文件（.gitignore 已包含）。
    # 冷备用的是 sqlite3 的 backup() API，在 WAL 下同样能拿到一致性快照。
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        # 某些网络文件系统不支持 WAL 需要的共享内存，失败就继续用原来的模式。
        pass
    conn.executescript(SCHEMA)
    # 兼容旧库：trips 若无 user_id 列则补上（历史数据 user_id 置 NULL）
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(trips)")]
    if "user_id" not in cols:
        conn.execute("ALTER TABLE trips ADD COLUMN user_id INTEGER REFERENCES users(id)")
    # 兼容旧库：trips 若无 status 列则补上（历史行程默认 planning）
    if "status" not in cols:
        conn.execute("ALTER TABLE trips ADD COLUMN status TEXT DEFAULT 'planning'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trips_user ON trips(user_id)")
    # 2026-09-16 短暂存在过一列 key_enc（Key 的加密存档，用来「验密码后取回」）。
    # 撤掉了 —— Key 只存 sha256 才是对的，服务器上不该有能还原出明文的东西。
    # 列本身 sqlite 不好删，就地清空，别让残留的密文躺在库里。
    apikey_cols = [r["name"] for r in conn.execute("PRAGMA table_info(api_keys)")]
    if "key_enc" in apikey_cols:
        conn.execute("UPDATE api_keys SET key_enc=NULL WHERE key_enc IS NOT NULL")
    # 兼容旧库：audit_log 若无 client_ip 列则补上（历史记录留空 —— 当时没采）
    audit_cols = [r["name"] for r in conn.execute("PRAGMA table_info(audit_log)")]
    if "client_ip" not in audit_cols:
        conn.execute("ALTER TABLE audit_log ADD COLUMN client_ip TEXT")
    # 兼容旧库：shares 若无 expires_at 列则补上（历史分享视为永久有效）
    share_cols = [r["name"] for r in conn.execute("PRAGMA table_info(shares)")]
    if "expires_at" not in share_cols:
        conn.execute("ALTER TABLE shares ADD COLUMN expires_at TEXT")
    if "password_hash" not in share_cols:
        conn.execute("ALTER TABLE shares ADD COLUMN password_hash TEXT")
    if "salt" not in share_cols:
        conn.execute("ALTER TABLE shares ADD COLUMN salt TEXT")
    # 兼容旧库：points 若无 route_mode 列则补上（默认驾车 'drive'）
    point_cols = [r["name"] for r in conn.execute("PRAGMA table_info(points)")]
    if "route_mode" not in point_cols:
        conn.execute("ALTER TABLE points ADD COLUMN route_mode TEXT DEFAULT 'drive'")
    if "sort_order" not in point_cols:
        conn.execute("ALTER TABLE points ADD COLUMN sort_order INTEGER DEFAULT 0")
    if "is_waypoint" not in point_cols:
        conn.execute("ALTER TABLE points ADD COLUMN is_waypoint INTEGER DEFAULT 0")
    if "coord_sys" not in point_cols:
        # 存量数据按 GCJ-02 打标（高德前端一直按火星坐标渲染）
        conn.execute("ALTER TABLE points ADD COLUMN coord_sys TEXT DEFAULT 'gcj02'")
    if "elevation" not in point_cols:
        # 海拔（米）。外部高程服务按需回填一次后就缓存在这里，NULL = 还没查过
        conn.execute("ALTER TABLE points ADD COLUMN elevation REAL")
    if "route_json" not in point_cols:
        # 这一点「到下一点」定下来的那条路线，JSON：{path, distance, time, label, roadRuns}。
        # NULL = 没定过，每次加载现去规划。
        #
        # 为什么要存：高德是按「此刻能不能走」规划的，同一段路今天给 328 公里、
        # 明天给 1361 公里（独库公路十月到五月封山），刷新一次线就换个样子。
        # 而这里记的是已经走过的路，不该随规划结果漂移。用户在「选择路线方案」里
        # 挑定哪条，就把哪条存下来。
        conn.execute("ALTER TABLE points ADD COLUMN route_json TEXT")
    # 兼容旧库：media 若无 caption 列则补上（每张照片/视频的一段文字描述）
    media_cols = [r["name"] for r in conn.execute("PRAGMA table_info(media)")]
    if "caption" not in media_cols:
        conn.execute("ALTER TABLE media ADD COLUMN caption TEXT")
    if "sort_order" not in media_cols:
        # 点详情里可以像发朋友圈那样拖拽排序，顺序存这里。
        # 默认 0 = 没排过，此时仍按拍摄时间排（见 list_media 的 ORDER BY）
        conn.execute("ALTER TABLE media ADD COLUMN sort_order INTEGER DEFAULT 0")
    # 兼容旧库：media_variants 表可能是早期版本建的，缺列就补上。
    # 表本身在 SCHEMA 里 CREATE IF NOT EXISTS，所以这里只管列。
    variant_cols = [r["name"] for r in conn.execute("PRAGMA table_info(media_variants)")]
    if variant_cols:            # 表存在才谈得上补列
        if "bitrate" not in variant_cols:
            # 档位实际码率（kb/s），NULL = 还没探测到（转码未完成或 ffprobe 缺失）
            conn.execute("ALTER TABLE media_variants ADD COLUMN bitrate INTEGER")
        if "filesize" not in variant_cols:
            # 档位文件字节数，NULL = 还没转好
            conn.execute("ALTER TABLE media_variants ADD COLUMN filesize INTEGER")
        if "error" not in variant_cols:
            # 最近一次转码失败的原因，成功后清回 NULL
            conn.execute("ALTER TABLE media_variants ADD COLUMN error TEXT")
        if "updated_at" not in variant_cols:
            conn.execute("ALTER TABLE media_variants ADD COLUMN updated_at TEXT")
    # 兼容旧库：upload_sessions 补直传要用的两列。
    # 走直传的会话不落 upload_chunks（分片直接进 OSS，本机不经手），
    # 「传到哪儿了」以 OSS 的 ListParts 为准。
    us_cols = [r["name"] for r in conn.execute("PRAGMA table_info(upload_sessions)")]
    if us_cols:
        if "oss_key" not in us_cols:
            conn.execute("ALTER TABLE upload_sessions ADD COLUMN oss_key TEXT")
        if "oss_upload_id" not in us_cols:
            conn.execute("ALTER TABLE upload_sessions ADD COLUMN oss_upload_id TEXT")
    # 兼容旧库：users 若无 last_trip_id 列则补上（记住上次打开的行程，跨设备生效）
    user_cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
    if "last_trip_id" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN last_trip_id INTEGER")
    # 过期会话只增不删（登出才 DELETE），启动时顺手清一次。
    # 表本身很小，清理是为了别让它无限长下去。
    conn.execute("DELETE FROM sessions WHERE expires_at IS NOT NULL "
                 "AND expires_at <= datetime('now','localtime')")
    conn.commit()
    conn.close()


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


# ---------- 密码 ----------
_PBKDF2_ITERS = 100_000


def hash_password(password: str, salt: str = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERS
    )
    return salt, h.hex()


def verify_password(password: str, salt: str, expected: str) -> bool:
    _, h = hash_password(password, salt)
    return hmac.compare_digest(h, expected)


# ---------- 用户 ----------
def create_user(username, password, display_name="", is_admin=0):
    salt, phash = hash_password(password)
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO users(username, password_hash, salt, display_name, is_admin) "
        "VALUES(?,?,?,?,?)",
        (username, phash, salt, display_name or username, is_admin),
    )
    conn.commit()
    uid = cur.lastrowid
    conn.close()
    return uid


def get_user_by_username(username):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_last_trip(user_id, trip_id):
    """记住该用户最后打开的行程（跨设备恢复；trip_id 为 None 时清空）"""
    conn = get_conn()
    conn.execute("UPDATE users SET last_trip_id=? WHERE id=?", (trip_id, user_id))
    conn.commit()
    conn.close()


def verify_user(username, password):
    u = get_user_by_username(username)
    if not u:
        return None
    if verify_password(password, u["salt"], u["password_hash"]):
        return u
    return None


def get_or_create_admin():
    """管理员账号（token 直登用），username 固定 admin，is_admin=1"""
    admin = get_user_by_username("admin")
    if admin:
        return admin
    salt, phash = hash_password(secrets.token_urlsafe(24))
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO users(username, password_hash, salt, display_name, is_admin) "
        "VALUES('admin',?,?,?,1)",
        (phash, salt, "管理员"),
    )
    conn.commit()
    uid = cur.lastrowid
    conn.close()
    return get_user_by_id(uid)


# ---------- 会话 ----------
def create_session(user_id):
    token = secrets.token_urlsafe(32)
    conn = get_conn()
    conn.execute(
        "INSERT INTO sessions(token, user_id, expires_at) "
        "VALUES(?,?,datetime('now','localtime','+%d hours'))" % SESSION_HOURS,
        (token, user_id),
    )
    conn.commit()
    conn.close()
    return token


def _session_needs_renew(expires_at) -> bool:
    """expires_at 已经比「现在 + SESSION_HOURS」少了 SKEW 分钟以上才值得重写。"""
    if not expires_at:
        return True
    try:
        exp = datetime.datetime.strptime(str(expires_at), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return True   # 格式不认识就按老行为续期，宁可多写一次也别让人莫名下线
    full = datetime.datetime.now() + datetime.timedelta(hours=SESSION_HOURS)
    return exp < full - datetime.timedelta(minutes=SESSION_RENEW_SKEW_MINUTES)


def get_session_user(token):
    """返回 session 对应 user（含过期检查 + 滑动刷新），无效/过期返回 None"""
    if not token:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT u.*, s.expires_at AS _session_expires_at "
        "FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token=? AND (s.expires_at IS NULL OR s.expires_at > datetime('now','localtime'))",
        (token,),
    ).fetchone()
    if not row:
        conn.close()
        return None
    user = dict(row)
    expires_at = user.pop("_session_expires_at", None)   # 只是内部用，不外泄给调用方
    # 滑动过期：有活动就顺延过期时间。但没必要每个请求都写 —— 见 SESSION_RENEW_SKEW_MINUTES。
    # 语义从「3 小时无活动下线」变成「3 小时无活动下线，精度 ±5 分钟」，用户无感。
    if _session_needs_renew(expires_at):
        conn.execute(
            "UPDATE sessions SET expires_at=datetime('now','localtime','+%d hours') WHERE token=?"
            % SESSION_HOURS,
            (token,),
        )
        conn.commit()
    conn.close()
    return user


def delete_session(token):
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()


# ---------- 归属校验辅助 ----------
def get_trip_owner_id(trip_id):
    conn = get_conn()
    row = conn.execute("SELECT user_id FROM trips WHERE id=?", (trip_id,)).fetchone()
    conn.close()
    return row["user_id"] if row else None


def get_point_owner_id(point_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT t.user_id FROM points p JOIN trips t ON t.id=p.trip_id WHERE p.id=?",
        (point_id,),
    ).fetchone()
    conn.close()
    return row["user_id"] if row else None


def get_media_trip_id(media_id):
    """媒体所属的行程 id（不存在返回 None）。

    分享页下载要用：只有「这条媒体确实属于该分享链接的行程」才放行，
    否则拿到任意一个 token 就能把别人行程的媒体一并下走。
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT p.trip_id FROM media m JOIN points p ON p.id=m.point_id WHERE m.id=?",
        (media_id,),
    ).fetchone()
    conn.close()
    return row["trip_id"] if row else None


def get_media_owner_id(media_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT t.user_id FROM media m JOIN points p ON p.id=m.point_id "
        "JOIN trips t ON t.id=p.trip_id WHERE m.id=?",
        (media_id,),
    ).fetchone()
    conn.close()
    return row["user_id"] if row else None


# ---------- 行程 ----------
def list_trips(user):
    conn = get_conn()
    if user["is_admin"]:
        rows = conn.execute(
            "SELECT * FROM trips ORDER BY COALESCE(start_date, created_at) DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trips WHERE user_id=? "
            "ORDER BY COALESCE(start_date, created_at) DESC",
            (user["id"],),
        ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def get_trip(trip_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_trip(user, name, start_date, end_date, description="", status="planning"):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO trips(user_id, name, start_date, end_date, description, status) "
        "VALUES(?,?,?,?,?,?)",
        (user["id"], name, start_date, end_date, description, status),
    )
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


# 字段级更新里「不改」和「改成 NULL」是两件事，但都想用 None 表达。
# 拿一个哨兵把后者分出来：val is None → 跳过；val is CLEAR → 写 NULL。
CLEAR = object()


def update_trip(trip_id, name=None, start_date=None, end_date=None,
                description=None, status=None):
    """字段级更新行程；None 字段跳过（支持只改 status 的状态机操作），
    CLEAR 字段写 NULL/空串 —— 跟 update_point 一个语义。"""
    sets, args = [], []
    for col, val in (("name", name), ("start_date", start_date), ("end_date", end_date),
                     ("description", description), ("status", status)):
        if val is not None:
            sets.append(f"{col}=?")
            args.append(("" if col == "description" else None) if val is CLEAR else val)
    if not sets:
        return
    conn = get_conn()
    conn.execute(f"UPDATE trips SET {', '.join(sets)} WHERE id=?", (*args, trip_id))
    conn.commit()
    conn.close()


# ---------- AI 生成内容落库（P1-2）----------
def save_generation(trip_id, kind, text, tokens_used, style="旅行者视角", language="zh"):
    """落库一次 AI 生成记录；返回记录 id"""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO generations(trip_id, kind, style, language, text, tokens_used) "
        "VALUES(?,?,?,?,?,?)",
        (trip_id, kind, style, language, text, tokens_used),
    )
    conn.commit()
    gid = cur.lastrowid
    conn.close()
    return gid


def list_generations(trip_id, kind=None, limit=20):
    """查询某行程的生成记录（按时间倒序，可选按 kind 过滤）"""
    conn = get_conn()
    if kind:
        rows = conn.execute(
            "SELECT * FROM generations WHERE trip_id=? AND kind=? "
            "ORDER BY id DESC LIMIT ?",
            (trip_id, kind, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM generations WHERE trip_id=? ORDER BY id DESC LIMIT ?",
            (trip_id, limit),
        ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def delete_trip(trip_id):
    conn = get_conn()
    conn.execute("DELETE FROM trips WHERE id=?", (trip_id,))
    # 清掉指向该行程的「上次打开」记录，避免登录时恢复到已删除的行程
    conn.execute("UPDATE users SET last_trip_id=NULL WHERE last_trip_id=?", (trip_id,))
    conn.commit()
    conn.close()


def get_trip_media_files(trip_id):
    """返回某行程下所有媒体的物理文件信息（filename + thumb_filename），用于级联删除"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT m.filename, m.thumb_filename FROM media m "
        "JOIN points p ON m.point_id = p.id WHERE p.trip_id = ?",
        (trip_id,),
    ).fetchall()
    conn.close()
    return [(r["filename"], r["thumb_filename"]) for r in rows]


def list_trip_media(trip_id):
    """行程内全部媒体（含所属点信息），按 EXIF/上传时间排序（P2-3 媒体底座）"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT m.id, m.point_id, m.type, m.filename, m.thumb_filename, "
        "m.exif_time, m.exif_lat, m.exif_lng, m.created_at, m.caption, "
        "p.name AS point_name, p.start_date AS point_start, p.end_date AS point_end "
        "FROM media m JOIN points p ON m.point_id = p.id "
        "WHERE p.trip_id = ? "
        "ORDER BY COALESCE(m.exif_time, m.created_at) ASC, m.id ASC",
        (trip_id,),
    ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def search_trip_media(trip_id, q=None, mtype=None, date_from=None, date_to=None):
    """行程内多媒体检索：关键字(文件名/地点名/描述/caption) / 类型 / EXIF 时间范围（P2-3）"""
    conn = get_conn()
    sql = (
        "SELECT m.id, m.point_id, m.type, m.filename, m.thumb_filename, "
        "m.exif_time, m.exif_lat, m.exif_lng, m.caption, "
        "p.name AS point_name, p.description AS point_desc, "
        "p.start_date AS point_start, p.end_date AS point_end "
        "FROM media m JOIN points p ON m.point_id = p.id "
        "WHERE p.trip_id = ?"
    )
    args = [trip_id]
    if mtype:
        sql += " AND m.type = ?"
        args.append(mtype)
    if date_from:
        sql += " AND COALESCE(m.exif_time, m.created_at) >= ?"
        args.append(date_from)
    if date_to:
        if len(date_to) == 10:
            # 只给了日期（YYYY-MM-DD）时按「整天」算。不能拼成 "<date> 23:59:59"：
            # exif_time 是 ISO 的 "<date>T08:00:00"，而 created_at 是 "<date> 08:00:00"，
            # 两种分隔符混存，字符串比较下 'T'(0x54) > ' '(0x20)，当天的照片会被自己的
            # 截止日期滤掉。改成只比日期前缀，对两种格式都正确。
            sql += " AND substr(COALESCE(m.exif_time, m.created_at), 1, 10) <= ?"
        else:
            sql += " AND COALESCE(m.exif_time, m.created_at) <= ?"
        args.append(date_to)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    result = rows_to_dicts(rows)
    if q:
        ql = q.lower()
        result = [r for r in result
                  if ql in (r["filename"] or "").lower()
                  or ql in (r["point_name"] or "").lower()
                  or ql in (r["point_desc"] or "").lower()
                  or ql in (r["caption"] or "").lower()]
    return result


# ---------- 点 ----------
def list_points(trip_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM points WHERE trip_id=? "
        "ORDER BY start_date ASC, sort_order ASC, id ASC",
        (trip_id,),
    ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def set_point_elevation(point_id, elevation):
    """写入某个点的海拔（米）。elevation 为 None 表示查不到，也写进去避免反复重查——
    这里统一存成 -32768 这种哨兵值不够直观，所以查不到就保持 NULL，由调用方控制重试节奏。"""
    conn = get_conn()
    conn.execute("UPDATE points SET elevation=? WHERE id=?", (elevation, point_id))
    conn.commit()
    conn.close()


def list_points_without_elevation(trip_id):
    """该行程里还没有海拔数据的点（带坐标的才算）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, lat, lng FROM points "
        "WHERE trip_id=? AND elevation IS NULL AND lat IS NOT NULL AND lng IS NOT NULL",
        (trip_id,),
    ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def list_all_points(user, bbox=None):
    """用户所有行程的所有点（含坐标），供足迹热力图/全量足迹展示。

    admin 返回全库；普通用户只返回自己的点。仅取有有效坐标的点。
    bbox=(min_lng, min_lat, max_lng, max_lat) 时只取该范围内的点——
    地图只看得到一屏，聚合就该只聚合这一屏。
    """
    conn = get_conn()
    base = (
        "SELECT p.id, p.trip_id, p.name, p.lat, p.lng, p.is_waypoint, p.start_date "
        "FROM points p JOIN trips t ON t.id = p.trip_id "
        "WHERE p.lat IS NOT NULL AND p.lng IS NOT NULL"
    )
    tail, args = "", []
    if not user["is_admin"]:
        tail += " AND t.user_id=?"
        args.append(user["id"])
    clause, bargs = _bbox_clause(bbox)
    tail += clause
    args += bargs
    rows = conn.execute(base + tail + " ORDER BY p.start_date ASC, p.id ASC",
                        args).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def _footprint_scope(user):
    """足迹类查询的公共 FROM/WHERE：只要有坐标的点，且只看得到自己的。"""
    sql = ("FROM points p JOIN trips t ON t.id = p.trip_id "
           "WHERE p.lat IS NOT NULL AND p.lng IS NOT NULL")
    args = []
    if not user["is_admin"]:
        sql += " AND t.user_id=?"
        args.append(user["id"])
    return sql, args


def count_all_points(user, bbox=None) -> int:
    """该用户有坐标的点有多少个。用来决定足迹要不要走网格聚合。

    有 bbox 时只数该范围内的点——用户放大地图之后视野里可能只剩几个点，
    那就该退回精确坐标，别拿全局的密度替他做决定。
    """
    scope, args = _footprint_scope(user)
    clause, bargs = _bbox_clause(bbox)
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) " + scope + clause, args + bargs).fetchone()[0]
    conn.close()
    return n


def _bbox_clause(bbox):
    """把 (min_lng, min_lat, max_lng, max_lat) 变成一段 WHERE 和参数。"""
    if not bbox:
        return "", []
    min_lng, min_lat, max_lng, max_lat = bbox
    return (" AND p.lng BETWEEN ? AND ? AND p.lat BETWEEN ? AND ?",
            [min_lng, max_lng, min_lat, max_lat])


def aggregate_footprint(user, cell_deg: float, bbox=None):
    """把足迹点按 cell_deg 大小的经纬度方格聚合，**在 SQL 里做**。

    点数上来之后，「取出全部点再在 Python 里 Counter」这条路是两头都疼：
    内存里要装下全库的点，网络上要把原始点全发给前端。GROUP BY 之后
    出库的行数只跟屏幕上画得下多少格有关，跟总点数无关。

    格子编号用 `(lng + 180) / cell` 取整：sqlite 的 CAST 是向零截断，
    不是向下取整，负经纬度会和正的挤进同一格；先平移成非负数就没这个问题
    （floor() 要 sqlite 3.35+ 且编译时开了数学函数，不能指望）。
    坐标取格内**均值**而不是格子中心 —— 只有一个点时位置就是准的。
    返回 [{lng, lat, count}, ...]，count = 该格里的到访次数。
    """
    scope, args = _footprint_scope(user)
    clause, bargs = _bbox_clause(bbox)
    conn = get_conn()
    rows = conn.execute(
        "SELECT AVG(p.lng) AS lng, AVG(p.lat) AS lat, COUNT(*) AS count " + scope + clause +
        " GROUP BY CAST((p.lng + 180.0) / ? AS INTEGER), "
        "          CAST((p.lat + 90.0) / ? AS INTEGER)",
        args + bargs + [cell_deg, cell_deg],
    ).fetchall()
    conn.close()
    return [{"lng": round(r["lng"], 6), "lat": round(r["lat"], 6),
             "count": r["count"]} for r in rows]


def list_photo_points(user, trip_id=None):
    """含媒体的点（坐标 + 该点媒体缩略图列表），供地图照片堆图层使用。

    trip_id 给了就只取这一个行程——地图上正在看哪个行程，就只该看到哪个行程的照片。
    不给则回落到全量（admin 全库、普通用户只看自己的）。仅取有坐标且有缩略图的点。
    照片和视频都算：视频用首帧缩略图，前端在角上打 ▶ 标识。
    返回 [{point_id, name, lng, lat, photos: [{media_id, thumb, type}]}, ...]
    """
    conn = get_conn()
    base = (
        "SELECT p.id AS pid, p.name, p.lat, p.lng, m.id AS mid, m.thumb_filename, m.type AS mtype "
        "FROM points p JOIN media m ON m.point_id = p.id "
        "JOIN trips t ON t.id = p.trip_id "
        "WHERE m.thumb_filename IS NOT NULL AND m.thumb_filename != '' "
        "AND p.lat IS NOT NULL AND p.lng IS NOT NULL"
    )
    tail = " ORDER BY p.id ASC, COALESCE(m.exif_time, m.created_at) ASC, m.id ASC"
    where, args = "", []
    if trip_id is not None:
        where += " AND p.trip_id=?"
        args.append(trip_id)
    if not user["is_admin"]:
        where += " AND t.user_id=?"
        args.append(user["id"])
    rows = conn.execute(base + where + tail, args).fetchall()
    conn.close()

    from collections import defaultdict
    grouped = defaultdict(list)
    for r in rows:
        grouped[r["pid"]].append(r)

    result = []
    for pid, ms in grouped.items():
        first = ms[0]
        result.append({
            "point_id": pid,
            "name": first["name"],
            "lng": first["lng"],
            "lat": first["lat"],
            "photos": [{"media_id": m["mid"], "thumb": m["thumb_filename"],
                        "type": m["mtype"]} for m in ms],
        })
    return result


def list_searchable_points(user):
    """用户所有行程的所有点（含名称/描述/所属行程名），供记忆语义检索召回。

    admin 返回全库；普通用户只返回自己的点。仅取有名称的点。
    """
    conn = get_conn()
    base = (
        "SELECT p.id, p.trip_id, t.name AS trip_name, p.name, p.description, "
        "p.start_date, p.lat, p.lng, t.status AS trip_status "
        "FROM points p JOIN trips t ON t.id = p.trip_id "
        "WHERE p.name IS NOT NULL AND p.name != ''"
    )
    if user["is_admin"]:
        rows = conn.execute(base + " ORDER BY p.start_date DESC, p.id DESC").fetchall()
    else:
        rows = conn.execute(
            base + " AND t.user_id=? ORDER BY p.start_date DESC, p.id DESC",
            (user["id"],),
        ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def get_point_media_counts(trip_id):
    """每个点的照片/视频计数，返回 {point_id: {"photo": n, "video": n}}"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT m.point_id AS pid, m.type AS type, COUNT(*) AS n "
        "FROM media m JOIN points p ON p.id = m.point_id "
        "WHERE p.trip_id = ? GROUP BY m.point_id, m.type",
        (trip_id,),
    ).fetchall()
    conn.close()
    counts = {}
    for r in rows:
        counts.setdefault(r["pid"], {"photo": 0, "video": 0})[r["type"]] = r["n"]
    return counts


def get_point(point_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM points WHERE id=?", (point_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_point(trip_id, name, lat, lng, start_date, end_date, description="", route_mode="drive",
                 is_waypoint=0, sort_order=None):
    """建点。sort_order=None 时追加到同日期末尾；给了值就插到那个位次，
    同日期里位次 >= 它的点整体后移一格（不然两个点抢同一个名次，顺序是静默乱的）。"""
    conn = get_conn()
    if sort_order is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) AS m FROM points "
            "WHERE trip_id=? AND COALESCE(start_date,'') = COALESCE(?,'')",
            (trip_id, start_date)).fetchone()
        sort_order = int(row["m"]) + 1
    else:
        sort_order = max(0, int(sort_order))
        conn.execute(
            "UPDATE points SET sort_order = sort_order + 1 "
            "WHERE trip_id=? AND COALESCE(start_date,'') = COALESCE(?,'') AND sort_order >= ?",
            (trip_id, start_date, sort_order))
    cur = conn.execute(
        "INSERT INTO points(trip_id, name, lat, lng, start_date, end_date, description, route_mode, is_waypoint, sort_order) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (trip_id, name, lat, lng, start_date, end_date, description, route_mode, int(is_waypoint), sort_order),
    )
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid


def update_point(point_id, name=None, lat=None, lng=None, start_date=None, end_date=None,
                 description=None, route_mode=None, is_waypoint=None, sort_order=None):
    """字段级更新地点；None 字段跳过，CLEAR 字段写 NULL —— 跟 update_trip 一个语义。

    原来是全量覆盖：没传的字段一律按默认值写进去。Web UI 每次都传全量，看不出
    问题；但 agent 调 /api/ai/points/{id} 往往只想改一个字段（「把这个点改名叫
    芒康」），结果日期被抹成 NULL、描述被抹成空串。线上「芒康」那个点丢日期就是
    这么来的。想显式清空的传空串，不传就是不改。
    """
    sets, args = [], []
    for col, val in (("name", name), ("lat", lat), ("lng", lng),
                     ("start_date", start_date), ("end_date", end_date),
                     ("description", description), ("route_mode", route_mode)):
        if val is not None:
            sets.append(f"{col}=?")
            args.append(("" if col == "description" else None) if val is CLEAR else val)
    if is_waypoint is not None:
        sets.append("is_waypoint=?")
        args.append(int(is_waypoint))
    if sort_order is not None:
        sets.append("sort_order=?")
        args.append(max(0, int(sort_order)))
    if not sets:
        return
    conn = get_conn()
    conn.execute(f"UPDATE points SET {', '.join(sets)} WHERE id=?", (*args, point_id))
    conn.commit()
    conn.close()


def clear_routes_around(point_id):
    """把这个点牵连的两段定线作废：它自己那段，和前一个点到它的那段。

    点一挪窝，存下来的路线两头就对不上了 —— 线还连在旧坐标上。清掉之后
    下次加载会重新规划。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT trip_id, start_date, sort_order, id FROM points WHERE id=?",
            (point_id,)).fetchone()
        if not row:
            return
        # 前一个点按列表里的同一套排序取：start_date, sort_order, id
        prev = conn.execute(
            "SELECT id FROM points WHERE trip_id=? AND ("
            "  COALESCE(start_date,'') < COALESCE(?,'')"
            "  OR (COALESCE(start_date,'') = COALESCE(?,'') AND sort_order < ?)"
            "  OR (COALESCE(start_date,'') = COALESCE(?,'') AND sort_order = ? AND id < ?)"
            ") ORDER BY start_date DESC, sort_order DESC, id DESC LIMIT 1",
            (row["trip_id"], row["start_date"], row["start_date"], row["sort_order"],
             row["start_date"], row["sort_order"], row["id"])).fetchone()
        ids = [point_id] + ([prev["id"]] if prev else [])
        conn.execute("UPDATE points SET route_json=NULL WHERE id IN (%s)"
                     % ",".join("?" * len(ids)), ids)
        conn.commit()


def set_point_route(point_id, route_json):
    """存/清这一点到下一点定下来的路线。传 None 表示清掉，回到每次现规划。"""
    conn = get_conn()
    conn.execute("UPDATE points SET route_json=? WHERE id=?", (route_json, point_id))
    conn.commit()
    conn.close()


def set_point_route_mode(point_id, route_mode):
    """仅更新某点「到下一点」的连线方式（drive/line）。"""
    conn = get_conn()
    conn.execute("UPDATE points SET route_mode=? WHERE id=?", (route_mode, point_id))
    conn.commit()
    conn.close()


def reorder_points(trip_id, ordered_ids):
    """按传入的完整点序重排同一天内的 sort_order。

    ordered_ids 是该行程下全部点的最终期望顺序（含所有日期）。
    按 start_date 分组后，组内顺序即 sort_order（0,1,2...）；跨天顺序不受影响
    （仍由 start_date 决定）。返回是否成功（id 全部属于该 trip 且无重复）。
    """
    if not ordered_ids:
        return False
    # 重复 id 必须拒绝：下面拿到的 rows 是去重后的集合，用 len(rows) 和 len(set(ids))
    # 比较等于两边都去了重，[a, a] 会被放过。重复项在分组赋 sort_order 时会让同一个
    # 点占两个名次，结果是静默的错序，所以在进库前就挡掉。
    if len(ordered_ids) != len(set(ordered_ids)):
        return False
    conn = get_conn()
    placeholders = ",".join("?" * len(ordered_ids))
    rows = conn.execute(
        f"SELECT id, start_date FROM points WHERE trip_id=? AND id IN ({placeholders})",
        (trip_id, *ordered_ids),
    ).fetchall()
    if len(rows) != len(ordered_ids):
        conn.close()
        return False
    date_map = {r["id"]: r["start_date"] for r in rows}

    # 按日期分组，组内按传入顺序赋 sort_order
    from collections import OrderedDict
    groups = OrderedDict()
    for pid in ordered_ids:
        if pid not in date_map:
            conn.close()
            return False
        groups.setdefault(date_map[pid], []).append(pid)
    for day, ids in groups.items():
        for idx, pid in enumerate(ids):
            conn.execute("UPDATE points SET sort_order=? WHERE id=?", (idx, pid))
    conn.commit()
    conn.close()
    return True


def delete_point(point_id):
    conn = get_conn()
    conn.execute("DELETE FROM points WHERE id=?", (point_id,))
    conn.commit()
    conn.close()


def get_point_media_files(point_id):
    """返回某点下所有媒体的物理文件信息（filename + thumb_filename）"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT filename, thumb_filename FROM media WHERE point_id = ?",
        (point_id,),
    ).fetchall()
    conn.close()
    return [(r["filename"], r["thumb_filename"]) for r in rows]


# ---------- 分享 ----------
def create_share(trip_id, expire_days=None, password=None):
    """单链接模式：一个行程最多一个活跃分享链接。
    已有活跃链接则复用其 token 并更新过期时间/密码（旧配置被覆盖），否则新建。
    expire_days=None 表示永久有效；password 可选（None=无密码）。"""
    salt = phash = None
    if password:
        salt, phash = hash_password(password)
    conn = get_conn()
    row = conn.execute(
        "SELECT token FROM shares WHERE trip_id=? "
        "AND (expires_at IS NULL OR expires_at > datetime('now','localtime')) "
        "ORDER BY created_at DESC LIMIT 1",
        (trip_id,),
    ).fetchone()
    if row:
        token = row["token"]
        # 复用：删除该 trip 其余活跃链接，保持「单链接」
        conn.execute(
            "DELETE FROM shares WHERE trip_id=? AND token!=? "
            "AND (expires_at IS NULL OR expires_at > datetime('now','localtime'))",
            (trip_id, token),
        )
        if expire_days:
            conn.execute(
                "UPDATE shares SET password_hash=?, salt=?, "
                "expires_at=datetime('now','localtime', ?) WHERE token=?",
                (phash, salt, f"+{int(expire_days)} days", token),
            )
        else:
            conn.execute(
                "UPDATE shares SET password_hash=?, salt=?, expires_at=NULL WHERE token=?",
                (phash, salt, token),
            )
    else:
        token = secrets.token_urlsafe(24)
        if expire_days:
            conn.execute(
                "INSERT INTO shares(token, trip_id, password_hash, salt, expires_at) "
                "VALUES(?,?,?,?, datetime('now','localtime', ?))",
                (token, trip_id, phash, salt, f"+{int(expire_days)} days"),
            )
        else:
            conn.execute(
                "INSERT INTO shares(token, trip_id, password_hash, salt) VALUES(?,?,?,?)",
                (token, trip_id, phash, salt),
            )
    conn.commit()
    conn.close()
    return token


def get_share(token):
    """按 token 取分享记录（含 trip_id）；不存在或已过期返回 None（过期则顺带清理）"""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM shares WHERE token=? "
        "AND (expires_at IS NULL OR expires_at > datetime('now','localtime'))",
        (token,),
    ).fetchone()
    if not row:
        conn.execute(
            "DELETE FROM shares WHERE token=? AND expires_at IS NOT NULL "
            "AND expires_at <= datetime('now','localtime')",
            (token,),
        )
        conn.commit()
        conn.close()
        return None
    conn.close()
    d = dict(row)
    d["has_password"] = bool(d.get("password_hash"))
    return d


def verify_share_password(token, password):
    """校验分享密码；分享无密码时恒为 True（用于统一放行逻辑）"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM shares WHERE token=?", (token,)).fetchone()
    conn.close()
    if not row:
        return False
    if not row["password_hash"]:
        return True
    if not password:
        return False
    return verify_password(password, row["salt"], row["password_hash"])


def delete_share(token):
    conn = get_conn()
    conn.execute("DELETE FROM shares WHERE token=?", (token,))
    conn.commit()
    conn.close()


def delete_shares_for_trip(trip_id):
    """撤销某行程的所有分享链接"""
    conn = get_conn()
    conn.execute("DELETE FROM shares WHERE trip_id=?", (trip_id,))
    conn.commit()
    conn.close()


def count_shares_for_trip(trip_id):
    """某行程当前有效（未过期）的分享链接数量"""
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM shares WHERE trip_id=? "
        "AND (expires_at IS NULL OR expires_at > datetime('now','localtime'))",
        (trip_id,),
    ).fetchone()[0]
    conn.close()
    return n


def get_trip_stats(trip_id):
    """行程统计：点数、媒体数、起止日期、停留总天数"""
    conn = get_conn()
    trip = conn.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
    if not trip:
        conn.close()
        return None
    point_count = conn.execute(
        "SELECT COUNT(*) FROM points WHERE trip_id=?", (trip_id,)).fetchone()[0]
    media_count = conn.execute(
        "SELECT COUNT(*) FROM media m JOIN points p ON m.point_id=p.id "
        "WHERE p.trip_id=?", (trip_id,)).fetchone()[0]
    photo_count = conn.execute(
        "SELECT COUNT(*) FROM media m JOIN points p ON m.point_id=p.id "
        "WHERE p.trip_id=? AND m.type='photo'", (trip_id,)).fetchone()[0]
    video_count = conn.execute(
        "SELECT COUNT(*) FROM media m JOIN points p ON m.point_id=p.id "
        "WHERE p.trip_id=? AND m.type='video'", (trip_id,)).fetchone()[0]
    # 轨迹点起止日期（trip 本身无日期时，用轨迹点日期兜底展示）
    date_row = conn.execute(
        "SELECT MIN(start_date) AS min_d, "
        "MAX(COALESCE(NULLIF(end_date,''), start_date)) AS max_d "
        "FROM points WHERE trip_id=? AND start_date IS NOT NULL AND start_date != ''",
        (trip_id,)).fetchone()
    conn.close()
    return {
        "trip_id": trip_id,
        "name": trip["name"],
        "description": trip["description"],
        "start_date": trip["start_date"],
        "end_date": trip["end_date"],
        "point_start_date": (date_row["min_d"] if date_row else None),
        "point_end_date": (date_row["max_d"] if date_row else None),
        "point_count": point_count,
        "media_count": media_count,
        "photo_count": photo_count,
        "video_count": video_count,
    }


# ---------- 媒体 ----------
def list_trip_media(trip_id, limit=None, offset=0):
    """整个行程的媒体，按「点顺序 + 点内顺序」排。

    带 limit/offset 是为了分页 —— 一条行程上千张照片是正常量级，一次全吐出去
    既慢又容易把 Agent 的上下文撑爆。带上点名称/日期，Agent 拿到就知道哪张是哪儿。
    """
    conn = get_conn()
    sql = (
        "SELECT m.*, p.name AS point_name, p.start_date AS point_date "
        "FROM media m JOIN points p ON p.id = m.point_id "
        "WHERE p.trip_id=? "
        "ORDER BY p.start_date ASC, p.sort_order ASC, p.id ASC, "
        "         COALESCE(NULLIF(m.sort_order,0), 999999) ASC, "
        "         COALESCE(m.exif_time, m.created_at) ASC, m.id ASC"
    )
    params = [trip_id]
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def count_trip_media(trip_id):
    """行程媒体总数。分页接口要拿它算总页数，别让 Agent 靠翻到空页才停。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM media m JOIN points p ON p.id = m.point_id "
        "WHERE p.trip_id=?", (trip_id,)
    ).fetchone()
    conn.close()
    return row["n"] if row else 0


def list_media(point_id):
    conn = get_conn()
    # 手工排过序的（sort_order > 0）排在前面并按该顺序；没排过的仍按拍摄时间。
    # 用 COALESCE(NULLIF(sort_order,0), 999999) 让 0 沉底，避免新上传的插到已排序的中间。
    rows = conn.execute(
        "SELECT * FROM media WHERE point_id=? "
        "ORDER BY COALESCE(NULLIF(sort_order, 0), 999999) ASC, "
        "         COALESCE(exif_time, created_at) ASC, id ASC",
        (point_id,),
    ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def set_media_order(point_id, ids):
    """按给定的 id 顺序重排该点的媒体。序号从 1 开始（0 保留给「没排过」）。
    只认属于这个点的 id，越权/脏数据自动忽略。"""
    conn = get_conn()
    own = {r["id"] for r in conn.execute(
        "SELECT id FROM media WHERE point_id=?", (point_id,))}
    n = 0
    for mid in ids:
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            continue
        if mid not in own:
            continue
        n += 1
        conn.execute("UPDATE media SET sort_order=? WHERE id=?", (n, mid))
    conn.commit()
    conn.close()
    return n


def get_media(media_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_media(point_id, mtype, filename, thumb_filename=None,
              exif_time=None, exif_lat=None, exif_lng=None):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO media(point_id, type, filename, thumb_filename, exif_time, exif_lat, exif_lng) "
        "VALUES(?,?,?,?,?,?,?)",
        (point_id, mtype, filename, thumb_filename, exif_time, exif_lat, exif_lng),
    )
    conn.commit()
    mid = cur.lastrowid
    conn.close()
    return mid


def set_media_caption(media_id, caption):
    """设置某个媒体的文字描述（空串按 NULL 存，便于「有没有描述」的判断统一）"""
    conn = get_conn()
    conn.execute("UPDATE media SET caption=? WHERE id=?", (caption or None, media_id))
    conn.commit()
    conn.close()


def delete_media(media_id):
    conn = get_conn()
    conn.execute("DELETE FROM media WHERE id=?", (media_id,))
    conn.commit()
    conn.close()


# ---------- 视频清晰度档位 ----------
# 档位行由后台转码线程写、由请求线程读。每个函数各自 get_conn()（连接不跨线程），
# 这是本文件一贯的约定，别把连接传进线程里。

_VARIANT_FIELDS = ("filename", "width", "height", "bitrate", "filesize", "status", "error")


def list_variants(media_id):
    """某个视频的全部档位行，按档位从高到低（1080p→480p）不保证，调用方自己排。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM media_variants WHERE media_id=? ORDER BY id ASC", (media_id,)
    ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def get_variant(media_id, label):
    conn = get_conn()
    row = conn.execute("SELECT * FROM media_variants WHERE media_id=? AND label=?",
                       (media_id, label)).fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_variant(media_id, label, **fields):
    """插入或更新一个档位行。只写传进来的字段，没传的保持原值。

    用 UNIQUE(media_id,label) 做 upsert —— 回填脚本反复跑不会产生重复行。
    """
    cols = {k: v for k, v in fields.items() if k in _VARIANT_FIELDS}
    conn = get_conn()
    conn.execute(
        "INSERT INTO media_variants(media_id, label) VALUES(?,?) "
        "ON CONFLICT(media_id, label) DO NOTHING",
        (media_id, label),
    )
    if cols:
        sets = ", ".join(f"{k}=?" for k in cols)
        conn.execute(
            f"UPDATE media_variants SET {sets}, updated_at=datetime('now','localtime') "
            "WHERE media_id=? AND label=?",
            (*cols.values(), media_id, label),
        )
    conn.commit()
    conn.close()


def set_variant_status(media_id, label, status, error=None):
    """只改状态（和失败原因）。成功时把 error 清回 NULL。"""
    conn = get_conn()
    conn.execute(
        "UPDATE media_variants SET status=?, error=?, "
        "updated_at=datetime('now','localtime') WHERE media_id=? AND label=?",
        (status, error, media_id, label),
    )
    conn.commit()
    conn.close()


def get_variants_map(media_ids):
    """批量取多个 media 的档位，返回 {media_id: [变体行, ...]}。

    一次查询搞定，避免相册页 N 张视频打 N 次库（N+1 查询）。
    """
    ids = [int(i) for i in media_ids if i is not None]
    if not ids:
        return {}
    out = {}
    conn = get_conn()
    # SQLite 默认变量上限 999，分批查
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT * FROM media_variants WHERE media_id IN ({marks}) ORDER BY id ASC",
            chunk,
        ).fetchall()
        for r in rows:
            out.setdefault(r["media_id"], []).append(dict(r))
    conn.close()
    return out


def delete_variants(media_id):
    """删掉某个视频的全部档位行，返回被删的档位文件名（调用方负责删盘上的文件）。"""
    conn = get_conn()
    names = [r["filename"] for r in conn.execute(
        "SELECT filename FROM media_variants WHERE media_id=?", (media_id,)
    ) if r["filename"]]
    conn.execute("DELETE FROM media_variants WHERE media_id=?", (media_id,))
    conn.commit()
    conn.close()
    return names


def get_point_variant_files(point_id):
    """某个点下所有媒体的档位文件名，用于级联删除时清盘。

    media_variants 有 ON DELETE CASCADE，删点时**数据库行**会自动没，
    但盘上的 mp4 不会 —— 不在删之前把文件名捞出来，档位文件就成了孤儿。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT v.filename FROM media_variants v "
        "JOIN media m ON v.media_id = m.id WHERE m.point_id = ?",
        (point_id,),
    ).fetchall()
    conn.close()
    return [r["filename"] for r in rows if r["filename"]]


def get_trip_variant_files(trip_id):
    """某个行程下所有媒体的档位文件名，用于级联删除时清盘。理由同上。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT v.filename FROM media_variants v "
        "JOIN media m ON v.media_id = m.id "
        "JOIN points p ON m.point_id = p.id WHERE p.trip_id = ?",
        (trip_id,),
    ).fetchall()
    conn.close()
    return [r["filename"] for r in rows if r["filename"]]


def list_all_videos():
    """全部视频媒体（id + filename），给回填脚本用。按 id 升序，便于断点续跑。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, filename FROM media WHERE type='video' ORDER BY id ASC"
    ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


# ---------- 断点续传会话 ----------
def find_upload_session(point_id, owner_id, fingerprint):
    """按 (点, 属主, 文件指纹) 找还在传的会话。找不到返回 None。

    只认 status='uploading' —— 已完成/已放弃的会话不复用，否则同一个文件
    第二次上传会被当成「已经传过了」直接返回旧的 media_id。
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM upload_sessions WHERE point_id=? AND owner_id IS ? "
        "AND fingerprint=? AND status='uploading' ORDER BY created_at DESC LIMIT 1",
        (point_id, owner_id, fingerprint),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_upload_session(session_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM upload_sessions WHERE session_id=?",
                       (session_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_upload_session(session_id, point_id, owner_id, fingerprint, filename,
                          filesize, chunk_size, total_chunks, mtype):
    conn = get_conn()
    conn.execute(
        "INSERT INTO upload_sessions(session_id, point_id, owner_id, fingerprint, "
        "filename, filesize, chunk_size, total_chunks, mtype) VALUES(?,?,?,?,?,?,?,?,?)",
        (session_id, point_id, owner_id, fingerprint, filename,
         filesize, chunk_size, total_chunks, mtype),
    )
    conn.commit()
    conn.close()
    return session_id


def add_upload_chunk(session_id, idx, size):
    """记一个分片已落盘。同一片重传是幂等的（主键冲突就覆盖）。

    顺手把会话的 updated_at 推后 —— 过期清理看的就是这个字段，
    只要还在传就不该被当成僵尸会话回收。
    """
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO upload_chunks(session_id, idx, size) "
                 "VALUES(?,?,?)", (session_id, idx, size))
    conn.execute("UPDATE upload_sessions SET updated_at=datetime('now','localtime') "
                 "WHERE session_id=?", (session_id,))
    conn.commit()
    conn.close()


def list_upload_chunks(session_id):
    """已收到的分片序号（升序）。"""
    conn = get_conn()
    rows = conn.execute("SELECT idx FROM upload_chunks WHERE session_id=? ORDER BY idx ASC",
                        (session_id,)).fetchall()
    conn.close()
    return [r["idx"] for r in rows]


def upload_chunks_total_size(session_id):
    """已收到分片的字节数合计 —— 用来在合并之前就挡住「谎报 filesize 然后猛传」。"""
    conn = get_conn()
    row = conn.execute("SELECT COALESCE(SUM(size), 0) AS s FROM upload_chunks "
                       "WHERE session_id=?", (session_id,)).fetchone()
    conn.close()
    return int(row["s"]) if row else 0


def finish_upload_session(session_id, media_id):
    conn = get_conn()
    conn.execute("UPDATE upload_sessions SET status='completed', media_id=?, "
                 "updated_at=datetime('now','localtime') WHERE session_id=?",
                 (media_id, session_id))
    conn.execute("DELETE FROM upload_chunks WHERE session_id=?", (session_id,))
    conn.commit()
    conn.close()


def abort_upload_session(session_id):
    conn = get_conn()
    conn.execute("UPDATE upload_sessions SET status='aborted', "
                 "updated_at=datetime('now','localtime') WHERE session_id=?",
                 (session_id,))
    conn.execute("DELETE FROM upload_chunks WHERE session_id=?", (session_id,))
    conn.commit()
    conn.close()


def create_oss_upload_session(session_id, point_id, owner_id, fingerprint, filename,
                              filesize, chunk_size, total_chunks, mtype,
                              oss_key, oss_upload_id):
    """建一条走 OSS 直传的会话。和 create_upload_session 的区别只在多存两列。"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO upload_sessions(session_id, point_id, owner_id, fingerprint, "
        "filename, filesize, chunk_size, total_chunks, mtype, oss_key, oss_upload_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, point_id, owner_id, fingerprint, filename,
         filesize, chunk_size, total_chunks, mtype, oss_key, oss_upload_id),
    )
    conn.commit()
    conn.close()
    return session_id


# ---------- OSS 回源队列 ----------

def enqueue_oss_backfill(media_id, oss_key, filesize=None, mtype=None):
    """排一条「把这个对象拉回本地」。重复排队是幂等的（主键覆盖，状态复位）。"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO oss_backfill(media_id, oss_key, filesize, mtype, status) "
        "VALUES(?,?,?,?,'pending') "
        "ON CONFLICT(media_id) DO UPDATE SET oss_key=excluded.oss_key, "
        "filesize=excluded.filesize, mtype=excluded.mtype, status='pending', "
        "error=NULL, updated_at=datetime('now','localtime')",
        (media_id, oss_key, filesize, mtype),
    )
    conn.commit()
    conn.close()


def get_oss_backfill(media_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM oss_backfill WHERE media_id=?",
                       (media_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_oss_backfill(status=None, limit=200):
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM oss_backfill WHERE status=? ORDER BY created_at LIMIT ?",
            (status, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM oss_backfill ORDER BY created_at LIMIT ?", (limit,)).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def take_oss_backfill(media_id) -> bool:
    """把一条从 pending/error 抢成 running。抢到返回 True。

    条件写在 UPDATE 的 WHERE 里，靠 SQLite 的行锁做互斥 —— 两个 worker 同时抢，
    只有一个的 rowcount 会是 1。
    """
    conn = get_conn()
    cur = conn.execute(
        "UPDATE oss_backfill SET status='running', attempts=attempts+1, "
        "updated_at=datetime('now','localtime') "
        "WHERE media_id=? AND status IN ('pending','error')", (media_id,))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n == 1


def finish_oss_backfill(media_id, ok=True, error=None):
    conn = get_conn()
    conn.execute(
        "UPDATE oss_backfill SET status=?, error=?, "
        "updated_at=datetime('now','localtime') WHERE media_id=?",
        ("done" if ok else "error", None if ok else (error or "")[:500], media_id))
    conn.commit()
    conn.close()


def reset_running_oss_backfill():
    """进程启动时把上次崩在 running 的复位回 pending —— 否则它们永远没人管。"""
    conn = get_conn()
    conn.execute("UPDATE oss_backfill SET status='pending', "
                 "updated_at=datetime('now','localtime') WHERE status='running'")
    conn.commit()
    conn.close()


def update_media_after_backfill(media_id, thumb_filename=None,
                                exif_time=None, exif_lat=None, exif_lng=None):
    """回源拿到本地文件之后补写缩略图名和 EXIF。

    只覆盖「本来就空着」的列：用户可能已经手工调过位置/时间，回源是后来才发生的，
    不能拿 EXIF 把人家改过的值顶掉。
    """
    conn = get_conn()
    conn.execute(
        "UPDATE media SET "
        "thumb_filename = COALESCE(thumb_filename, ?), "
        "exif_time      = COALESCE(exif_time, ?), "
        "exif_lat       = COALESCE(exif_lat, ?), "
        "exif_lng       = COALESCE(exif_lng, ?) "
        "WHERE id=?",
        (thumb_filename, exif_time, exif_lat, exif_lng, media_id))
    conn.commit()
    conn.close()


def set_media_thumb(media_id, thumb_filename):
    conn = get_conn()
    conn.execute("UPDATE media SET thumb_filename=? WHERE id=?",
                 (thumb_filename, media_id))
    conn.commit()
    conn.close()


def list_stale_upload_sessions(hours=24):
    """超过 hours 没动静、还挂在 uploading 的会话。给定时清理用。

    也一并带上早已 completed/aborted 但行还留着的老会话（同样按 hours 过期），
    这张表没有别的读者，留着只会无限长下去。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM upload_sessions "
        "WHERE updated_at <= datetime('now','localtime', ?)",
        (f"-{int(hours)} hours",),
    ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def delete_upload_session(session_id):
    conn = get_conn()
    conn.execute("DELETE FROM upload_chunks WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM upload_sessions WHERE session_id=?", (session_id,))
    conn.commit()
    conn.close()


def list_point_upload_sessions(point_id, owner_id):
    """某个点上「还没传完」的会话，前端据此显示未完成占位格。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM upload_sessions WHERE point_id=? AND owner_id IS ? "
        "AND status='uploading' ORDER BY created_at ASC",
        (point_id, owner_id),
    ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


# ---------- 路线缓存 ----------
def get_route_cache(cache_key):
    """读取路线缓存（OSRM 规划结果），命中返回 payload dict，否则 None。"""
    conn = get_conn()
    row = conn.execute("SELECT payload FROM route_cache WHERE cache_key=?",
                       (cache_key,)).fetchone()
    conn.close()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None


def get_route_cache_raw(cache_key):
    """读取路线缓存的**原始 JSON 字节**，命中返回 bytes，否则 None。

    给行政区划这种超大 payload 用（单行最大 3.8MB）。走 get_route_cache 的话要先
    json.loads 成 dict，再由 FastAPI json.dumps 回去，两趟全量处理都发生在事件循环
    线程上，实测能把整个进程堵住几百毫秒。直接把库里存的 JSON 文本当响应体发出去，
    这两趟就都省了。

    text_factory=bytes 是为了连 UTF-8 的 decode/encode 都省掉 —— 库里存的本来就是
    UTF-8 字节，解成 str 再编回去是纯空转。
    """
    conn = get_conn()
    conn.text_factory = bytes
    row = conn.execute("SELECT payload FROM route_cache WHERE cache_key=?",
                       (cache_key,)).fetchone()
    conn.close()
    return row[0] if row else None


def set_route_cache(cache_key, payload):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO route_cache(cache_key, payload) VALUES(?,?)",
                 (cache_key, json.dumps(payload, ensure_ascii=False)))
    conn.commit()
    conn.close()


def clear_route_cache():
    conn = get_conn()
    conn.execute("DELETE FROM route_cache")
    conn.commit()
    conn.close()


# ===== 领域 API 层：API Key =====
def create_api_key(user_id, name, scope, expire_days=None, raw_key=None):
    """创建 API Key，返回 (raw_key, key_prefix, key_id)。raw_key 仅返回一次，库里只存 hash。"""
    if raw_key is None:
        raw_key = "tm_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12]
    conn = get_conn()
    if expire_days:
        cur = conn.execute(
            "INSERT INTO api_keys(key_hash, key_prefix, name, user_id, scope, expires_at) "
            "VALUES(?,?,?,?,?,datetime('now','localtime', ?))",
            (key_hash, key_prefix, name, user_id, scope, f"+{int(expire_days)} days"),
        )
    else:
        cur = conn.execute(
            "INSERT INTO api_keys(key_hash, key_prefix, name, user_id, scope) "
            "VALUES(?,?,?,?,?)",
            (key_hash, key_prefix, name, user_id, scope),
        )
    conn.commit()
    key_id = cur.lastrowid
    conn.close()
    return raw_key, key_prefix, key_id


def get_api_key_by_hash(key_hash):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key_hash=? AND revoked=0 "
        "AND (expires_at IS NULL OR expires_at > datetime('now','localtime'))",
        (key_hash,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def touch_api_key(key_id):
    conn = get_conn()
    conn.execute("UPDATE api_keys SET last_used_at=datetime('now','localtime') WHERE id=?", (key_id,))
    conn.commit()
    conn.close()


def list_api_keys(user_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, key_prefix, name, scope, expires_at, last_used_at, revoked, created_at "
        "FROM api_keys WHERE user_id=? ORDER BY created_at DESC", (user_id,)
    ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def revoke_api_key(key_id, user_id):
    conn = get_conn()
    conn.execute("UPDATE api_keys SET revoked=1 WHERE id=? AND user_id=?", (key_id, user_id))
    conn.commit()
    conn.close()


def api_key_name_taken(user_id, name) -> bool:
    """这个名字有没有被一把**还有效**的 Key 占着。

    只挡有效的：已吊销、已过期的 Key 还留在列表里（历史要能查），但它们的名字
    该让出来 —— 不然「Cursor 接入」这种名字用一次就废一个。
    比较时去空格 + 忽略大小写，「Cursor」和「cursor 」算同一个。
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM api_keys WHERE user_id=? AND revoked=0 "
        "  AND LOWER(TRIM(name)) = LOWER(TRIM(?)) "
        "  AND (expires_at IS NULL OR expires_at > datetime('now','localtime')) "
        "LIMIT 1",
        (user_id, name),
    ).fetchone()
    conn.close()
    return row is not None


def api_key_belongs_to(key_id, user_id) -> bool:
    """这个 Key 是不是这个用户的 —— 控制台按 Key 筛日志前先验一道归属。"""
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM api_keys WHERE id=? AND user_id=?",
                       (key_id, user_id)).fetchone()
    conn.close()
    return row is not None


def delete_api_key(key_id, user_id):
    """彻底删除 API Key。audit_log 外键为 ON DELETE SET NULL，审计记录保留。"""
    conn = get_conn()
    cur = conn.execute("DELETE FROM api_keys WHERE id=? AND user_id=?", (key_id, user_id))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


# ===== 领域 API 层：审计 =====
def add_audit(api_key_id, user_id, action, method, path, scope, status,
              detail=None, tokens_used=0, latency_ms=None, client_request_id=None,
              client_ip=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO audit_log(api_key_id, user_id, action, method, path, scope, status, "
        "detail, tokens_used, latency_ms, client_request_id, client_ip) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (api_key_id, user_id, action, method, path, scope, status, detail,
         tokens_used, latency_ms, client_request_id, client_ip),
    )
    conn.commit()
    conn.close()


# ===== 领域 API 层：幂等键（P0-2） =====
def idempotency_claim(api_key_id, client_request_id, action):
    """原子抢占幂等键：首次返回 True（可执行），重复返回 False（已存在）。

    用 UNIQUE(api_key_id, client_request_id) 保证同一 key 的同一 request id 只执行一次。
    """
    conn = get_conn()
    cur = conn.execute(
        "INSERT OR IGNORE INTO idempotency_keys(api_key_id, client_request_id, action, status) "
        "VALUES(?,?,?,'pending')",
        (api_key_id, client_request_id, action),
    )
    claimed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return claimed


def idempotency_get(api_key_id, client_request_id):
    """查已处理的幂等记录。返回 dict 或 None。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM idempotency_keys WHERE api_key_id=? AND client_request_id=?",
        (api_key_id, client_request_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def idempotency_complete(api_key_id, client_request_id, result_json):
    """执行成功后：标记 completed 并缓存结果（幂等命中时原样返回）。"""
    conn = get_conn()
    conn.execute(
        "UPDATE idempotency_keys SET status='completed', result=? "
        "WHERE api_key_id=? AND client_request_id=?",
        (result_json, api_key_id, client_request_id),
    )
    conn.commit()
    conn.close()


def idempotency_discard(api_key_id, client_request_id):
    """执行失败后：删除 pending 记录，允许客户端重试。"""
    conn = get_conn()
    conn.execute(
        "DELETE FROM idempotency_keys WHERE api_key_id=? AND client_request_id=? AND status='pending'",
        (api_key_id, client_request_id),
    )
    conn.commit()
    conn.close()


def list_audit(user_id=None, limit=100, api_key_id=None):
    """审计日志。api_key_id 给控制台按 Key 筛选用；顺带带出 Key 名字/前缀，
    否则前端只拿到一个 id，展示不出「这条是哪个 Key 调的」。"""
    conn = get_conn()
    sql = ("SELECT a.*, k.name AS key_name, k.key_prefix AS key_prefix "
           "FROM audit_log a LEFT JOIN api_keys k ON k.id = a.api_key_id")
    where, params = [], []
    if user_id:
        where.append("a.user_id=?")
        params.append(user_id)
    if api_key_id is not None:
        where.append("a.api_key_id=?")
        params.append(api_key_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows_to_dicts(rows)


# ===== 领域 API 层：用户偏好画像 =====
def upsert_pref(user_id, pref_key, pref_value=None, strength_delta=0.1):
    conn = get_conn()
    conn.execute(
        "INSERT INTO user_prefs(user_id, pref_key, pref_value, strength, evidence_count) "
        "VALUES(?,?,?,?,1) "
        "ON CONFLICT(user_id, pref_key) DO UPDATE SET "
        "  pref_value=excluded.pref_value, "
        "  strength=MIN(1.0, user_prefs.strength + ?), "
        "  evidence_count=user_prefs.evidence_count + 1, "
        "  updated_at=datetime('now','localtime')",
        (user_id, pref_key, pref_value, strength_delta, strength_delta),
    )
    conn.commit()
    conn.close()


def list_prefs(user_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT pref_key, pref_value, strength, evidence_count, updated_at "
        "FROM user_prefs WHERE user_id=? ORDER BY strength DESC, evidence_count DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


# ===== N-7 点级情绪/场景元数据 =====
def list_point_meta_by_trip(trip_id):
    """取行程内所有点的情绪元数据（JOIN points 拿地点名），供 LLM 创作引用。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT pm.point_id, p.name AS point_name, pm.mood, pm.scene, "
            "pm.time_of_day, pm.season "
            "FROM point_meta pm JOIN points p ON p.id = pm.point_id "
            "WHERE p.trip_id = ? AND (pm.mood IS NOT NULL OR pm.scene IS NOT NULL)",
            (trip_id,),
        ).fetchall()
    finally:
        conn.close()
    return rows_to_dicts(rows)


def upsert_point_meta(point_id, mood=None, weather=None, scene=None,
                      time_of_day=None, season=None, meta_json=None, source="llm"):
    """写入/更新某点的情绪·天气·场景·时段·季节元数据（点唯一，增量覆盖）。

    仅非空字段参与更新（避免把已有维度冲成 NULL）。
    """
    conn = get_conn()
    conn.execute(
        "INSERT INTO point_meta(point_id, mood, weather, scene, time_of_day, "
        "season, meta_json, source) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(point_id) DO UPDATE SET "
        "  mood=COALESCE(excluded.mood, point_meta.mood), "
        "  weather=COALESCE(excluded.weather, point_meta.weather), "
        "  scene=COALESCE(excluded.scene, point_meta.scene), "
        "  time_of_day=COALESCE(excluded.time_of_day, point_meta.time_of_day), "
        "  season=COALESCE(excluded.season, point_meta.season), "
        "  meta_json=COALESCE(excluded.meta_json, point_meta.meta_json), "
        "  source=excluded.source, "
        "  updated_at=datetime('now','localtime')",
        (point_id, mood, weather, scene, time_of_day, season, meta_json, source),
    )
    conn.commit()
    conn.close()


def get_point_meta(point_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM point_meta WHERE point_id=?", (point_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_trip_meta(trip_id):
    """某行程下所有点的元数据（JOIN points 带出点名称/坐标），供感官重建查询。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT m.*, p.name AS point_name, p.lat, p.lng, p.start_date "
        "FROM point_meta m JOIN points p ON p.id = m.point_id "
        "WHERE p.trip_id=? ORDER BY p.start_date ASC, p.id ASC",
        (trip_id,),
    ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


# ===== 开放平台 / 开发者控制台：用量统计 =====
def usage_stats(user_id):
    """按 API Key 聚合用量（调用次数/成功/失败/token 消耗），供开发者控制台展示。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT k.id, k.key_prefix, k.name, k.scope, k.revoked, "
        "       k.created_at, k.last_used_at, k.expires_at, "
        "       COUNT(a.id) AS calls, "
        "       SUM(CASE WHEN a.status='ok' THEN 1 ELSE 0 END) AS ok_calls, "
        "       COALESCE(SUM(a.tokens_used), 0) AS tokens "
        "FROM api_keys k "
        "LEFT JOIN audit_log a ON a.api_key_id = k.id "
        "WHERE k.user_id=? "
        "GROUP BY k.id "
        "ORDER BY k.created_at DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows_to_dicts(rows)
