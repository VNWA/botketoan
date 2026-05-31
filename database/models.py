import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os

load_dotenv()

# =========================
# CẤU HÌNH DATABASE
# =========================
DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASS"),
    "database": os.getenv("DB_DATABASE"),
    "port": os.getenv("DB_PORT", "5432")
}


# =========================
# KẾT NỐI DATABASE
# =========================
def get_conn():
    conn = psycopg2.connect(**DB_CONFIG)
    return conn


# =========================
# KHỞI TẠO CÁC BẢNG
# =========================
def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # === Users ===
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(255) UNIQUE
        )
    """)

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

    conn.commit()
    conn.close()


def purge_closed_sessions_older_than_days(days: int = 3):
    """
    Xóa transactions rồi sessions đã đóng lâu hơn `days` ngày.
    Trả về (số_session_xóa, số_transaction_xóa).
    """
    conn = get_conn()
    cur = conn.cursor()
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
    conn.close()
    return n_sess, n_tx


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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        cur.execute(sql, self._params)
        row = cur.fetchone()
        conn.close()
        self._reset()
        return dict(row) if row else None

    def get(self):
        sql = f'SELECT * FROM "{self.table}"'
        if self._where:
            sql += " WHERE " + " AND ".join(self._where)
        sql += self._order
        sql += self._limit

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, self._params)
        rows = cur.fetchall()
        conn.close()
        self._reset()
        return [dict(row) for row in rows]

    def all(self):
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(f'SELECT * FROM "{self.table}"')
        rows = cur.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def find(self, id):
        return self.where("id", id).first()

    def exists(self):
        sql = f'SELECT 1 FROM "{self.table}"'
        if self._where:
            sql += " WHERE " + " AND ".join(self._where)
        sql += " LIMIT 1"

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, self._params)
        exists = cur.fetchone() is not None
        conn.close()
        self._reset()
        return exists

    def count(self):
        sql = f'SELECT COUNT(*) as total FROM "{self.table}"'
        if self._where:
            sql += " WHERE " + " AND ".join(self._where)

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, self._params)
        count = cur.fetchone()["total"]
        conn.close()
        self._reset()
        return count

    def insert(self, data):
        keys = ", ".join([f'"{k}"' for k in data.keys()])
        placeholders = ", ".join(["%s"] * len(data))
        values = tuple(data.values())
        sql = f'INSERT INTO "{self.table}" ({keys}) VALUES ({placeholders}) RETURNING id'

        conn = get_conn()
        cur = conn.cursor()
        cur.execute(sql, values)
        last_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return last_id

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
        cur.execute(sql, params)
        conn.commit()
        affected = cur.rowcount
        conn.close()
        self._reset()
        return affected

    def delete(self):
        sql = f'DELETE FROM "{self.table}"'
        if self._where:
            sql += " WHERE " + " AND ".join(self._where)

        conn = get_conn()
        cur = conn.cursor()
        cur.execute(sql, self._params)
        conn.commit()
        affected = cur.rowcount
        conn.close()
        self._reset()
        return affected

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
