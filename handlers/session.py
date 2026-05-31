import asyncio

from telegram import Update
from telegram.ext import ContextTypes
from database.models import DB, fetch_usernames_by_ids, compute_session_totals, load_transactions_for_display
from utils import auth_required, is_super_admin, now_app, as_app_tz
from datetime import datetime
from dotenv import load_dotenv
import os
load_dotenv()

MESSAGE_START = os.getenv("MESSAGE_START")

# Sau mỗi lệnh +/- chỉ hiển thị N giao dịch mới nhất (data / close vẫn xem đủ — max_lines=None).
TX_DISPLAY_AFTER_TRADE = int(os.getenv("TX_DISPLAY_AFTER_TRADE", "3"))
# Cơ chế an toàn khi phiên quá lớn: data/close sẽ tự động chỉ hiện phần đuôi để tránh treo bot.
TX_FULL_RENDER_MAX = int(os.getenv("TX_FULL_RENDER_MAX", "500"))
TX_FULL_RENDER_TAIL = int(os.getenv("TX_FULL_RENDER_TAIL", "150"))


class Session:
    @staticmethod
    async def _reply_text_safe(update: Update, text: str, parse_mode=None, chunk_size: int = 3500):
        """
        Telegram giới hạn ~4096 ký tự/tin.
        Chia nhỏ theo dòng để tránh gửi fail khi phiên có quá nhiều giao dịch.
        """
        if text is None:
            return
        if len(text) <= chunk_size:
            await update.message.reply_text(text, parse_mode=parse_mode)
            return

        lines = text.split("\n")
        current = ""
        for line in lines:
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= chunk_size:
                current = candidate
                continue
            if current:
                await update.message.reply_text(current, parse_mode=parse_mode)
            if len(line) <= chunk_size:
                current = line
            else:
                # Fallback cho dòng quá dài bất thường.
                for i in range(0, len(line), chunk_size):
                    await update.message.reply_text(line[i:i + chunk_size], parse_mode=parse_mode)
                current = ""
        if current:
            await update.message.reply_text(current, parse_mode=parse_mode)

    @staticmethod
    def _get_or_create_user_for_session(username: str):
        db_user = DB.table("users").where("username", username).first()
        if db_user:
            return db_user
        # Admin/Super Admin có thể thao tác phiên dù chưa có trong users.
        if is_super_admin(username):
            new_id = DB.table("users").insert({"username": username})
            return DB.table("users").where("id", new_id).first()
        return None

    # ================= Mở phiên =================
    @auth_required
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        chat_id = update.effective_chat.id

        if len(context.args) > 0:
            return await update.message.reply_text("⚠️ Cú pháp: start (không thêm gì sau lệnh này)")

        db_user = self._get_or_create_user_for_session(user.username)
        if not db_user:
            return await update.message.reply_text("⚠️ Bạn chưa được đăng ký trong hệ thống.")

        if DB.table("sessions").where("chat_id", chat_id).where_null("close_at").exists():
            return await update.message.reply_text("⚠️ Nhóm đã có phiên chưa đóng. Đóng phiên cũ trước.")

        session_id = DB.table("sessions").insert({
            "chat_id": chat_id,
            "user_id": db_user["id"],
            "chiet_khau": 0,
            "chiet_khau_vao": 0,
            "chiet_khau_ra": 0,
            "ti_gia": 1,
            "ti_gia_xuat": 1,
            "tong_vao": 0,
            "tong_ra": 0,
            "doanh_thu": 0,
            "created_at": now_app().strftime("%Y-%m-%d %H:%M:%S")
        })

        await update.message.reply_text(
            f"{MESSAGE_START}, @{user.username}\n\n"
            "✅ Phiên mới đã được tạo thành công!\n"
            f"🆔 ID Phiên: {session_id}\n"
            "💱 Tỉ giá vào: 1\n"
            "💱 Tỉ giá xuất: 1\n"
            "💰 CKV (chiết khấu vào): 0%\n"
            "💸 CKR (chiết khấu ra): 0%\n\n"
            "📘 *Cách sử dụng nhanh:*\n"
            "- Gõ `ckv 10` để đặt chiết khấu vào 10%\n"
            "- Gõ `ckr 10` để đặt chiết khấu ra 10%\n"
            "- Gõ `tigia 23000` để đặt tỉ giá vào cho lệnh cộng\n"
            "- Gõ `tigiax 23100` để đặt tỉ giá xuất cho lệnh trừ\n"
            "- Gõ `back` để hoàn tác giao dịch gần nhất\n"
            "- Gõ `+1000` / `-500` để ghi giao dịch VND\n"
            "- Gõ `u+10` / `u-3.5` để ghi USDT trực tiếp (không tính vào doanh thu VND; xem dòng U+ / U đã thanh toán / Còn lại)\n"
            "- Gõ `data` để xem tổng kết tạm thời\n"
            "- Gõ `close` để đóng phiên\n\n"
            "- Gõ `mo_lai_phien_truoc` để mở lại phiên vừa đóng (khi chưa có phiên mở)\n\n"
            "👉 Có thể dùng cả `/start`, `/ckv`, `/ckr`, `/tigia`, `/tigiax`, `/back`, `/data`, `/close`, `/mo_lai_phien_truoc` hoặc không có dấu `/` đều được.\n"
            "ℹ️ Gõ `help` để xem đầy đủ danh sách lệnh.",
            parse_mode="Markdown"
        )

    # ================= Tính toán doanh thu =================
    @staticmethod
    async def calc(session_id):
        # Chạy SQL aggregate trên worker thread — không block event loop, không nạp toàn bộ transactions.
        return await asyncio.to_thread(compute_session_totals, session_id)

    @staticmethod
    def format_revenue_block(totals):
        """Doanh thu chỉ từ VND; u+/u- tách; còn lại = doanh thu − u- + u+."""
        if not totals:
            return ""
        dt_u = totals.get("doanh_thu_usdt", 0)
        uv = totals.get("usdt_vao", 0)
        ur = totals.get("usdt_ra", 0)
        cl = totals.get("con_lai_u", dt_u - ur + uv)
        return (
            f"📊 Doanh thu (từ VND): {dt_u:,.2f} USDT\n"
            f"💵 U+ trực tiếp: {uv:,.2f} U\n"
            f"💸 U đã thanh toán (u-): {ur:,.2f} U\n"
            f"➡️ Còn lại: {cl:,.2f} U"
        )

    # ================= Xem thông tin phiên =================
    @auth_required
    async def data(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        user = update.effective_user

        db_user = DB.table("users").where("username", user.username).first()
        if not db_user:
            return await update.message.reply_text("⚠️ Bạn chưa được đăng ký trong hệ thống.")

        session = DB.table("sessions").where("chat_id", chat_id).where_null("close_at").first()
        if not session:
            return await update.message.reply_text("⚠️ Không tìm thấy phiên nào đang mở trong nhóm này.")

        totals = await self.calc(session['id'])
        session = DB.table("sessions").where("id", session["id"]).first()
        info_message = self._format_session_info(session, db_user['username'], totals)
        await self._reply_text_safe(update, info_message)

    # ================= Đóng phiên =================
    @auth_required
    async def close(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        user = update.effective_user

        # Admin/Super Admin có thể đóng phiên của nhóm dù do người khác mở.
        session = DB.table("sessions").where("chat_id", chat_id).where_null("close_at").first()
        if not session:
            return await update.message.reply_text("⚠️ Không tìm thấy phiên đang mở.")

        close_time = now_app()
        DB.table("sessions").where("id", session["id"]).update({"close_at": close_time.strftime("%Y-%m-%d %H:%M:%S")})

        # Handle both string and datetime for created_at (naive trong DB = giờ APP_TIMEZONE)
        created_at_raw = session.get("created_at")
        if isinstance(created_at_raw, datetime):
            created_at = as_app_tz(created_at_raw)
        else:
            created_at = as_app_tz(datetime.strptime(str(created_at_raw), "%Y-%m-%d %H:%M:%S"))
        duration = close_time - created_at
        total_sec = max(0, int(duration.total_seconds()))
        hours, remainder = divmod(total_sec, 3600)
        minutes, _ = divmod(remainder, 60)

        totals = await self.calc(session['id'])
        tv_u = totals.get("tong_vao_usdt_vnd", totals.get("tong_vao_usdt", 0)) if totals else 0
        tr_u = totals.get("tong_ra_usdt_vnd", totals.get("tong_ra_usdt", 0)) if totals else 0

        ti_gia = (totals or {}).get("ti_gia") or session.get("ti_gia", 1) or 1
        if ti_gia == 0:
            ti_gia = 1
        ti_gia_xuat = (totals or {}).get("ti_gia_xuat") or session.get("ti_gia_xuat", 1) or 1
        if ti_gia_xuat == 0:
            ti_gia_xuat = 1
        display_ti_gia = session.get("ti_gia", ti_gia) or 1
        display_ti_gia_xuat = session.get("ti_gia_xuat", ti_gia_xuat) or 1

        ckv = session.get('chiet_khau_vao', session.get('chiet_khau', 0))
        ckr = session.get('chiet_khau_ra', 0)
        if totals:
            ckv = totals.get("chiet_khau_vao", ckv)
            ckr = totals.get("chiet_khau_ra", ckr)

        transactions_list, _, _ = self._format_transactions_list(
            session['id'], ckv, ckr, ti_gia, ti_gia_xuat, max_lines=None
        )

        tw = totals["tong_vao"] if totals else session["tong_vao"]
        tr = totals["tong_ra"] if totals else session["tong_ra"]

        info_message = (
            f"✅ Phiên đã được đóng thành công!\n\n"
            f"📊 Thông tin phiên\n"
            f"🆔 ID Phiên: {session['id']}\n"
            f"👤 Người tạo: @{user.username}\n"
            f"🕒 Thời gian mở: {session['created_at']}\n"
            f"🕒 Thời gian đóng: {close_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"⏱ Thời gian chạy: {hours} giờ {minutes} phút\n\n"
            f"{transactions_list}\n\n"
            f"💱 Tỉ giá vào: {display_ti_gia:,} VND/USDT | Tỉ giá xuất: {display_ti_gia_xuat:,} VND/USDT | CKV: {self._format_chiet_khau(ckv)} | CKR: {self._format_chiet_khau(ckr)}\n"
            f"💰 Tổng vào VND → U: {self._format_vnd(tw)} ({tv_u:,.2f} U)\n"
            f"💸 Tổng chi VND → U: {self._format_vnd(tr)} ({tr_u:,.2f} U)\n"
            f"{self.format_revenue_block(totals)}\n\n"
            f"Chốt ngày : {close_time.day} - {close_time.month} - {close_time.year}"
        )
        await self._reply_text_safe(update, info_message)

    # ================= Mở lại phiên gần nhất đã đóng =================
    @auth_required
    async def reopen_last_closed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        user = update.effective_user
        if len(context.args) > 0:
            return await update.message.reply_text("⚠️ Cú pháp: mo_lai_phien_truoc (không thêm gì sau lệnh này)")

        db_user = self._get_or_create_user_for_session(user.username)
        if not db_user:
            return await update.message.reply_text("⚠️ Bạn chưa được đăng ký trong hệ thống.")

        # Chỉ cho mở lại khi hiện tại chưa có phiên mở.
        if DB.table("sessions").where("chat_id", chat_id).where_null("close_at").exists():
            return await update.message.reply_text("⚠️ Nhóm đang có phiên mở. Hãy đóng phiên hiện tại trước.")

        last_closed = (
            DB.table("sessions")
            .where("chat_id", chat_id)
            .where_not_null("close_at")
            .order_by("close_at", "DESC")
            .first()
        )
        if not last_closed:
            return await update.message.reply_text("⚠️ Không có phiên nào đã đóng để mở lại.")

        DB.table("sessions").where("id", last_closed["id"]).update({"close_at": None})

        totals = await self.calc(last_closed["id"])
        reopened = DB.table("sessions").where("id", last_closed["id"]).first()
        info_message = self._format_session_info(reopened, user.username, totals)
        await self._reply_text_safe(
            update,
            f"♻️ Đã mở lại phiên gần nhất thành công!\n\n{info_message}",
        )

    # ================= Cập nhật chung =================
    async def _update_field(self, update, context, field_name, label, value_type="int", suffix="", min_val=None, max_val=None):
        chat_id = update.effective_chat.id
        user = update.effective_user
        db_user = DB.table("users").where("username", user.username).first()
        if not db_user:
            return await update.message.reply_text("⚠️ Bạn chưa được đăng ký trong hệ thống.")

        session = DB.table("sessions").where("chat_id", chat_id).where_null("close_at").first()
        if not session:
            return await update.message.reply_text("⚠️ Không tìm thấy phiên đang mở.")

        args = context.args or []
        if len(args) < 1:
            return await update.message.reply_text("⚠️ Cú pháp: ckv/ckr/tigia/tigiax + <giá trị> (vd: ckv 10)")

        raw_value = args[0].replace("%", "").strip()
        try:
            value = int(raw_value) if value_type == "int" else float(raw_value)
        except ValueError:
            return await update.message.reply_text("⚠️ Giá trị phải là số hợp lệ.")

        if min_val is not None and value < min_val:
            return await update.message.reply_text(f"⚠️ Giá trị tối thiểu: {min_val}.")
        if max_val is not None and value > max_val:
            return await update.message.reply_text(f"⚠️ Giá trị tối đa: {max_val}.")

        DB.table("sessions").where("id", session["id"]).update({field_name: value})

        # Tự động tính lại doanh thu nếu chỉnh ckv/ckr/tỉ giá
        if field_name in ["chiet_khau_vao", "chiet_khau_ra", "ti_gia", "ti_gia_xuat"]:
            await self.calc(session['id'])

        # Format display value based on type and suffix
        if suffix == "%":
            # For percentage, show decimal if needed (e.g., 3.5% or 10%)
            if value_type == "float" and value % 1 != 0:
                display_value = f"{value:.2f}%".rstrip('0').rstrip('.') + "%"
            else:
                display_value = f"{value:.0f}%"
        else:
            display_value = f"{value:,}"
        await update.message.reply_text(f"✅ Đã cập nhật *{label}* = `{display_value}`", parse_mode="Markdown")

    @auth_required
    async def edit_chiet_khau_vao(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._update_field(update, context, "chiet_khau_vao", "Chiết khấu vào (CKV)", "float", "%", 0, 100)

    @auth_required
    async def edit_chiet_khau_ra(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._update_field(update, context, "chiet_khau_ra", "Chiết khấu ra (CKR)", "float", "%", 0, 100)

    @auth_required
    async def edit_ti_gia(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._update_field(update, context, "ti_gia", "Tỉ giá vào (áp dụng cho lệnh cộng)", "int")

    @auth_required
    async def edit_ti_gia_xuat(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._update_field(update, context, "ti_gia_xuat", "Tỉ giá xuất (áp dụng cho lệnh trừ)", "int")

    # ================= Hàm format thông tin phiên =================
    def _format_session_info(self, session, username, totals=None):
        ti_gia = session.get('ti_gia', 1)
        if ti_gia == 0:
            ti_gia = 1
        ti_gia_xuat = session.get('ti_gia_xuat', 1) or 1
        ckv = session.get('chiet_khau_vao', session.get('chiet_khau', 0))
        ckr = session.get('chiet_khau_ra', 0)
        if totals:
            tong_vao_usdt = totals.get("tong_vao_usdt_vnd", totals.get("tong_vao_usdt", 0))
            tong_ra_usdt = totals.get("tong_ra_usdt_vnd", totals.get("tong_ra_usdt", 0))
            ti_gia = totals.get("ti_gia") or ti_gia
            ti_gia_xuat = totals.get("ti_gia_xuat") or ti_gia_xuat
            if ti_gia == 0:
                ti_gia = 1
            ckv = totals.get("chiet_khau_vao", ckv)
            ckr = totals.get("chiet_khau_ra", ckr)
        else:
            real_tong_vao = session['tong_vao'] * (100 - ckv) / 100
            tong_vao_usdt = real_tong_vao / ti_gia if ti_gia != 0 else 0
            tong_ra_sau_ckr = session['tong_ra'] * (100 + ckr) / 100
            tong_ra_usdt = tong_ra_sau_ckr / ti_gia if ti_gia != 0 else 0
            dt_fallback = tong_vao_usdt - tong_ra_usdt
            totals = {
                "doanh_thu_usdt": dt_fallback,
                "doanh_thu_vnd": dt_fallback * ti_gia,
                "usdt_vao": 0,
                "usdt_ra": 0,
                "con_lai_u": dt_fallback,
            }

        transactions_list, _, _ = self._format_transactions_list(
            session['id'], ckv, ckr, ti_gia, ti_gia_xuat, max_lines=None
        )

        tw = totals.get("tong_vao", session["tong_vao"]) if totals else session["tong_vao"]
        tr = totals.get("tong_ra", session["tong_ra"]) if totals else session["tong_ra"]

        return (
            f"📊 Thông tin phiên hiện tại\n"
            f"🆔 ID Phiên: {session['id']}\n"
            f"👤 Người tạo: @{username}\n"
            f"🕒 Thời gian tạo: {session.get('created_at', '—')}\n\n"
            f"{transactions_list}\n\n"
            f"💱 Tỉ giá vào: {ti_gia:,} VND/USDT | Tỉ giá xuất: {ti_gia_xuat:,} VND/USDT | CKV: {self._format_chiet_khau(ckv)} | CKR: {self._format_chiet_khau(ckr)}\n"
            f"💰 Tổng vào VND → U: {self._format_vnd(tw)} ({tong_vao_usdt:,.2f} U)\n"
            f"💸 Tổng chi VND → U: {self._format_vnd(tr)} ({tong_ra_usdt:,.2f} U)\n"
            f"{self.format_revenue_block(totals)}"
        )

    # ================= Hàm helper format VND =================
    @staticmethod
    def _format_vnd(amount):
        return f"{amount:,.0f} ₫"

    # ================= Hàm helper format chiết khấu =================
    @staticmethod
    def _format_chiet_khau(chiet_khau):
        """Format chiết khấu, hiển thị số thập phân nếu cần (e.g., 3.5% or 10%)"""
        if chiet_khau % 1 == 0:
            return f"{chiet_khau:.0f}%"
        else:
            return f"{chiet_khau:.2f}%".rstrip('0').rstrip('.') + "%"

    # ================= Hàm format danh sách giao dịch =================
    @staticmethod
    def _format_transactions_list(session_id, ckv, ckr, default_ti_gia, default_ti_gia_xuat=None, max_lines=None):
        """Format danh sách giao dịch theo thứ tự thời gian (không chia nạp/rút).

        max_lines: nếu set và số giao dịch vượt quá, chỉ đọc + hiển thị N dòng mới nhất (không tải cả phiên vào RAM).
        """
        if max_lines is not None and max_lines <= 0:
            max_lines = None

        safety_prefix = ""
        if max_lines is None:
            # Với data/close: nếu phiên quá lớn, tự động hiển thị phần đuôi để giữ bot phản hồi ổn định.
            total = DB.table("transactions").where("session_id", session_id).count()
            if total > TX_FULL_RENDER_MAX:
                max_lines = max(1, TX_FULL_RENDER_TAIL)
                safety_prefix = (
                    f"⚠️ Phiên có {total} giao dịch, bot đang hiển thị {max_lines} giao dịch mới nhất để tránh treo.\n"
                )

        transactions, hidden = load_transactions_for_display(session_id, max_lines)

        if not transactions:
            return "", 0, 0

        hidden_prefix = ""
        if hidden > 0:
            hidden_prefix = f"… Ẩn {hidden} giao dịch cũ (gõ data hoặc close để xem đủ).\n"

        user_map = fetch_usernames_by_ids(t["user_id"] for t in transactions)

        transaction_lines = []
        income_count = 0
        expense_count = 0

        for trans in transactions:
            username = user_map.get(trans["user_id"], "Unknown")
            
            # Parse time
            created_at = trans.get("created_at")
            if isinstance(created_at, datetime):
                time_str = created_at.strftime("%H:%M")
            else:
                try:
                    dt = datetime.strptime(str(created_at), "%Y-%m-%d %H:%M:%S")
                    time_str = dt.strftime("%H:%M")
                except:
                    time_str = "00:00"
            
            amount = trans["amount"]
            if float(amount).is_integer():
                amount_str = f"{amount:,.0f}"
            else:
                amount_str = f"{amount:,.2f}".rstrip("0").rstrip(".")
            trans_type = trans["type"]
            currency = trans.get("currency", "vnd")
            
            if trans_type == "income":
                if currency == "usdt":
                    transaction_lines.append(f"{time_str}  u+{amount:,.2f}U @{username}")
                else:
                    rate = trans.get("ti_gia") or default_ti_gia or 1
                    ckv_tx = trans.get("chiet_khau_vao")
                    if ckv_tx is None:
                        ckv_tx = ckv
                    ckv_pct = f"{ckv_tx:.0f}%" if float(ckv_tx).is_integer() else f"{ckv_tx:.2f}%".rstrip("0").rstrip(".") + "%"
                    net_amount = amount * (100 - ckv_tx) / 100
                    usdt_amount = net_amount / rate if rate != 0 else 0
                    transaction_lines.append(f"{time_str}  ({amount_str} - {ckv_pct}) / {rate:,}={usdt_amount:.2f}U @{username}")
                income_count += 1
            else:
                if currency == "usdt":
                    transaction_lines.append(f"{time_str}  u-{amount:,.2f}U @{username}")
                else:
                    ckr_tx = trans.get("chiet_khau_ra")
                    if ckr_tx is None:
                        ckr_tx = ckr
                    ckr_str = f"{ckr_tx:.0f}%" if float(ckr_tx).is_integer() else f"{ckr_tx:.2f}%".rstrip("0").rstrip(".") + "%"
                    gross_amount = amount * (100 + ckr_tx) / 100
                    gross_amount_str = f"{gross_amount:,.0f}" if float(gross_amount).is_integer() else f"{gross_amount:,.2f}".rstrip("0").rstrip(".")
                    out_rate = trans.get("ti_gia_xuat") or default_ti_gia_xuat or default_ti_gia or 1
                    gross_usdt = gross_amount / out_rate if out_rate else 0
                    transaction_lines.append(f"{time_str}  -({amount_str} + {ckr_str}) = {gross_amount_str} / {out_rate:,} = {gross_usdt:.2f}U @{username}")
                expense_count += 1
        
        body = "\n".join(transaction_lines) if transaction_lines else ""
        result = (safety_prefix + hidden_prefix + body) if body else (safety_prefix + hidden_prefix).rstrip("\n")
        return result, income_count, expense_count
