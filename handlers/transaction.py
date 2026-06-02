import asyncio
import logging
from collections import defaultdict

from telegram import Update
from telegram.ext import ContextTypes

from database.models import (
    DB,
    telegram_bot_dedupe_key,
    telegram_update_release_claim,
    telegram_update_try_claim,
)
from handlers.session import Session, TX_DISPLAY_AFTER_TRADE
from utils import auth_required, now_app

logger = logging.getLogger(__name__)

_CHAT_TX_LOCKS = defaultdict(asyncio.Lock)


class Transaction:
    @staticmethod
    def _format_tx_short(tx: dict) -> str:
        amount = float(tx.get("amount", 0) or 0)
        amount_str = f"{amount:,.2f}".rstrip("0").rstrip(".")
        currency = tx.get("currency", "vnd")
        tx_type = tx.get("type")
        if currency == "usdt":
            sign = "u+" if tx_type == "income" else "u-"
            unit = "U"
        else:
            sign = "+" if tx_type == "income" else "-"
            unit = "VND"
        return f"{sign}{amount_str} {unit}"

    @auth_required
    async def add_manual(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        sign: str,
        number: float,
        currency: str = "vnd",
    ):
        chat_id = update.effective_chat.id
        async with _CHAT_TX_LOCKS[chat_id]:
            try:
                if sign == "+":
                    trans_type = "income"
                elif sign == "-":
                    trans_type = "expense"
                else:
                    await update.message.reply_text("⚠️ Dấu không hợp lệ, chỉ nhận + hoặc -.")
                    return
                if currency not in ["vnd", "usdt"]:
                    await update.message.reply_text("⚠️ Loại tiền không hợp lệ.")
                    return

                username = update.effective_user.username

                # Check if user exists
                db_user = DB.table("users").where("username", username).first()
                if not db_user:
                    await update.message.reply_text("⚠️ Bạn chưa được đăng ký trong hệ thống.")
                    return

                # Check if there is an open session
                session = DB.table("sessions").where("chat_id", chat_id).where_null("close_at").first()
                if not session:
                    await update.message.reply_text("⚠️ Vui lòng bắt đầu một phiên trước khi ghi giao dịch.")
                    return

                dedupe_key = telegram_bot_dedupe_key()
                idem_storage = None
                uid = getattr(update, "update_id", None)
                if uid is not None:
                    ok_idem, idem_storage = telegram_update_try_claim(dedupe_key, uid)
                    if not ok_idem:
                        await update.message.reply_text(
                            "ℹ️ Tin nhắn này đã được xử lý trước đó (Telegram gửi lặp). Không ghi thêm giao dịch."
                        )
                        return

                # Không set tỉ giá thì mặc định = 1 (cho cả vào/xuất).
                session_ti_gia = session.get("ti_gia", 1) or 1
                session_ti_gia_xuat = session.get("ti_gia_xuat", 1) or 1

                # Snapshot tỉ giá + CKV/CKR tại thời điểm giao dịch (đổi sau không ảnh hưởng dòng cũ).
                tx_ti_gia = session_ti_gia
                tx_ti_gia_xuat = session_ti_gia_xuat
                snap_ckv = session.get("chiet_khau_vao", session.get("chiet_khau", 0)) or 0
                snap_ckr = session.get("chiet_khau_ra", 0) or 0
                if currency == "vnd":
                    row_ckv = snap_ckv if trans_type == "income" else 0
                    row_ckr = snap_ckr if trans_type == "expense" else 0
                else:
                    row_ckv = 0
                    row_ckr = 0

                now = now_app()
                try:
                    DB.table("transactions").insert(
                        {
                            "session_id": session["id"],
                            "user_id": db_user["id"],
                            "type": trans_type,
                            "amount": number,
                            "currency": currency,
                            "ti_gia": tx_ti_gia,
                            "ti_gia_xuat": tx_ti_gia_xuat,
                            "chiet_khau_vao": row_ckv,
                            "chiet_khau_ra": row_ckr,
                            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
                except Exception:
                    telegram_update_release_claim(dedupe_key, uid, idem_storage)
                    raise
                # Tổng phiên cập nhật trong Session.calc (tin chi tiết sau khóa).
            except Exception:
                await update.message.reply_text("⚠️ Có lỗi khi ghi giao dịch. Vui lòng thử lại sau vài giây.")
                return

        amount_display = f"{number:,.2f}".rstrip("0").rstrip(".")
        unit = "USDT" if currency == "usdt" else "VND"
        symbol = "📈" if sign == "+" else "📉"
        tx_prefix = f"u{sign}" if currency == "usdt" else sign
        try:
            await update.message.reply_text(
                f"{symbol} Đã ghi nhận {tx_prefix}{amount_display} {unit}. Chi tiết ở tin tiếp theo."
            )
        except Exception as e:
            logger.warning("Không gửi được tin xác nhận ngắn sau khi ghi DB: %s", e)

        totals = await Session.calc(session["id"])
        session_row = DB.table("sessions").where("id", session["id"]).first() or session
        ctx = Session._display_context(session_row, totals)

        transactions_list, _, _ = Session._format_transactions_list(
            session_row["id"],
            ctx["ckv"],
            ctx["ckr"],
            ctx["ti_gia"],
            ctx["ti_gia_xuat"],
            max_lines=TX_DISPLAY_AFTER_TRADE,
        )

        totals_lines = Session._format_totals_lines_from_context(ctx)

        message = (
            f"📊 Chi tiết phiên (sau {tx_prefix}{amount_display} {unit}):\n\n"
            f"{transactions_list}\n\n"
            f"{totals_lines}\n"
            f"ℹ️ Danh sách giao dịch đầy đủ: gõ data (hoặc close khi xong phiên)."
        )

        try:
            await Session._reply_text_safe(update, message)
        except Exception as e:
            logger.warning("Không gửi được tin chi tiết sau ghi giao dịch: %s", e)
            try:
                await update.message.reply_text(
                    "⚠️ Giao dịch đã được ghi. Gõ data nếu cần xem tổng hợp đầy đủ (tin chi tiết có thể không gửi được)."
                )
            except Exception:
                pass

    @auth_required
    async def undo_last(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        async with _CHAT_TX_LOCKS[chat_id]:
            username = update.effective_user.username
            db_user = DB.table("users").where("username", username).first()
            if not db_user:
                return await update.message.reply_text("⚠️ Bạn chưa được đăng ký trong hệ thống.")

            session = DB.table("sessions").where("chat_id", chat_id).where_null("close_at").first()
            if not session:
                return await update.message.reply_text("⚠️ Không có phiên mở để hoàn tác.")

            last_tx = (
                DB.table("transactions")
                .where("session_id", session["id"])
                .order_by("id", "DESC")
                .first()
            )
            if not last_tx:
                return await update.message.reply_text("⚠️ Phiên hiện tại chưa có giao dịch nào để hoàn tác.")

            dedupe_key = telegram_bot_dedupe_key()
            idem_storage = None
            uid = getattr(update, "update_id", None)
            if uid is not None:
                ok_idem, idem_storage = telegram_update_try_claim(dedupe_key, uid)
                if not ok_idem:
                    return await update.message.reply_text(
                        "ℹ️ Tin nhắn này đã được xử lý trước đó (Telegram gửi lặp). Không hoàn tác thêm."
                    )

            try:
                DB.table("transactions").where("id", last_tx["id"]).delete()
            except Exception:
                telegram_update_release_claim(dedupe_key, uid, idem_storage)
                logger.exception("Hoàn tác: xóa giao dịch thất bại")
                return await update.message.reply_text("⚠️ Không hoàn tác được. Thử lại sau vài giây.")
            # Tổng phiên: Session.calc trong tin chi tiết sau khóa.

        try:
            await update.message.reply_text("↩️ Đã hoàn tác giao dịch gần nhất. Chi tiết ở tin tiếp theo.")
        except Exception as e:
            logger.warning("Không gửi được tin xác nhận ngắn sau hoàn tác: %s", e)

        totals = await Session.calc(session["id"])
        session_row = DB.table("sessions").where("id", session["id"]).first() or session
        ctx = Session._display_context(session_row, totals)

        transactions_list, _, _ = Session._format_transactions_list(
            session_row["id"],
            ctx["ckv"],
            ctx["ckr"],
            ctx["ti_gia"],
            ctx["ti_gia_xuat"],
            max_lines=TX_DISPLAY_AFTER_TRADE,
        )

        totals_lines = Session._format_totals_lines_from_context(ctx)

        message = (
            f"📊 Chi tiết phiên sau hoàn tác (đã xóa: {self._format_tx_short(last_tx)}):\n\n"
            f"{transactions_list}\n\n"
            f"{totals_lines}\n"
            f"ℹ️ Danh sách giao dịch đầy đủ: gõ data (hoặc close khi xong phiên)."
        )
        try:
            await Session._reply_text_safe(update, message)
        except Exception as e:
            logger.warning("Không gửi được tin chi tiết sau hoàn tác: %s", e)
            try:
                await update.message.reply_text(
                    "⚠️ Hoàn tác đã xong. Gõ data nếu cần xem tổng hợp (tin chi tiết có thể không gửi được)."
                )
            except Exception:
                pass
