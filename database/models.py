from __future__ import annotations

import hashlib
import logging
import os
import threading
from datetime import datetime, timedelta

import psycopg2
from dotenv import load_dotenv
from psycopg2 import pool as psycopg2_pool
from psycopg2.extras import RealDictCursor
from psycopg2.extensions import connection as _PGConnection

load_dotenv()

logger = logging.getLogger(__name__)
DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASS"),
    "database": os.getenv("DB_DATABASE"),
    "port": os.getenv("DB_PORT", "5432"),
}


# =========================
# KẾT NỐI DATABASE (pool)
# =========================
_pg_pool = None
_pool_lock = threading.Lock()
_redis_client = None
_redis_init_failed = False


def _use_connection_pool() -> bool:
    return os.getenv("DB_POOL_ENABLED", "true").lower() in ("1", "true", "yes")


def _threaded_pool():
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    with _pool_lock:
        if _pg_pool is not None:
            return _pg_pool
        minc = max(1, int(os.getenv("DB_POOL_MIN", "1")))
        maxc = max(minc, int(os.getenv("DB_POOL_MAX", "48")))
        _pg_pool = psycopg2_pool.ThreadedConnectionPool(minc, maxc, **DB_CONFIG)
        logger.info("PostgreSQL pool started (min=%s max=%s)", minc, maxc)
        return _pg_pool


def get_conn():
    if not _use_connection_pool():
        return psycopg2.connect(**DB_CONFIG)
    return _threaded_pool().getconn()


def _raw_close(conn):
    if conn is None:
        return
    try:
        _PGConnection.close(conn)
    except Exception:
        pass


def put_conn(conn):
    """Trả kết nối về pool (rollback trước để tránh transaction aborted kế thừa)."""
    if conn is None:
        return
    if not _use_connection_pool():
        _raw_close(conn)
        return
    try:
        try:
            conn.rollback()
        except Exception:
            pass
        _threaded_pool().putconn(conn)
    except Exception as e:
        logger.warning("put_conn failed, closing connection: %s", e)
        _raw_close(conn)


def telegram_bot_dedupe_key() -> str:
    """Khóa ổn định theo token bot nhóm (tránh trùng khi nhiều instance)."""
    t = (os.getenv("KETOAN_TOKEN") or "").encode()
    return hashlib.sha256(t).hexdigest()[:32]


def _redis_idem_client():
    """Redis tùy chọn cho idempotency (nhanh). Không có REDIS_URL → chỉ dùng PostgreSQL."""
    global _redis_client, _redis_init_failed
    if _redis_init_failed:
        return None
    url = (os.getenv("REDIS_URL") or "").strip()
    if not url:
        return None
    if _redis_client is not None:
        return _redis_client
    try:
        import redis as redis_lib

        c = redis_lib.Redis.from_url(url, decode_responses=True, socket_connect_timeout=2.0)
        c.ping()
        _redis_client = c
        logger.info("Redis: idempotency enabled (SET NX)")
        return _redis_client
    except Exception as e:
        logger.warning("Redis unavailable, idempotency uses PostgreSQL only: %s", e)
        _redis_init_failed = True
        return None


def telegram_update_try_claim(bot_key: str, update_id: int | None) -> tuple[bool, str | None]:
    """
    Chống xử lý trùng cùng một Telegram update_id (retry / lag).
    Trả về (tiếp_tục_xử_lý, storage): storage 'redis'|'pg'|None — dùng cho release nếu insert lỗi.
    """
    if update_id is None:
        return True, None
    uid = int(update_id)
    r = _redis_idem_client()
    if r is not None:
        key = f"bothuchi:idem:{bot_key}:{uid}"
        try:
            ttl = int(os.getenv("REDIS_IDEM_TTL_SEC", "172800"))
            if r.set(key, "1", nx=True, ex=ttl):
                return True, "redis"
            return False, None
        except Exception as e:
            logger.warning("redis idem set failed, falling back to PG: %s", e)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO processed_telegram_updates (bot_key, update_id)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            (bot_key, uid),
        )
        conn.commit()
        return (cur.rowcount == 1), ("pg" if cur.rowcount == 1 else None)
    except Exception as e:
        logger.warning("telegram_update_try_claim PG failed (fail-open): %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return True, None
    finally:
        put_conn(conn)


def telegram_update_release_claim(bot_key: str, update_id: int | None, storage: str | None) -> None:
    """Khi INSERT giao dịch lỗi sau khi đã claim — bỏ claim để Telegram retry được xử lý lại."""
    if update_id is None or not storage:
        return
    uid = int(update_id)
    if storage == "redis":
        r = _redis_idem_client()
        if r is not None:
            try:
                r.delete(f"bothuchi:idem:{bot_key}:{uid}")
            except Exception as e:
                logger.warning("redis idem release failed: %s", e)
        return
    if storage == "pg":
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM processed_telegram_updates WHERE bot_key = %s AND update_id = %s",
                (bot_key, uid),
            )
            conn.commit()
        except Exception as e:
            logger.warning("pg idem release failed: %s", e)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            put_conn(conn)


def purge_processed_telegram_updates_older_than_days(days: int = 14) -> int:
    """Dọn bảng idempotency (mặc định 14 ngày). Trả về số dòng đã xóa."""
    if days <= 0:
        return 0
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM processed_telegram_updates
            WHERE created_at < NOW() - (%s * INTERVAL '1 day')
            """,
            (int(days),),
        )
        n = cur.rowcount
        conn.commit()
        return n
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        put_conn(conn)


# =========================
# KHỞI TẠO CÁC BẢNG
# =========================
# Hai process (vd. run_both_bots) không được chạy DDL init cùng lúc — tránh
# InternalError: tuple concurrently updated trên catalog PostgreSQL.
_INIT_DB_ADVISORY_KEY = 4827364191


def _run_init_db_migrations(cur):
    # === Users ===
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(255) UNIQUE
        )
    """)
    cur.execute(
        'ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(16) NOT NULL DEFAULT \'user\''
    )

    # === Admins ===
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id SERIAL PRIMARY KEY,
            username VARCHAR(255) UNIQUE
        )
    """)

    # === Sessions ===
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            chat_id BIGINT NOT NULL,
            chiet_khau DOUBLE PRECISION DEFAULT 0,
            chiet_khau_vao DOUBLE PRECISION DEFAULT 0,
            chiet_khau_ra DOUBLE PRECISION DEFAULT 0,
            ti_gia INTEGER DEFAULT 1,
            ti_gia_xuat INTEGER DEFAULT 1,
            tong_vao DOUBLE PRECISION DEFAULT 0,
            tong_ra DOUBLE PRECISION DEFAULT 0,
            doanh_thu DOUBLE PRECISION DEFAULT 0,
            close_at TIMESTAMP NULL DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    
    # Create trigger for updated_at (PostgreSQL doesn't support ON UPDATE CURRENT_TIMESTAMP)
    cur.execute("""
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = CURRENT_TIMESTAMP;
            RETURN NEW;
        END;
        $$ language 'plpgsql';
    """)
    
    cur.execute("""
        DROP TRIGGER IF EXISTS update_sessions_updated_at ON sessions;
        CREATE TRIGGER update_sessions_updated_at
        BEFORE UPDATE ON sessions
        FOR EACH ROW
        EXECUTE FUNCTION update_updated_at_column();
    """)
    
    # Migration: Alter columns if they are still INTEGER (for existing tables)
    try:
        cur.execute("ALTER TABLE sessions ALTER COLUMN chiet_khau TYPE DOUBLE PRECISION")
        cur.execute("ALTER TABLE sessions ALTER COLUMN tong_vao TYPE DOUBLE PRECISION")
        cur.execute("ALTER TABLE sessions ALTER COLUMN tong_ra TYPE DOUBLE PRECISION")
        cur.execute("ALTER TABLE sessions ALTER COLUMN doanh_thu TYPE DOUBLE PRECISION")
        print("✅ Migrated sessions table columns to DOUBLE PRECISION")
    except Exception as e:
        # Column might already be DOUBLE PRECISION or table doesn't exist yet, ignore
        pass

    # Migration: add new discount columns for in/out flow
    cur.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS chiet_khau_vao DOUBLE PRECISION DEFAULT 0")
    cur.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS chiet_khau_ra DOUBLE PRECISION DEFAULT 0")
    cur.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS ti_gia_xuat INTEGER DEFAULT 1")
    cur.execute('ALTER TABLE sessions ALTER COLUMN "ti_gia" SET DEFAULT 1')
    cur.execute('ALTER TABLE sessions ALTER COLUMN "ti_gia_xuat" SET DEFAULT 1')

    # === Transactions ===
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            type VARCHAR(10) NOT NULL CHECK (type IN ('income', 'expense')),
            currency VARCHAR(10) NOT NULL DEFAULT 'vnd' CHECK (currency IN ('vnd', 'usdt')),
            ti_gia INTEGER DEFAULT 1,
            ti_gia_xuat INTEGER DEFAULT 1,
            chiet_khau_vao DOUBLE PRECISION,
            chiet_khau_ra DOUBLE PRECISION,
            amount DOUBLE PRECISION NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(session_id) REFERENCES sessions(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    # Migration cho bảng transactions cũ chưa có cột currency
    cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS currency VARCHAR(10) NOT NULL DEFAULT 'vnd'")
    cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS ti_gia INTEGER DEFAULT 1")
    cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS ti_gia_xuat INTEGER DEFAULT 1")
    cur.execute('ALTER TABLE transactions ALTER COLUMN "ti_gia" SET DEFAULT 1')
    cur.execute('ALTER TABLE transactions ALTER COLUMN "ti_gia_xuat" SET DEFAULT 1')
    cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS chiet_khau_vao DOUBLE PRECISION")
    cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS chiet_khau_ra DOUBLE PRECISION")

    # Index để phiên nhiều giao dịch vẫn nhanh
    cur.execute('CREATE INDEX IF NOT EXISTS idx_transactions_session_created ON "transactions" ("session_id", "created_at")')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_sessions_close_at ON "sessions" ("close_at") WHERE "close_at" IS NOT NULL')

    # Một phiên theo ngày (theo dữ liệu ngày — phục hồi sau khi bot restart)
    cur.execute('ALTER TABLE sessions ADD COLUMN IF NOT EXISTS business_date DATE')
    try:
        cur.execute(
            """
            UPDATE sessions
            SET business_date = (created_at::timestamp)::date
            WHERE business_date IS NULL AND created_at IS NOT NULL
            """
        )
    except Exception:
        pass

    cur.execute(
        'CREATE INDEX IF NOT EXISTS idx_sessions_chat_business_date ON "sessions" ("chat_id", "business_date")'
    )

    # Nhóm logic (admin) + chat Telegram gắn nhóm — lưu DB, không mất khi bot restart
    cur.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id SERIAL PRIMARY KEY,
            admin_user_id INTEGER NOT NULL REFERENCES users(id),
            name VARCHAR(255) NOT NULL,
            chiet_khau_vao DOUBLE PRECISION DEFAULT 0,
            chiet_khau_ra DOUBLE PRECISION DEFAULT 0,
            ti_gia_mua INTEGER DEFAULT 1,
            ti_gia_ban INTEGER DEFAULT 1,
            status VARCHAR(20) NOT NULL DEFAULT 'closed',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_group_context (
            chat_id BIGINT PRIMARY KEY,
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        DROP TRIGGER IF EXISTS update_groups_updated_at ON groups;
        CREATE TRIGGER update_groups_updated_at
        BEFORE UPDATE ON groups
        FOR EACH ROW
        EXECUTE FUNCTION update_updated_at_column();
    """)

    # Tổng kết theo ngày (bot tổng kế toán) — giá U set, hiện tại U, thực tế U (cột riêng), lợi nhuận, số nhóm; tong_ra cột cũ
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tongket (
            ngay DATE PRIMARY KEY,
            gia_u_set DOUBLE PRECISION NOT NULL,
            tong_vao DOUBLE PRECISION NOT NULL,
            tong_ra DOUBLE PRECISION NOT NULL,
            loi_nhuan DOUBLE PRECISION NOT NULL,
            so_nhom_tham_gia INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        DROP TRIGGER IF EXISTS update_tongket_updated_at ON tongket;
        CREATE TRIGGER update_tongket_updated_at
        BEFORE UPDATE ON tongket
        FOR EACH ROW
        EXECUTE FUNCTION update_updated_at_column();
    """)
    cur.execute(
        "ALTER TABLE tongket ADD COLUMN IF NOT EXISTS tong_vao_thuc_te_u DOUBLE PRECISION"
    )
    cur.execute(
        "ALTER TABLE tongket ADD COLUMN IF NOT EXISTS loi_nhuan_chiet_khau_vnd DOUBLE PRECISION"
    )

    # Idempotency Telegram (update_id): tránh ghi đôi khi retry; Redis tùy chọn, mặc định PG.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_telegram_updates (
            bot_key VARCHAR(32) NOT NULL,
            update_id BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (bot_key, update_id)
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_processed_telegram_updates_created "
        "ON processed_telegram_updates (created_at)"
    )


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT pg_advisory_lock(%s)", (_INIT_DB_ADVISORY_KEY,))
    try:
        _run_init_db_migrations(cur)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_INIT_DB_ADVISORY_KEY,))
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        try:
            cur.close()
        except Exception:
            pass
        try:
            put_conn(conn)
        except Exception:
            pass


def purge_closed_sessions_older_than_days(days: int = 3):
    """
    Xóa transactions rồi sessions đã đóng lâu hơn `days` ngày.
    Trả về (số_session_xóa, số_transaction_xóa).
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        cutoff = datetime.now() - timedelta(days=days)
        cur.execute(
            '''
            DELETE FROM "transactions"
            WHERE "session_id" IN (
                SELECT id FROM "sessions"
                WHERE "close_at" IS NOT NULL AND "close_at" < %s
            )
            ''',
            (cutoff,),
        )
        n_tx = cur.rowcount
        cur.execute(
            '''
            DELETE FROM "sessions"
            WHERE "close_at" IS NOT NULL AND "close_at" < %s
            ''',
            (cutoff,),
        )
        n_sess = cur.rowcount
        conn.commit()
        return n_sess, n_tx
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            cur.close()
        except Exception:
            pass
        put_conn(conn)


def fetch_usernames_by_ids(user_ids):
    """Một query cho nhiều user_id → {id: username}."""
    ids = []
    for i in user_ids:
        if i is None:
            continue
        try:
            ids.append(int(i))
        except (TypeError, ValueError):
            continue
    ids = list(set(ids))
    if not ids:
        return {}
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT id, username FROM "users" WHERE id = ANY(%s)', (ids,))
        rows = cur.fetchall()
        return {int(r["id"]): (r.get("username") or "Unknown") for r in rows}
    finally:
        put_conn(conn)


def compute_session_totals(session_id):
    """
    Tính tổng phiên trên PostgreSQL (một round-trip, không đọc toàn bộ bảng vào Python).
    Cập nhật tong_vao, tong_ra, doanh_thu; trả về dict giống logic Session.calc cũ hoặc None nếu không có phiên.
    """
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            WITH s AS (
                SELECT id, chiet_khau_vao, chiet_khau_ra, chiet_khau, ti_gia, ti_gia_xuat
                FROM "sessions"
                WHERE id = %s
            )
            SELECT
                s.id,
                s.chiet_khau_vao,
                s.chiet_khau_ra,
                s.chiet_khau,
                s.ti_gia,
                s.ti_gia_xuat,
                COALESCE(a.tong_vao, 0)::double precision AS tong_vao,
                COALESCE(a.tong_ra, 0)::double precision AS tong_ra,
                COALESCE(a.usdt_vao, 0)::double precision AS usdt_vao,
                COALESCE(a.usdt_ra, 0)::double precision AS usdt_ra,
                COALESCE(a.real_tong_vao, 0)::double precision AS real_tong_vao,
                COALESCE(a.tong_ra_sau_ckr, 0)::double precision AS tong_ra_sau_ckr,
                COALESCE(a.tong_vao_usdt_vnd, 0)::double precision AS tong_vao_usdt_vnd,
                COALESCE(a.tong_ra_usdt_vnd, 0)::double precision AS tong_ra_usdt_vnd
            FROM s
            LEFT JOIN (
                SELECT
                    t.session_id,
                    SUM(CASE WHEN t.type = 'income' AND t.currency = 'vnd' THEN t.amount ELSE 0 END) AS tong_vao,
                    SUM(CASE WHEN t.type = 'expense' AND t.currency = 'vnd' THEN t.amount ELSE 0 END) AS tong_ra,
                    SUM(CASE WHEN t.type = 'income' AND t.currency = 'usdt' THEN t.amount ELSE 0 END) AS usdt_vao,
                    SUM(CASE WHEN t.type = 'expense' AND t.currency = 'usdt' THEN t.amount ELSE 0 END) AS usdt_ra,
                    SUM(
                        CASE WHEN t.type = 'income' AND t.currency = 'vnd' THEN
                            t.amount * (100.0 - COALESCE(t.chiet_khau_vao, s.chiet_khau_vao, s.chiet_khau, 0)) / 100.0
                        ELSE 0 END
                    ) AS real_tong_vao,
                    SUM(
                        CASE WHEN t.type = 'expense' AND t.currency = 'vnd' THEN
                            t.amount * (100.0 + COALESCE(t.chiet_khau_ra, s.chiet_khau_ra, 0)) / 100.0
                        ELSE 0 END
                    ) AS tong_ra_sau_ckr,
                    SUM(
                        CASE WHEN t.type = 'income' AND t.currency = 'vnd' THEN
                            (t.amount * (100.0 - COALESCE(t.chiet_khau_vao, s.chiet_khau_vao, s.chiet_khau, 0)) / 100.0)
                            / GREATEST(
                                COALESCE(NULLIF(t.ti_gia, 0), NULLIF(s.ti_gia, 0), 1)::double precision,
                                1
                            )
                        ELSE 0 END
                    ) AS tong_vao_usdt_vnd,
                    SUM(
                        CASE WHEN t.type = 'expense' AND t.currency = 'vnd' THEN
                            (t.amount * (100.0 + COALESCE(t.chiet_khau_ra, s.chiet_khau_ra, 0)) / 100.0)
                            / GREATEST(
                                COALESCE(
                                    NULLIF(t.ti_gia_xuat, 0),
                                    NULLIF(s.ti_gia_xuat, 0),
                                    NULLIF(t.ti_gia, 0),
                                    NULLIF(s.ti_gia, 0),
                                    1
                                )::double precision,
                                1
                            )
                        ELSE 0 END
                    ) AS tong_ra_usdt_vnd
                FROM "transactions" t
                INNER JOIN s ON s.id = t.session_id
                GROUP BY t.session_id
            ) a ON a.session_id = s.id
            """,
            (session_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        tong_vao = float(row["tong_vao"])
        tong_ra = float(row["tong_ra"])
        usdt_vao = float(row["usdt_vao"])
        usdt_ra = float(row["usdt_ra"])
        real_tong_vao = float(row["real_tong_vao"])
        tong_ra_sau_ckr = float(row["tong_ra_sau_ckr"])
        tong_vao_usdt_vnd = float(row["tong_vao_usdt_vnd"])
        tong_ra_usdt_vnd = float(row["tong_ra_usdt_vnd"])

        doanh_thu_usdt = tong_vao_usdt_vnd - tong_ra_usdt_vnd
        u_da_thanh_toan = usdt_ra
        con_lai_u = doanh_thu_usdt - u_da_thanh_toan + usdt_vao
        doanh_thu_vnd_chua_ti_gia = real_tong_vao - tong_ra_sau_ckr

        ti_raw = row["ti_gia"]
        session_ti_gia = float(ti_raw) if ti_raw not in (None, 0) else 1.0
        if session_ti_gia == 0:
            session_ti_gia = 1.0
        ti_xuat_raw = row.get("ti_gia_xuat")
        session_ti_gia_xuat = float(ti_xuat_raw) if ti_xuat_raw not in (None, 0) else 1.0
        doanh_thu_vnd = doanh_thu_usdt * session_ti_gia

        ckv_src = row.get("chiet_khau_vao")
        if ckv_src is None:
            ckv_src = row.get("chiet_khau")
        ckv = float(ckv_src or 0)
        ckr = float(row.get("chiet_khau_ra") or 0)

        cur.execute(
            """
            UPDATE "sessions"
            SET "tong_vao" = %s, "tong_ra" = %s, "doanh_thu" = %s
            WHERE id = %s
            """,
            (tong_vao, tong_ra, doanh_thu_usdt, session_id),
        )
        conn.commit()

        return {
            "tong_vao": tong_vao,
            "tong_ra": tong_ra,
            "usdt_vao": usdt_vao,
            "usdt_ra": usdt_ra,
            "tong_vao_usdt_vnd": tong_vao_usdt_vnd,
            "tong_ra_usdt_vnd": tong_ra_usdt_vnd,
            "tong_vao_usdt": tong_vao_usdt_vnd,
            "tong_ra_usdt": tong_ra_usdt_vnd,
            "doanh_thu_usdt": doanh_thu_usdt,
            "doanh_thu_vnd": doanh_thu_vnd,
            "doanh_thu_vnd_chua_ti_gia": doanh_thu_vnd_chua_ti_gia,
            "real_tong_vao": real_tong_vao,
            "u_da_thanh_toan": u_da_thanh_toan,
            "con_lai_u": con_lai_u,
            "chiet_khau_vao": ckv,
            "chiet_khau_ra": ckr,
            "ti_gia": session_ti_gia,
            "ti_gia_xuat": session_ti_gia_xuat,
        }
    finally:
        put_conn(conn)


_SUM_KEYS = (
    "tong_vao",
    "tong_ra",
    "usdt_vao",
    "usdt_ra",
    "real_tong_vao",
    "tong_ra_sau_ckr",
    "tong_vao_usdt_vnd",
    "tong_ra_usdt_vnd",
)


def _merge_totals_dict(acc: dict | None, part: dict) -> dict:
    """Cộng dồn các chỉ số từ compute_session_totals (cùng đơn vị)."""
    out = {k: 0.0 for k in _SUM_KEYS}
    if acc:
        for k in _SUM_KEYS:
            out[k] = float(acc.get(k, 0))
    for k in _SUM_KEYS:
        out[k] += float(part.get(k, 0))
    out["doanh_thu_usdt"] = out["tong_vao_usdt_vnd"] - out["tong_ra_usdt_vnd"]
    out["con_lai_u"] = out["doanh_thu_usdt"] - out["usdt_ra"] + out["usdt_vao"]
    out["tong_vao_usdt"] = out["tong_vao_usdt_vnd"]
    out["tong_ra_usdt"] = out["tong_ra_usdt_vnd"]
    return out


def list_sessions_for_business_date(target_date):
    """
    Các phiên thuộc ngày nghiệp vụ target_date (ngày mở phiên: business_date hoặc ngày của created_at).
    target_date: datetime.date
    """
    from datetime import date as date_type

    if not isinstance(target_date, date_type):
        raise TypeError("list_sessions_for_business_date expects datetime.date")
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, chat_id, close_at, created_at, business_date
            FROM "sessions"
            WHERE COALESCE(business_date, (created_at::timestamp)::date) = %s
            ORDER BY chat_id, id
            """,
            (target_date,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        put_conn(conn)


def get_session_for_chat_business_date(chat_id: int, target_date):
    """
    Phiên của nhóm chat_id thuộc ngày nghiệp vụ target_date (business_date hoặc ngày created_at).
    Nếu có nhiều bản ghi trùng ngày (bất thường), lấy bản mới nhất theo id.
    target_date: datetime.date
    """
    from datetime import date as date_type

    if not isinstance(target_date, date_type):
        raise TypeError("get_session_for_chat_business_date expects datetime.date")
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT * FROM "sessions"
            WHERE chat_id = %s
              AND COALESCE(business_date, (created_at::timestamp)::date) = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(chat_id), target_date),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        put_conn(conn)


def logical_group_names_for_chat_ids(chat_ids):
    """chat_id -> tên nhóm logic (map /start_group)."""
    ids = []
    for x in chat_ids or []:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    ids = list(set(ids))
    if not ids:
        return {}
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT c.chat_id, g.name
            FROM chat_group_context c
            INNER JOIN groups g ON g.id = c.group_id
            WHERE c.chat_id = ANY(%s)
            """,
            (ids,),
        )
        return {int(r["chat_id"]): (r.get("name") or "") for r in cur.fetchall()}
    except Exception:
        return {}
    finally:
        put_conn(conn)


def aggregate_business_date_rows(target_date):
    """
    Tổng hợp mọi phiên có cùng ngày nghiệp vụ (theo business_date / ngày mở).

    Trả về:
      date: ISO date string
      grand: dict tổng (merge compute_session_totals) hoặc None
      chats: [{ chat_id, logical_name, sessions: [{id, close_at}], totals }]
    """
    from collections import defaultdict
    from datetime import date as date_type

    if not isinstance(target_date, date_type):
        raise TypeError("aggregate_business_date_rows expects datetime.date")
    rows = list_sessions_for_business_date(target_date)
    if not rows:
        return {"date": target_date.isoformat(), "grand": None, "chats": []}

    by_chat = defaultdict(list)
    for r in rows:
        by_chat[int(r["chat_id"])].append(r)

    names = logical_group_names_for_chat_ids(list(by_chat.keys()))
    grand = None
    chats_out = []
    for cid in sorted(by_chat.keys()):
        chat_tot = None
        meta_sessions = []
        for r in by_chat[cid]:
            t = compute_session_totals(int(r["id"]))
            if not t:
                continue
            chat_tot = _merge_totals_dict(chat_tot, t)
            meta_sessions.append({"id": int(r["id"]), "close_at": r.get("close_at")})
        if chat_tot is None:
            continue
        raw_nm = names.get(cid)
        logical = (raw_nm or "").strip() or None
        chats_out.append(
            {
                "chat_id": cid,
                "logical_name": logical,
                "sessions": meta_sessions,
                "totals": chat_tot,
            }
        )
        grand = _merge_totals_dict(grand, chat_tot)

    return {"date": target_date.isoformat(), "grand": grand, "chats": chats_out}


def list_closed_sessions_for_business_date(target_date):
    """
    Phiên đã đóng (close_at NOT NULL) thuộc ngày nghiệp vụ target_date (mọi bản ghi).
    Bot tổng kết ngày dùng list_last_closed_session_per_chat_for_business_date (mỗi nhóm một phiên).
    """
    from datetime import date as date_type

    if not isinstance(target_date, date_type):
        raise TypeError("list_closed_sessions_for_business_date expects datetime.date")
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, chat_id, close_at, created_at, business_date
            FROM "sessions"
            WHERE close_at IS NOT NULL
              AND COALESCE(business_date, (created_at::timestamp)::date) = %s
            ORDER BY chat_id, id
            """,
            (target_date,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        put_conn(conn)


def list_last_closed_session_per_chat_for_business_date(target_date):
    """
    Với mỗi chat_id trong ngày nghiệp vụ: chỉ lấy **một** phiên đã đóng — bản có id lớn nhất
    (phiên tạo sau cùng trong các phiên đã đóng của ngày đó). Dùng cho tổng kết ngày.
    """
    from datetime import date as date_type

    if not isinstance(target_date, date_type):
        raise TypeError("list_last_closed_session_per_chat_for_business_date expects datetime.date")
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, chat_id, close_at, created_at, business_date
            FROM (
                SELECT DISTINCT ON (chat_id)
                    id, chat_id, close_at, created_at, business_date
                FROM "sessions"
                WHERE close_at IS NOT NULL
                  AND COALESCE(business_date, (created_at::timestamp)::date) = %s
                ORDER BY chat_id, id DESC
            ) sub
            ORDER BY chat_id
            """,
            (target_date,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        put_conn(conn)


def aggregate_closed_vnd_for_tongket_day(target_date):
    """
    Phiên đã đóng trong ngày nghiệp vụ: **mỗi nhóm (chat_id) chỉ lấy một phiên** — phiên có id
    lớn nhất trong ngày (phiên tạo sau cùng trong các phiên đã đóng), rồi cộng toàn hệ thống.

    Công thức tổng kết (bot tổng kế toán, sau khi bạn nhập giá U thực tế `gia_u_set` trong UI):
    - **Tổng vào hiện tại (U)** = Σ `tong_vao_usdt_vnd` — đúng số U trong ngoặc của dòng
      «Tổng vào VND → U» trên tin close từng nhóm (theo tỉ giá/CK từng phiên).
    - **Tổng vào thực tế (U)** = (Σ VND vào thô `tong_vao`) / `gia_u_set` — chưa qua tỉ giá từng nhóm.
    - **Lợi nhuận (U)** = thực tế − hiện tại (tính trong UI khi đã có `gia_u_set`).
    - **Lợi nhuận từ chiết khấu (VND)** = Σ theo từng phiên đã đóng:
      `(VND vào thô phiên) × (CKV % phiên) / 100` — CKV = chiết khấu vào trên phiên
      (cùng logic dòng «Tổng vào VND → U» × % trên tin close).

    Trả về: tong_vao_hien_tai_u, tong_vao_vnd, loi_nhuan_chiet_khau_vnd, so_nhom, so_phien,
            chi_tiet_nhom (list từng nhóm: tên logic, VND vào thô, U hiện tại, LN CK VND, số phiên)
            (kèm usdt_vao, tong_ra_vnd chỉ để tham khảo hiển thị phụ nếu cần).
    """
    from collections import defaultdict
    from datetime import date as date_type

    if not isinstance(target_date, date_type):
        raise TypeError("aggregate_closed_vnd_for_tongket_day expects datetime.date")

    rows = list_last_closed_session_per_chat_for_business_date(target_date)
    if not rows:
        return {
            "tong_vao_hien_tai_u": 0.0,
            "tong_vao_vnd": 0.0,
            "loi_nhuan_chiet_khau_vnd": 0.0,
            "tong_ra_vnd": 0.0,
            "usdt_vao": 0.0,
            "usdt_ra": 0.0,
            "so_nhom": 0,
            "so_phien": 0,
            "chi_tiet_nhom": [],
        }

    by_chat = defaultdict(list)
    for r in rows:
        by_chat[int(r["chat_id"])].append(r)

    names = logical_group_names_for_chat_ids(list(by_chat.keys()))

    grand = None
    nhom_tham_gia = 0
    loi_nhuan_chiet_khau_vnd = 0.0
    chi_tiet_nhom = []

    for cid, sess_rows in sorted(by_chat.items(), key=lambda kv: kv[0]):
        chat_tot = None
        loi_ck_chat = 0.0
        n_sess_ok = 0
        for r in sess_rows:
            t = compute_session_totals(int(r["id"]))
            if not t:
                continue
            vnd_tho = float(t.get("tong_vao", 0) or 0)
            ckv = float(t.get("chiet_khau_vao", 0) or 0)
            part_ck = vnd_tho * (ckv / 100.0)
            loi_ck_chat += part_ck
            loi_nhuan_chiet_khau_vnd += part_ck
            chat_tot = _merge_totals_dict(chat_tot, t)
            n_sess_ok += 1
        if chat_tot is None:
            continue
        nhom_tham_gia += 1
        grand = _merge_totals_dict(grand, chat_tot)
        ten = (names.get(cid) or "").strip()
        if not ten:
            ten = f"Nhóm chat {cid}"
        chi_tiet_nhom.append(
            {
                "chat_id": cid,
                "ten_nhom": ten,
                "tong_vao_vnd": float(chat_tot.get("tong_vao", 0)),
                "tong_vao_hien_tai_u": float(chat_tot.get("tong_vao_usdt_vnd", 0)),
                "loi_nhuan_chiet_khau_vnd": float(loi_ck_chat),
                "so_phien": n_sess_ok,
            }
        )

    chi_tiet_nhom.sort(key=lambda x: (str(x["ten_nhom"]).lower(), x["chat_id"]))

    if grand is None:
        return {
            "tong_vao_hien_tai_u": 0.0,
            "tong_vao_vnd": 0.0,
            "loi_nhuan_chiet_khau_vnd": loi_nhuan_chiet_khau_vnd,
            "tong_ra_vnd": 0.0,
            "usdt_vao": 0.0,
            "usdt_ra": 0.0,
            "so_nhom": 0,
            "so_phien": len(rows),
            "chi_tiet_nhom": chi_tiet_nhom,
        }

    # Chỉ lấy U từ nhánh VND→U (khớp ngoặc «Tổng vào VND → U: … (xxx U)»), không cộng U+ trực tiếp vào dòng này.
    tong_vao_hien_tai_u = float(grand.get("tong_vao_usdt_vnd", 0))

    return {
        "tong_vao_hien_tai_u": tong_vao_hien_tai_u,
        "tong_vao_vnd": float(grand.get("tong_vao", 0)),
        "loi_nhuan_chiet_khau_vnd": loi_nhuan_chiet_khau_vnd,
        "tong_ra_vnd": float(grand.get("tong_ra", 0)),
        "usdt_vao": float(grand.get("usdt_vao", 0)),
        "usdt_ra": float(grand.get("usdt_ra", 0)),
        "so_nhom": nhom_tham_gia,
        "so_phien": len(rows),
        "chi_tiet_nhom": chi_tiet_nhom,
    }


def get_tongket_row(ngay):
    """ngay: datetime.date → dict hoặc None."""
    from datetime import date as date_type

    if not isinstance(ngay, date_type):
        raise TypeError("get_tongket_row expects datetime.date")
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT * FROM "tongket" WHERE ngay = %s', (ngay,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        put_conn(conn)


def tongket_upsert(
    ngay,
    gia_u_set,
    tong_vao_hien_tai_u,
    tong_vao_thuc_te_u,
    loi_nhuan,
    so_nhom_tham_gia,
    loi_nhuan_chiet_khau_vnd,
):
    """
    Lưu tongket:
    - tong_vao = tổng vào hiện tại (U) từ close
    - tong_vao_thuc_te_u = (Σ VND vào thô) / gia_u_set
    - tong_ra = 0 (cột cũ, không còn dùng cho tổng kết; tránh nhầm «tổng ra»)
    - loi_nhuan = thực tế − hiện tại
    - loi_nhuan_chiet_khau_vnd = Σ (VND vào thô × CKV%) theo từng phiên đóng trong ngày
    """
    from datetime import date as date_type

    if not isinstance(ngay, date_type):
        raise TypeError("tongket_upsert expects datetime.date")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tongket (ngay, gia_u_set, tong_vao, tong_ra, tong_vao_thuc_te_u, loi_nhuan, so_nhom_tham_gia, loi_nhuan_chiet_khau_vnd)
            VALUES (%s, %s, %s, 0, %s, %s, %s, %s)
            ON CONFLICT (ngay) DO UPDATE SET
                gia_u_set = EXCLUDED.gia_u_set,
                tong_vao = EXCLUDED.tong_vao,
                tong_ra = 0,
                tong_vao_thuc_te_u = EXCLUDED.tong_vao_thuc_te_u,
                loi_nhuan = EXCLUDED.loi_nhuan,
                so_nhom_tham_gia = EXCLUDED.so_nhom_tham_gia,
                loi_nhuan_chiet_khau_vnd = EXCLUDED.loi_nhuan_chiet_khau_vnd,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                ngay,
                float(gia_u_set),
                float(tong_vao_hien_tai_u),
                float(tong_vao_thuc_te_u),
                float(loi_nhuan),
                int(so_nhom_tham_gia),
                float(loi_nhuan_chiet_khau_vnd),
            ),
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        put_conn(conn)


def load_transactions_for_display(session_id, max_lines=None):
    """
    Tải giao dịch để hiển thị. Nếu max_lines > 0 và tổng số dòng > max_lines, chỉ đọc N dòng mới nhất.
    Tránh COUNT khi phiên có ≤ max_lines giao dịch (chỉ 1 query).
    Trả về (list[dict], hidden_count).
    """
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if max_lines is None or max_lines <= 0:
            cur.execute(
                """
                SELECT * FROM "transactions"
                WHERE "session_id" = %s
                ORDER BY "created_at" ASC, "id" ASC
                """,
                (session_id,),
            )
            return [dict(r) for r in cur.fetchall()], 0

        cur.execute(
            """
            SELECT * FROM "transactions"
            WHERE "session_id" = %s
            ORDER BY "created_at" DESC, "id" DESC
            LIMIT %s
            """,
            (session_id, max_lines),
        )
        chunk = [dict(r) for r in cur.fetchall()]
        if len(chunk) < max_lines:
            chunk.reverse()
            return chunk, 0

        cur.execute(
            'SELECT COUNT(*)::int AS c FROM "transactions" WHERE "session_id" = %s',
            (session_id,),
        )
        total = cur.fetchone()["c"]
        chunk.reverse()
        hidden = max(0, total - max_lines)
        return chunk, hidden
    finally:
        put_conn(conn)


# =========================
# Nhóm (admin) + map chat → nhóm (persist DB)
# =========================


def get_user(username):
    """Trả về (id, username) hoặc None — tương thích code cũ dùng db_user[0]."""
    if not username:
        return None
    r = DB.table("users").where("username", username).first()
    if not r:
        return None
    return (r["id"], r["username"])


def set_user_role(username: str, role: str) -> int:
    """
    Cập nhật cột users.role (raw SQL — bảng users không có updated_at nên không dùng QueryBuilder.update).
    Chỉ hỗ trợ role 'user' (đồng bộ sau add_user / gỡ viewer cũ trong DB).
    """
    if role != "user":
        raise ValueError("role must be 'user'")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            'UPDATE "users" SET role = %s WHERE username = %s',
            (role, username),
        )
        conn.commit()
        return cur.rowcount
    finally:
        put_conn(conn)


def add_group(admin_user_id, name):
    return DB.table("groups").insert(
        {
            "admin_user_id": admin_user_id,
            "name": name.strip(),
            "status": "closed",
        }
    )


def list_groups(filter_admin_id=None):
    q = DB.table("groups")
    if filter_admin_id is not None:
        q = q.where("admin_user_id", filter_admin_id)
    rows = q.order_by("id", "ASC").get()
    out = []
    for r in rows:
        out.append(
            (
                r["id"],
                r["admin_user_id"],
                r["name"],
                r.get("chiet_khau_vao") or 0,
                r.get("chiet_khau_ra") or 0,
                r.get("ti_gia_mua") or 1,
                r.get("ti_gia_ban") or 1,
                r.get("status") or "closed",
            )
        )
    return out


def get_group(group_id):
    r = DB.table("groups").where("id", group_id).first()
    if not r:
        return None
    return (
        r["id"],
        r["admin_user_id"],
        r["name"],
        r.get("chiet_khau_vao") or 0,
        r.get("chiet_khau_ra") or 0,
        r.get("ti_gia_mua") or 1,
        r.get("ti_gia_ban") or 1,
        r.get("status") or "closed",
    )


def update_group_field(group_id, field, value):
    allowed = {
        "name": str,
        "chiet_khau_vao": float,
        "chiet_khau_ra": float,
        "ti_gia_mua": int,
        "ti_gia_ban": int,
    }
    if field not in allowed:
        raise ValueError(f"field không hợp lệ: {field}")
    t = allowed[field]
    if t is str:
        v = str(value).strip()
    elif t is int:
        v = int(value)
    else:
        v = float(value)
    DB.table("groups").where("id", group_id).update({field: v})


def delete_group(group_id):
    DB.table("chat_group_context").where("group_id", group_id).delete()
    DB.table("groups").where("id", group_id).delete()


def set_current_group(_admin_user_id, chat_id, group_id):
    """Gắn chat Telegram với bản ghi nhóm (upsert DB)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO chat_group_context (chat_id, group_id)
            VALUES (%s, %s)
            ON CONFLICT (chat_id) DO UPDATE SET
                group_id = EXCLUDED.group_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (chat_id, group_id),
        )
        conn.commit()
    finally:
        put_conn(conn)


def get_current_group(_admin_user_id, chat_id):
    """group_id đang gắn với chat (nếu có)."""
    r = DB.table("chat_group_context").where("chat_id", chat_id).first()
    return r["group_id"] if r else None


def set_group_session_status_for_chat(chat_id, status):
    """
    Phiên Telegram mở/đóng → cập nhật trạng thái nhóm logic đã gắn với chat.
    status: 'open' | 'closed'
    """
    if status not in ("open", "closed"):
        return
    ctx = DB.table("chat_group_context").where("chat_id", chat_id).first()
    if not ctx:
        return
    DB.table("groups").where("id", ctx["group_id"]).update({"status": status})


# =========================
# MINI ORM GIỐNG LARAVEL DB
# =========================
class DB:
    def __init__(self, table):
        self.table = table
        self._where = []
        self._params = []
        self._order = ""
        self._limit = ""

    @staticmethod
    def table(name):
        """Khởi tạo DB với tên bảng"""
        return DB(name)

    @staticmethod
    def raw(value):
        """Trả về giá trị SQL không escape"""
        return {"__raw__": value}

    def where(self, column, operator=None, value=None):
        if value is None:
            value = operator
            operator = "="

        if value is None:
            self._where.append(f'"{column}" IS NULL')
        elif isinstance(value, dict) and "__raw__" in value:
            self._where.append(f'"{column}" {operator} {value["__raw__"]}')
        else:
            self._where.append(f'"{column}" {operator} %s')
            self._params.append(value)
        return self

    def where_null(self, column):
        self._where.append(f'"{column}" IS NULL')
        return self

    def where_not_null(self, column):
        self._where.append(f'"{column}" IS NOT NULL')
        return self

    def order_by(self, column, direction="ASC"):
        self._order = f' ORDER BY "{column}" {direction.upper()}'
        return self

    def limit(self, n):
        self._limit = f" LIMIT {n}"
        return self

    def first(self):
        sql = f'SELECT * FROM "{self.table}"'
        if self._where:
            sql += " WHERE " + " AND ".join(self._where)
        sql += self._order
        sql += " LIMIT 1"

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        out = None
        try:
            cur.execute(sql, self._params)
            row = cur.fetchone()
            out = dict(row) if row else None
        finally:
            try:
                cur.close()
            except Exception:
                pass
            put_conn(conn)
        self._reset()
        return out

    def get(self):
        sql = f'SELECT * FROM "{self.table}"'
        if self._where:
            sql += " WHERE " + " AND ".join(self._where)
        sql += self._order
        sql += self._limit

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        out = []
        try:
            cur.execute(sql, self._params)
            rows = cur.fetchall()
            out = [dict(row) for row in rows]
        finally:
            try:
                cur.close()
            except Exception:
                pass
            put_conn(conn)
        self._reset()
        return out

    def all(self):
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        out = []
        try:
            cur.execute(f'SELECT * FROM "{self.table}"')
            rows = cur.fetchall()
            out = [dict(row) for row in rows]
        finally:
            try:
                cur.close()
            except Exception:
                pass
            put_conn(conn)
        return out

    def find(self, id):
        return self.where("id", id).first()

    def exists(self):
        sql = f'SELECT 1 FROM "{self.table}"'
        if self._where:
            sql += " WHERE " + " AND ".join(self._where)
        sql += " LIMIT 1"

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        exists = False
        try:
            cur.execute(sql, self._params)
            exists = cur.fetchone() is not None
        finally:
            try:
                cur.close()
            except Exception:
                pass
            put_conn(conn)
        self._reset()
        return exists

    def count(self):
        sql = f'SELECT COUNT(*) as total FROM "{self.table}"'
        if self._where:
            sql += " WHERE " + " AND ".join(self._where)

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        count = 0
        try:
            cur.execute(sql, self._params)
            count = cur.fetchone()["total"]
        finally:
            try:
                cur.close()
            except Exception:
                pass
            put_conn(conn)
        self._reset()
        return count

    def insert(self, data):
        keys = ", ".join([f'"{k}"' for k in data.keys()])
        placeholders = ", ".join(["%s"] * len(data))
        values = tuple(data.values())
        sql = f'INSERT INTO "{self.table}" ({keys}) VALUES ({placeholders}) RETURNING id'

        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute(sql, values)
            last_id = cur.fetchone()[0]
            conn.commit()
            return last_id
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                cur.close()
            except Exception:
                pass
            put_conn(conn)

    def update(self, data):
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        set_clause = []
        values = []
        for k, v in data.items():
            if isinstance(v, dict) and "__raw__" in v:
                set_clause.append(f'"{k}" = {v["__raw__"]}')
            else:
                set_clause.append(f'"{k}" = %s')
                values.append(v)

        sql = f'UPDATE "{self.table}" SET {", ".join(set_clause)}'
        if self._where:
            sql += " WHERE " + " AND ".join(self._where)

        params = values + self._params
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            conn.commit()
            affected = cur.rowcount
            return affected
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                cur.close()
            except Exception:
                pass
            put_conn(conn)
            self._reset()

    def delete(self):
        sql = f'DELETE FROM "{self.table}"'
        if self._where:
            sql += " WHERE " + " AND ".join(self._where)

        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute(sql, self._params)
            conn.commit()
            affected = cur.rowcount
            return affected
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                cur.close()
            except Exception:
                pass
            put_conn(conn)
            self._reset()

    def first_or_create(self, where_data, create_data=None):
        for k, v in where_data.items():
            self = self.where(k, v)
        record = self.first()
        if record:
            return record
        create_data = create_data or where_data
        new_id = DB.table(self.table).insert(create_data)
        return DB.table(self.table).find(new_id)

    def _reset(self):
        self._where = []
        self._params = []
        self._order = ""
        self._limit = ""
