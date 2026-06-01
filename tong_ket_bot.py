"""
Bot tổng kế toán — process riêng (TONG_KET_TOAN_TOKEN).

UI: /start → chọn ngày (inline, ~6 tháng + phân trang) → tongket / nhập giá U.
Chỉ tính phiên *đã đóng* theo ngày mở phiên (business_date).

Chạy: python tong_ket_bot.py  (cùng .env + PostgreSQL với bot nhóm)
"""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from database.models import init_db
from handlers.tong_ket_ui import cmd_help, cmd_start, on_gia_u_message, tongket_callback
from utils import ensure_env_super_admin_users

load_dotenv()

logger = logging.getLogger(__name__)


def _telegram_concurrent_updates_tongket():
    raw = (os.getenv("TELEGRAM_CONCURRENT_UPDATES_TONGKET") or "12").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("true", "yes", "on"):
        return True
    try:
        return max(1, min(int(raw), 64))
    except ValueError:
        return 12


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = (os.getenv("TONG_KET_TOAN_TOKEN") or "").strip()
    if not token:
        print("Thiếu TONG_KET_TOAN_TOKEN trong .env")
        return
    init_db()
    ensure_env_super_admin_users()
    app = ApplicationBuilder().token(token).concurrent_updates(_telegram_concurrent_updates_tongket()).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(tongket_callback, pattern=r"^tk:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_gia_u_message))
    print("Bot tổng kế toán đang chạy (menu /start)...")
    app.run_polling()


if __name__ == "__main__":
    main()
