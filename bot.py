import asyncio
import logging
import os
import re
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
from database.models import (
    init_db,
    DB,
    purge_closed_sessions_older_than_days,
    purge_processed_telegram_updates_older_than_days,
)
from handlers.user import User
from handlers.session import Session
from handlers.transaction import Transaction
from utils import ensure_env_super_admin_users

load_dotenv()
PURGE_CLOSED_DAYS = int(os.getenv("PURGE_CLOSED_DAYS", "3"))
PURGE_INTERVAL_SEC = int(os.getenv("PURGE_INTERVAL_SEC", "3600"))
PURGE_START_DELAY_SEC = int(os.getenv("PURGE_START_DELAY_SEC", "120"))
IDEM_PURGE_DAYS = int(os.getenv("IDEM_PURGE_DAYS", "14"))

logger = logging.getLogger(__name__)


def _telegram_concurrent_updates():
    """Nhiều nhóm song song: số worker (1–256), hoặc true/false."""
    raw = (os.getenv("TELEGRAM_CONCURRENT_UPDATES") or "24").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("true", "yes", "on"):
        return True
    try:
        n = int(raw)
        return max(1, min(n, 256))
    except ValueError:
        return 24


# Global handlers so they can be reused for both slash commands and plain text commands
user_handler = User()
session_handler = Session()
transaction_handler = Transaction()

def init():
    init_db()
    ensure_env_super_admin_users()


async def _purge_closed_sessions_loop():
    """Định kỳ xóa phiên đã đóng > PURGE_CLOSED_DAYS và transactions liên quan (chạy trong thread)."""
    await asyncio.sleep(PURGE_START_DELAY_SEC)
    while True:
        try:
            n_sess, n_tx = await asyncio.to_thread(
                purge_closed_sessions_older_than_days,
                PURGE_CLOSED_DAYS,
            )
            if n_sess or n_tx:
                logger.info(
                    "DB purge: removed %s closed session(s), %s transaction row(s) (>%s days)",
                    n_sess,
                    n_tx,
                    PURGE_CLOSED_DAYS,
                )
            n_idem = await asyncio.to_thread(
                purge_processed_telegram_updates_older_than_days,
                IDEM_PURGE_DAYS,
            )
            if n_idem:
                logger.info("Idempotency table purge: removed %s row(s) (>%s days)", n_idem, IDEM_PURGE_DAYS)
        except Exception:
            logger.exception("DB purge failed")
        await asyncio.sleep(PURGE_INTERVAL_SEC)


async def post_init(application):
    asyncio.create_task(_purge_closed_sessions_loop())


# ===================== TRANSACTION HANDLER (+ / -) =====================
async def handle_transaction_message(update, context):
    """
    Handle simple manual transaction messages:
    - VND flow: +1000 / -500
    - USDT direct flow: u+10 / u-3.5
    """
    text = update.message.text.strip()

    pattern = r"^(u?[+-])(\d+(?:\.\d+)?)$"
    match = re.match(pattern, text)

    if match:
        raw_sign = match.group(1).lower()
        number = float(match.group(2))
        currency = "usdt" if raw_sign.startswith("u") else "vnd"
        sign = raw_sign[1:] if currency == "usdt" else raw_sign
        await transaction_handler.add_manual(update, context, sign, number, currency)
    
       
async def handle_text_message(update, context):
    """
    Router for plain text messages.
    - If text looks like a known command (without leading '/'), call the corresponding handler.
    - Otherwise, try to handle it as a manual transaction (+1000 / -500 / u+10 / u-3.5).
    """
    raw_text = update.message.text or ""
    text = raw_text.strip()
    if not text:
        return

    # Normalize: remove optional leading slash, make command lower case
    normalized = text
    if normalized.startswith("/"):
        normalized = normalized[1:]
    normalized = normalized.strip()

    parts = normalized.split()
    if not parts:
        return

    # Emulate CommandHandler behavior: args = the rest of the tokens
    # This prevents context.args from being None in handlers.
    context.args = parts[1:]

    command = parts[0].lower()

    # Strict validation:
    # - Commands without arguments must be exactly one word (e.g. "start")
    # - Commands with arguments must have at least 2 tokens (e.g. "ckv 10")

    # Help commands (no arguments)
    if command == "help" and len(parts) == 1:
        await help(update, context)
        return

    if command == "help_admin" and len(parts) == 1:
        await help_admin(update, context)
        return

    # User commands
    if command == "add_user":
        await user_handler.add(update, context)
        return

    if command == "list_user" and len(parts) == 1:
        await user_handler.list(update, context)
        return

    if command == "remove_user" and len(parts) >= 2:
        await user_handler.delete(update, context)
        return

    if command == "add_admin" and len(parts) >= 2:
        await user_handler.add_admin(update, context)
        return

    if command == "list_admin" and len(parts) == 1:
        await user_handler.list_admin(update, context)
        return

    # Session commands
    if command == "start" and len(parts) == 1:
        await session_handler.start(update, context)
        return

    if command == "close" and len(parts) == 1:
        await session_handler.close(update, context)
        return

    if command == "data" and len(parts) == 1:
        await session_handler.data(update, context)
        return

    if command == "mo_lai_phien":
        await session_handler.reopen_by_business_date(update, context)
        return

    if command in ["ckv", "ck"] and len(parts) >= 2:
        await session_handler.edit_chiet_khau_vao(update, context)
        return

    if command == "ckr" and len(parts) >= 2:
        await session_handler.edit_chiet_khau_ra(update, context)
        return

    if command == "tigia" and len(parts) >= 2:
        await session_handler.edit_ti_gia(update, context)
        return

    if command in ["tigiax", "tigia_xuat"] and len(parts) >= 2:
        await session_handler.edit_ti_gia_xuat(update, context)
        return

    if command in ["back", "undo", "huy_lenh_truoc"] and len(parts) == 1:
        await transaction_handler.undo_last(update, context)
        return

    # If the text is not a valid command, try to treat it as a transaction.
    # Supported: +number, -number, u+number, u-number
    await handle_transaction_message(update, context)

async def help_admin(update, context):
    await update.message.reply_text(
        "🤖 Supper Admin Command!\n\n"
        "Các lệnh quản lý người dùng:\n"
        "add_user <tên> [user|admin] — thêm user / đổi quyền (mặc định user; admin = admin tổng như add_admin)\n"
        "list_user - Xem danh sách user (không tính admin)\n"
        "remove_user <username> - Xóa user\n"
        "add_admin <username> - Thêm admin tổng\n"
        "list_admin - Xem danh sách admin tổng\n"
        "Các lệnh bot:\n"
        "start, close, data, mo_lai_phien dd-mm-yyyy, ckv, ckr, tigia, tigiax, back\n"
        "Giao dịch nhanh:\n"
        "+số / -số (VND → tính doanh thu)\n"
        "u+số / u-số (USDT trực tiếp; còn lại = doanh thu VND − u- + u+)"
    )

async def help(update, context):
    await update.message.reply_text(
        "🤖 Bot Command!\n\n"
        "Các lệnh quản lý phiên:\n"
        "start - Mở phiên mới (sau `close`, `start` lại = phiên trắng, không kế thừa giao dịch phiên cũ; sửa ngày đã đóng: mo_lai_phien)\n"
        "close - Đóng phiên\n"
        "data - Xem thông tin phiên hiện tại\n"
        "mo_lai_phien dd-mm-yyyy - Mở lại phiên đã đóng của ngày đó (chỉnh sửa dữ liệu cũ; đóng phiên hiện tại trước)\n"
        "ckv <giá trị> - Cập nhật chiết khấu vào (%) cho lệnh +\n"
        "ckr <giá trị> - Cập nhật chiết khấu ra (%) cho lệnh -\n"
        "tigia <giá trị> - Cập nhật tỉ giá vào (cho lệnh +)\n"
        "tigiax <giá trị> - Cập nhật tỉ giá xuất cho lệnh trừ\n\n"
        "back - Hoàn tác giao dịch gần nhất (+, -, u+, u-)\n\n"
        "Hoặc nhập trực tiếp:\n"
        "+1000 để cộng tiền VND\n"
        "-500 để trừ tiền VND\n"
        "u+10 / u-3.5 ghi USDT trực tiếp (không vào dòng doanh thu VND).\n"
        "Sau mỗi lệnh +/- hoặc back, bot chỉ gửi vài giao dịch mới nhất (mặc định 3); xem đủ và tổng vào/ra: data hoặc close.\n"
        "Tổng kết: Doanh thu (VND), U+ riêng, U đã thanh toán (u-), Còn lại = doanh thu − u- + u+"
    ) 

# ===================== MAIN =====================
def main():
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    init()
    token = os.getenv("KETOAN_TOKEN")
    app = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(_telegram_concurrent_updates())
        .post_init(post_init)
        .build()
    )

    # Help
    app.add_handler(CommandHandler("help", help))
    app.add_handler(CommandHandler("help_admin", help_admin))

    # User 
    app.add_handler(CommandHandler("add_user", user_handler.add))
    app.add_handler(CommandHandler("list_user", user_handler.list))
    app.add_handler(CommandHandler("remove_user", user_handler.delete))
    app.add_handler(CommandHandler("add_admin", user_handler.add_admin))
    app.add_handler(CommandHandler("list_admin", user_handler.list_admin))

    # Session 
    app.add_handler(CommandHandler("start", session_handler.start))
    app.add_handler(CommandHandler("close", session_handler.close))
    app.add_handler(CommandHandler("data", session_handler.data))
    app.add_handler(CommandHandler("mo_lai_phien", session_handler.reopen_by_business_date))
    app.add_handler(CommandHandler("ckv", session_handler.edit_chiet_khau_vao))
    app.add_handler(CommandHandler("ckr", session_handler.edit_chiet_khau_ra))
    app.add_handler(CommandHandler("ck", session_handler.edit_chiet_khau_vao))
    app.add_handler(CommandHandler("tigia", session_handler.edit_ti_gia))
    app.add_handler(CommandHandler("tigiax", session_handler.edit_ti_gia_xuat))
    app.add_handler(CommandHandler("tigia_xuat", session_handler.edit_ti_gia_xuat))
    app.add_handler(CommandHandler("back", transaction_handler.undo_last))
    app.add_handler(CommandHandler("undo", transaction_handler.undo_last))
    app.add_handler(CommandHandler("huy_lenh_truoc", transaction_handler.undo_last))

    # Plain text router:
    # - Recognizes commands like "start", "close", "data", "mo_lai_phien", "ckv", "ckr", "tigia", "help", "help_admin"
    # - Still supports manual transactions like "+1000", "-500", "u+10", "u-3.5"
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    print("🚀 Bot đang chạy...")
    app.run_polling()

if __name__ == "__main__":
    main()
