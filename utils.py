import os
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes
from database.models import DB
from dotenv import load_dotenv

load_dotenv()
SUPER_ADMIN = os.getenv("SUPER_ADMIN")
SUPER_ADMIN2 = os.getenv("SUPER_ADMIN2")

# Múi giờ hiển thị / ghi close_at, created_at, giao dịch (mặc định VN). Tránh server UTC làm lệch ngày "Chốt ngày".
_APP_TZ_NAME = os.getenv("APP_TIMEZONE", "Asia/Ho_Chi_Minh")
APP_TIMEZONE = ZoneInfo(_APP_TZ_NAME)


def now_app() -> datetime:
    """Giờ hiện tại theo múi ứng dụng (offset-aware)."""
    return datetime.now(APP_TIMEZONE)


def as_app_tz(dt: datetime) -> datetime:
    """Naive datetime từ DB → gắn múi APP (coi chuỗi DB là giờ địa phương app)."""
    if dt.tzinfo is not None:
        return dt.astimezone(APP_TIMEZONE)
    return dt.replace(tzinfo=APP_TIMEZONE)


def is_super_admin(username: str) -> bool:
    if not username:
        return False
    u = username.lower()
    # Super admin từ biến môi trường
    if SUPER_ADMIN and u == SUPER_ADMIN.lower():
        return True
    if SUPER_ADMIN2 and u == SUPER_ADMIN2.lower():
        return True
    # Admin tổng trong DB
    return DB.table("admins").where("username", username).exists()


def ensure_env_super_admin_users() -> None:
    """
    Đảm bảo user trong DB cho SUPER_ADMIN và SUPER_ADMIN2 (.env) nếu đặt tên và chưa có.
    Gọi sau init_db() từ bot nhóm hoặc bot tổng kết.
    """
    for label, raw in (("SUPER_ADMIN", SUPER_ADMIN), ("SUPER_ADMIN2", SUPER_ADMIN2)):
        name = (raw or "").strip()
        if not name:
            continue
        if not DB.table("users").where("username", name).exists():
            DB.table("users").insert({"username": name})
            print(f"✅ Đã thêm {label} vào bảng users: {name}")


def auth(username: str) -> bool:
    if not username:
        return False
    if is_super_admin(username):
        return True
    return DB.table("users").where("username", username).exists()


def auth_required(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        """
        Hỗ trợ cả method trong class lẫn function độc lập.
        """
        # Nếu là method (có self)
        if len(args) >= 3:
            self, update, context = args[0], args[1], args[2]
            other_args = args[3:]
        else:
            update, context = args[0], args[1]
            other_args = args[2:]
            self = None

        user = update.effective_user
        if not user or not auth(user.username):
            await update.message.reply_text("⚠️ Bạn chưa được đăng ký hoặc không có quyền thực hiện lệnh này.")
            return

        # Gọi hàm gốc đúng thứ tự
        if self:
            return await func(self, update, context, *other_args, **kwargs)
        else:
            return await func(update, context, *other_args, **kwargs)

    return wrapper


def get_user_role(username: str) -> str:
    """
    super_admin — biến .env hoặc bảng admins.
    admin — user là chủ ít nhất một nhóm (groups.admin_user_id).
    viewer — cột users.role = viewer (chỉ xem phiên: data / help).
    user — mặc định.
    """
    if not username:
        return "none"
    if is_super_admin(username):
        return "super_admin"
    row = DB.table("users").where("username", username).first()
    if not row:
        return "none"
    uid = row["id"]
    if DB.table("groups").where("admin_user_id", uid).exists():
        return "admin"
    if (row.get("role") or "user").strip().lower() == "viewer":
        return "viewer"
    return "user"


def is_viewer(username: str) -> bool:
    return bool(username) and get_user_role(username) == "viewer"


def role_middleware(allowed_roles):
    """Decorator cho lệnh nhóm /new_group … — chỉ role trong danh sách được gọi handler."""

    allowed = frozenset(allowed_roles)

    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
            user = update.effective_user
            if not user or not auth(user.username):
                await update.message.reply_text(
                    "⚠️ Bạn chưa được đăng ký hoặc không có quyền thực hiện lệnh này."
                )
                return
            role = get_user_role(user.username)
            if role not in allowed:
                await update.message.reply_text("⚠️ Bạn không có quyền thực hiện lệnh này.")
                return
            return await func(update, context, *a, **kw)

        return wrapper

    return decorator


async def deny_if_viewer(update: Update) -> bool:
    """
    True nếu user là viewer và đã gửi tin từ chối — handler nên return ngay.
    """
    user = update.effective_user
    if not user or not user.username or not is_viewer(user.username):
        return False
    if update.message:
        await update.message.reply_text(
            "⚠️ Tài khoản viewer chỉ xem phiên: lệnh data, help (không ghi +/−, không mở/đóng/sửa phiên)."
        )
    return True