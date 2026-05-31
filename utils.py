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
    # Check from env variable
    if SUPER_ADMIN and username.lower() == SUPER_ADMIN.lower():
        return True
    # Check from database admins table
    return DB.table("admins").where("username", username).exists()


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