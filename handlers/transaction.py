from telegram import Update
from telegram.ext import ContextTypes
from database.models import DB
from utils import auth_required, now_app
from datetime import datetime
from handlers.session import Session, TX_DISPLAY_AFTER_TRADE
from collections import defaultdict
import asyncio


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

                # Recalculate session totals
                totals = await Session.calc(session["id"])
                if not totals:
                    # Fallback simple message if something goes wrong
                    symbol = "📈" if sign == "+" else "📉"
                    unit = "USDT" if currency == "usdt" else "VND"
                    amount_display = f"{number:,.2f}".rstrip("0").rstrip(".")
                    await update.message.reply_text(f"{symbol} Ghi nhận giao dịch {sign}{amount_display} {unit} ({trans_type}) thành công.\n")
                    return
            except Exception:
                await update.message.reply_text("⚠️ Có lỗi khi ghi giao dịch. Vui lòng thử lại sau vài giây.")
                return

        ckv = totals.get("chiet_khau_vao", 0)
        ckr = totals.get("chiet_khau_ra", 0)
        real_tong_vao = totals["real_tong_vao"]
        tong_vao = totals["tong_vao"]
        tong_ra = totals["tong_ra"]
        tong_vao_usdt = totals.get("tong_vao_usdt_vnd", totals.get("tong_vao_usdt", 0))
        tong_ra_usdt = totals.get("tong_ra_usdt_vnd", totals.get("tong_ra_usdt", 0))
        current_ti_gia = totals.get("ti_gia", 1)
        current_ti_gia_xuat = totals.get("ti_gia_xuat", 1)

        # Format chiết khấu để hiển thị đẹp
        def format_chiet_khau(ck):
            if ck % 1 == 0:
                return f"{ck:.0f}%"
            else:
                return f"{ck:.2f}%".rstrip('0').rstrip('.') + "%"
        ckv_str = format_chiet_khau(ckv)
        ckr_str = format_chiet_khau(ckr)

        # Get formatted transactions list
        transactions_list, _, _ = Session._format_transactions_list(
            session["id"],
            ckv,
            ckr,
            totals.get("ti_gia", 1),
            totals.get("ti_gia_xuat", 1),
            max_lines=TX_DISPLAY_AFTER_TRADE,
        )

        symbol = "📈" if sign == "+" else "📉"

        amount_display = f"{number:,.2f}".rstrip("0").rstrip(".")
        unit = "USDT" if currency == "usdt" else "VND"
        revenue_block = Session.format_revenue_block(totals)
        message = (
            f"{symbol} Giao dịch {sign}{amount_display} {unit} đã được ghi nhận.\n\n"
            f"{transactions_list}\n\n"
            f"💱 Tỉ giá vào: {current_ti_gia:,} | Tỉ giá xuất: {current_ti_gia_xuat:,} | CKV: {ckv_str} | CKR: {ckr_str}\n"
            f"💰 Tổng vào VND → U: {tong_vao:,.0f} VND ({tong_vao_usdt:,.2f} U)\n"
            f"💸 Tổng chi VND → U: {tong_ra:,.0f} VND ({tong_ra_usdt:,.2f} U)\n"
            f"💰 Tổng vào sau CKV (VND): {real_tong_vao:,.0f} VND\n"
            f"{revenue_block}"
        )

        await Session._reply_text_safe(update, message)

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

            DB.table("transactions").where("id", last_tx["id"]).delete()
            totals = await Session.calc(session["id"])

        if not totals:
            return await update.message.reply_text("✅ Đã hoàn tác giao dịch gần nhất.")

        ckv = totals.get("chiet_khau_vao", 0)
        ckr = totals.get("chiet_khau_ra", 0)
        current_ti_gia = totals.get("ti_gia", 1)
        current_ti_gia_xuat = totals.get("ti_gia_xuat", 1)
        tong_vao = totals.get("tong_vao", 0)
        tong_ra = totals.get("tong_ra", 0)
        tong_vao_usdt = totals.get("tong_vao_usdt_vnd", totals.get("tong_vao_usdt", 0))
        tong_ra_usdt = totals.get("tong_ra_usdt_vnd", totals.get("tong_ra_usdt", 0))
        revenue_block = Session.format_revenue_block(totals)

        transactions_list, _, _ = Session._format_transactions_list(
            session["id"],
            ckv,
            ckr,
            current_ti_gia,
            current_ti_gia_xuat,
            max_lines=TX_DISPLAY_AFTER_TRADE,
        )

        def format_chiet_khau(ck):
            if ck % 1 == 0:
                return f"{ck:.0f}%"
            return f"{ck:.2f}%".rstrip("0").rstrip(".") + "%"

        message = (
            f"↩️ Đã hoàn tác giao dịch gần nhất: `{self._format_tx_short(last_tx)}`\n\n"
            f"{transactions_list}\n\n"
            f"💱 Tỉ giá vào: {current_ti_gia:,} | Tỉ giá xuất: {current_ti_gia_xuat:,} | CKV: {format_chiet_khau(ckv)} | CKR: {format_chiet_khau(ckr)}\n"
            f"💰 Tổng vào VND → U: {tong_vao:,.0f} VND ({tong_vao_usdt:,.2f} U)\n"
            f"💸 Tổng chi VND → U: {tong_ra:,.0f} VND ({tong_ra_usdt:,.2f} U)\n"
            f"{revenue_block}"
        )
        await Session._reply_text_safe(update, message, parse_mode="Markdown")
