"""
Luồng UI bot tổng kế toán: inline keyboard chọn ngày (6 tháng + phân trang),
bảng tongket, nhập giá U, chỉ tính phiên đã đóng theo ngày mở phiên.
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import date, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from database.models import (
    aggregate_closed_vnd_for_tongket_day,
    aggregate_standard_sessions_in_range,
    get_tongket_row,
    tongket_upsert,
)
from handlers.session import Session
from tong_ket_mirror import split_telegram_text_chunks
from utils import now_app

logger = logging.getLogger(__name__)

# ~6 tháng, mỗi trang PAGE_SIZE nút ngày
MONTHS_BACK = 6
PAGE_SIZE = 6


def _all_dates() -> list[date]:
    today = now_app().date()
    n = 31 * MONTHS_BACK + 1
    return [today - timedelta(days=i) for i in range(n)]


def total_pages() -> int:
    d = _all_dates()
    return max(1, (len(d) + PAGE_SIZE - 1) // PAGE_SIZE)


def _fmt_d(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def build_date_keyboard(page: int, prefix: str = "tk") -> InlineKeyboardMarkup:
    all_d = _all_dates()
    tp = total_pages()
    page = max(0, min(page, tp - 1))
    chunk = all_d[page * PAGE_SIZE : page * PAGE_SIZE + PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(chunk), 2):
        row = []
        for j in range(i, min(i + 2, len(chunk))):
            d = chunk[j]
            row.append(InlineKeyboardButton(_fmt_d(d), callback_data=f"{prefix}:d:{_ymd(d)}"))
        rows.append(row)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Trước", callback_data=f"{prefix}:p:{page - 1}"))
    if page < tp - 1:
        nav.append(InlineKeyboardButton("Sau ➡️", callback_data=f"{prefix}:p:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def _tongket_thuc_te_u_from_row(row: dict) -> float:
    """Ưu tiên cột mới; bản ghi cũ có thể lưu nhầm ở tong_ra."""
    v = row.get("tong_vao_thuc_te_u")
    if v is not None:
        return float(v)
    return float(row.get("tong_ra") or 0)


def format_saved_tongket(row: dict) -> str:
    ng = row.get("ngay")
    if isinstance(ng, date) and not isinstance(ng, datetime):
        ds = ng.strftime("%d/%m/%Y")
    elif isinstance(ng, datetime):
        ds = ng.strftime("%d/%m/%Y")
    else:
        ds = str(ng)[:10]
    gia = float(row["gia_u_set"])
    hien_tai = float(row["tong_vao"])
    thuc_te = _tongket_thuc_te_u_from_row(row)
    ln = float(row["loi_nhuan"])
    ck_vnd = float(row.get("loi_nhuan_chiet_khau_vnd") or 0)
    sn = int(row.get("so_nhom_tham_gia") or 0)
    return (
        f"📋 <b>Đã có tongket</b> ngày {ds}\n"
        f"• Giá U thực tế (bạn nhập): <code>{gia:,.0f}</code> VND / 1 USDT\n"
        f"• <b>Tổng vào hiện tại (U)</b> — cộng số U trong ngoặc «Tổng vào VND → U» trên close: <code>{hien_tai:,.2f}</code> U\n"
        f"• <b>Tổng vào thực tế (U)</b> — Σ VND vào thô ÷ giá U: <code>{thuc_te:,.2f}</code> U\n"
        f"• <b>Lợi nhuận (U)</b> = thực tế − hiện tại: <code>{ln:,.2f}</code> U\n"
        f"• <b>Lợi nhuận từ chiết khấu (VND)</b> — Σ (VND vào thô × CKV%) từng phiên: <code>{Session._format_vnd(ck_vnd)}</code>\n"
        f"• Số nhóm tham gia: <code>{sn}</code>"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "📅 <b>Bạn muốn tổng kết kế toán ngày nào?</b>\n\n"
        "Chỉ tính các phiên <b>đã đóng</b> (theo ngày mở phiên / business_date).\n"
        "Chọn ngày bên dưới — danh sách ~6 tháng gần nhất, có phân trang.",
        reply_markup=build_date_keyboard(0),
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "🤖 <b>Bot tổng kế toán</b>\n\n"
        "• /start — chọn ngày (nút), nhập <b>giá U thực tế</b> khi được hỏi; nếu ngày đã có tongket: "
        "<b>Có</b> = nhập giá mới, <b>Không</b> = giữ giá U đã lưu và cập nhật lại số theo phiên.\n"
        "• /tong_thang — tổng kết theo khoảng ngày (chọn ngày bắt đầu → ngày kết thúc); "
        "chỉ tính <b>phiên chuẩn</b> (nhóm đóng bằng lệnh <code>closeday</code> trên bot nhóm).\n"
        "• Chỉ lấy phiên <b>đã đóng</b> (<code>close_at</code> có giá trị) đúng ngày mở phiên; "
        "<b>mỗi nhóm chỉ tính một phiên</b> — phiên có id lớn nhất trong ngày (phiên tạo sau cùng trong các phiên đã đóng của ngày đó).\n"
        "• <b>Tổng vào hiện tại (U)</b> = cộng số U trong ngoặc của dòng «Tổng vào VND → U» trên tin close từng nhóm (theo tỉ giá/CK từng phiên).\n"
        "• <b>Tổng vào thực tế (U)</b> = tổng VND vào thô (số trước «₫» trên dòng đó) ÷ giá U bạn nhập.\n"
        "• <b>Lợi nhuận (U)</b> = thực tế − hiện tại (ví dụ 2 nhóm: (15M+10M)/giáU − (U₁+U₂) từ ngoặc close).\n"
        "• <b>Lợi nhuận từ chiết khấu (VND)</b> = Σ trên từng phiên đóng: (VND vào thô trên dòng «Tổng vào VND → U») × (CKV % phiên) / 100.\n"
        "• Sau khi lưu tongket, tin xác nhận có <b>chi tiết theo nhóm</b> (tên nhóm logic từ /start_group): VND vào, U hiện tại, U thực tế, LN (U), LN CK (VND) từng nhóm + dòng tổng cộng.\n\n"
        "<i>Lưu DB: <code>tong_vao</code> = hiện tại U, <code>tong_vao_thuc_te_u</code> = thực tế U, <code>loi_nhuan_chiet_khau_vnd</code> = tổng lợi nhuận CK (VND); cột <code>tong_ra</code> không dùng (luôn 0).</i>",
        parse_mode="HTML",
    )


async def _safe_edit(q, text: str, **kwargs):
    try:
        await q.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "not modified" in str(e).lower() or "message is not modified" in str(e).lower():
            return
        try:
            await q.message.reply_text(text, **kwargs)
        except Exception:
            logger.exception("safe_edit fallback failed")


async def show_date_picker_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    q = update.callback_query
    tp = total_pages()
    page = max(0, min(page, tp - 1))
    text = (
        "📅 <b>Chọn ngày tổng kết kế toán</b> (phiên đã đóng)\n\n"
        f"<i>Trang {page + 1}/{tp}</i> — chọn một ngày:"
    )
    await _safe_edit(
        q,
        text,
        reply_markup=build_date_keyboard(page),
        parse_mode="HTML",
    )


async def tongket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data:
        return
    await q.answer()
    parts = q.data.split(":")
    if len(parts) < 2 or parts[0] != "tk":
        return
    tag = parts[1]

    if tag == "p" and len(parts) == 3:
        await show_date_picker_page(update, context, int(parts[2]))
        return

    if tag == "x":
        context.user_data.pop("tongket_wait", None)
        await _safe_edit(q, "Đã hủy. Gõ /start để chọn lại.", reply_markup=None)
        return

    if tag == "d" and len(parts) == 3:
        d = _parse_ymd(parts[2])
        row = await asyncio.to_thread(get_tongket_row, d)
        if row:
            text = (
                format_saved_tongket(row)
                + "\n\n<b>Bạn muốn nhập giá U mới không?</b>\n"
                "<i>• Có — nhập giá U mới rồi tính lại.\n"
                "• Không — giữ giá U đã lưu và <b>cập nhật lại</b> số liệu tongket theo phiên đã đóng hiện tại.</i>"
            )
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Có", callback_data=f"tk:yR:{parts[2]}"),
                        InlineKeyboardButton("❌ Không", callback_data=f"tk:nR:{parts[2]}"),
                    ]
                ]
            )
        else:
            text = (
                f"📆 Ngày <b>{_fmt_d(d)}</b>\n\n"
                "Chưa có dữ liệu trong bảng <code>tongket</code>.\n\n"
                "<b>Tổng kết ngày này?</b>"
            )
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Có", callback_data=f"tk:yN:{parts[2]}"),
                        InlineKeyboardButton("❌ Không", callback_data=f"tk:nN:{parts[2]}"),
                    ]
                ]
            )
        await _safe_edit(q, text, reply_markup=kb, parse_mode="HTML")
        return

    if tag == "nN" and len(parts) == 3:
        await _safe_edit(q, "Đã hủy. Gõ /start để chọn ngày khác.", reply_markup=None)
        return

    if tag == "nR" and len(parts) == 3:
        d = _parse_ymd(parts[2])
        row = await asyncio.to_thread(get_tongket_row, d)
        if not row:
            await _safe_edit(
                q,
                "Không còn bản ghi tongket cho ngày này. Gõ /start để chọn lại.",
                reply_markup=None,
            )
            return
        gia = float(row["gia_u_set"])
        if gia <= 0:
            await _safe_edit(
                q,
                "⚠️ Giá U đã lưu không hợp lệ. Chọn «Có» để nhập giá U mới.",
                reply_markup=None,
            )
            return
        try:
            fields = await asyncio.to_thread(persist_tongket_for_day, d, gia)
        except Exception:
            logger.exception("persist_tongket_for_day keep gia")
            await _safe_edit(q, "⚠️ Lỗi khi tính lại / lưu tongket.", reply_markup=None)
            return
        context.user_data.pop("tongket_wait", None)
        lines = _lines_tongket_saved(d, gia, fields, "keep_gia")
        await _safe_edit(q, "\n".join(lines), reply_markup=None, parse_mode="HTML")
        return

    if tag in ("yN", "yR") and len(parts) == 3:
        d = _parse_ymd(parts[2])
        mode = "insert" if tag == "yN" else "update"
        context.user_data["tongket_wait"] = {"kind": "gia_u", "ngay": d, "mode": mode}
        await _safe_edit(
            q,
            f"📌 Ngày <b>{_fmt_d(d)}</b>\n\n"
            "Nhập <b>giá U thực tế</b> (số VND cho <b>1 USDT</b>), ví dụ: <code>25500</code>\n"
            "(dùng để: Σ VND vào thô ÷ giá U = «tổng vào thực tế U»; lợi nhuận = thực tế − hiện tại)\n"
            "(Gửi tin nhắn text thường, không cần dấu /)",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Hủy nhập", callback_data="tk:x")]]
            ),
            parse_mode="HTML",
        )
        return


def _parse_gia_u(text: str) -> float | None:
    t = text.strip().replace(",", "").replace(" ", "")
    if not t:
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    if v <= 0:
        return None
    return v


def persist_tongket_for_day(ngay: date, gia: float) -> dict:
    """
    Đọc phiên đã đóng trong ngày, ghi / cập nhật một dòng tongket với giá U cho trước.
    Trả về dict phục vụ tin nhắn xác nhận; ném exception nếu DB lỗi.
    """
    agg = aggregate_closed_vnd_for_tongket_day(ngay)
    vnd_vao = float(agg["tong_vao_vnd"])
    hien_tai_u = float(agg["tong_vao_hien_tai_u"])
    loi_nhuan_ck_vnd = float(agg.get("loi_nhuan_chiet_khau_vnd", 0) or 0)
    so_nhom = int(agg["so_nhom"])
    so_phien = int(agg["so_phien"])

    thuc_te_u = vnd_vao / gia if gia else 0.0
    loi_nhuan = thuc_te_u - hien_tai_u

    tongket_upsert(
        ngay,
        gia,
        hien_tai_u,
        thuc_te_u,
        loi_nhuan,
        so_nhom,
        loi_nhuan_ck_vnd,
    )
    return {
        "so_phien": so_phien,
        "so_nhom": so_nhom,
        "hien_tai_u": hien_tai_u,
        "thuc_te_u": thuc_te_u,
        "loi_nhuan": loi_nhuan,
        "loi_nhuan_ck_vnd": loi_nhuan_ck_vnd,
        "vnd_vao": vnd_vao,
        "chi_tiet_nhom": list(agg.get("chi_tiet_nhom") or []),
    }


def _lines_tongket_saved(ngay: date, gia: float, fields: dict, save_mode: str) -> list[str]:
    """save_mode: insert | update | keep_gia"""
    if save_mode == "insert":
        head = f"✅ Đã lưu tongket <b>{_fmt_d(ngay)}</b>"
    elif save_mode == "keep_gia":
        head = (
            f"✅ Đã cập nhật tongket <b>{_fmt_d(ngay)}</b> "
            f"<i>(giữ giá U đã lưu: <code>{gia:,.0f}</code> VND / 1 USDT)</i>"
        )
    else:
        head = f"✅ Đã cập nhật tongket <b>{_fmt_d(ngay)}</b>"

    so_phien = int(fields["so_phien"])
    so_nhom = int(fields["so_nhom"])
    hien_tai_u = float(fields["hien_tai_u"])
    thuc_te_u = float(fields["thuc_te_u"])
    loi_nhuan = float(fields["loi_nhuan"])
    loi_nhuan_ck_vnd = float(fields["loi_nhuan_ck_vnd"])
    vnd_vao = float(fields["vnd_vao"])
    ds = _fmt_d(ngay)

    lines = [head, ""]
    if so_phien == 0:
        lines.append("⚠️ <b>Không có phiên đã đóng</b> trong ngày này — các chỉ số U = 0.")
        lines.append("")
    lines.extend(
        [
            f"• Giá U thực tế (đã lưu): <code>{gia:,.0f}</code> VND / 1 USDT",
            f"• Số phiên đã đóng: <code>{so_phien}</code> — số nhóm gộp: <code>{so_nhom}</code>",
            "",
        ]
    )

    chi = fields.get("chi_tiet_nhom") or []
    if chi:
        lines.append("<b>Chi tiết theo nhóm</b> <i>(cùng giá U đã lưu)</i>")
        for it in chi:
            tn = html.escape(str(it.get("ten_nhom") or "—"), quote=False)
            vnd_g = float(it.get("tong_vao_vnd", 0) or 0)
            u_ht_g = float(it.get("tong_vao_hien_tai_u", 0) or 0)
            ck_g = float(it.get("loi_nhuan_chiet_khau_vnd", 0) or 0)
            nsp = int(it.get("so_phien", 0) or 0)
            thuc_g = vnd_g / gia if gia else 0.0
            ln_g = thuc_g - u_ht_g
            lines.append(
                f"▸ <b>{tn}</b> — <code>{nsp}</code> phiên\n"
                f"   VND vào thô: <code>{Session._format_vnd(vnd_g)}</code> — "
                f"Hiện tại (U): <code>{u_ht_g:,.2f}</code> — "
                f"Thực tế (U): <code>{thuc_g:,.2f}</code>\n"
                f"   <b>LN (U):</b> <code>{ln_g:+,.2f}</code> — "
                f"<b>LN CK:</b> <code>{Session._format_vnd(ck_g)}</code>"
            )
        lines.append("")

    lines.extend(
        [
            f"<b>📊 Tổng vào cả ngày — {ds}</b>",
            f"• VND vào thô (Σ «Tổng vào VND → U»): <code>{Session._format_vnd(vnd_vao)}</code>",
            f"• Hiện tại (U), Σ ngoặc close: <code>{hien_tai_u:,.2f}</code> U",
            f"• Thực tế (U) @ giá đã lưu: <code>{thuc_te_u:,.2f}</code> U",
            "",
            f"<b>💰 Tổng lợi nhuận ngày {ds}</b>",
            f"<b>Lợi nhuận (U)</b> <i>(thực tế − hiện tại)</i>\n<b>→ <code>{loi_nhuan:+,.2f}</code> U</b>",
            f"<b>Lợi nhuận chiết khấu (VND)</b>\n<b>→ <code>{Session._format_vnd(loi_nhuan_ck_vnd)}</code></b>",
            "",
            "<i>Gõ /start để chọn ngày khác.</i>",
        ]
    )
    return lines


async def on_gia_u_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    w = context.user_data.get("tongket_wait")
    if not w or w.get("kind") != "gia_u":
        return

    gia = _parse_gia_u(update.message.text)
    if gia is None:
        await update.message.reply_text(
            "⚠️ Giá U không hợp lệ. Nhập một số dương (VD: 25500 — VND cho 1 USDT)."
        )
        return

    ngay: date = w["ngay"]
    mode = w.get("mode", "insert")

    try:
        fields = await asyncio.to_thread(persist_tongket_for_day, ngay, gia)
    except Exception:
        logger.exception("persist_tongket_for_day")
        await update.message.reply_text("⚠️ Lỗi khi đọc DB / tính phiên / lưu tongket.")
        return

    context.user_data.pop("tongket_wait", None)

    save_mode = "insert" if mode == "insert" else "update"
    lines = _lines_tongket_saved(ngay, gia, fields, save_mode)
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ===================== Tổng kết tháng (phiên chuẩn / closeday) =====================


def _format_ck_pct(value: float) -> str:
    if float(value) % 1 == 0:
        return f"{float(value):.0f}%"
    return f"{float(value):.2f}%".rstrip("0").rstrip(".") + "%"


def _format_tong_thang_summary(agg: dict) -> list[str]:
    start_s = _fmt_d(date.fromisoformat(agg["start_date"]))
    end_s = _fmt_d(date.fromisoformat(agg["end_date"]))
    so_phien = int(agg.get("so_phien") or 0)

    if so_phien == 0:
        return [
            f"📊 <b>TỔNG KẾT THÁNG</b> ({start_s} → {end_s})",
            "",
            "⚠️ Không có phiên chuẩn (closeday) trong khoảng này.",
            "",
            "<i>Gõ /tong_thang để chọn khoảng khác.</i>",
        ]

    tv = float(agg.get("tong_vao", 0) or 0)

    lines = [
        f"📊 <b>TỔNG KẾT THÁNG</b> ({start_s} → {end_s})",
        f"Số phiên chuẩn: <code>{so_phien}</code>",
        "",
        f"💰 Tổng vào: <code>{Session._format_vnd(tv)}</code>",
        "",
        "──────────",
        "<b>Chi tiết theo ngày</b>",
        "",
    ]

    for day_block in agg.get("by_date") or []:
        d: date = day_block["date"]
        ds = _fmt_d(d)
        lines.append(f"📆 <b>{ds}</b>")
        for sess in day_block.get("sessions") or []:
            label = html.escape(str(sess.get("label") or "—"), quote=False)
            sid = int(sess.get("session_id") or 0)
            ckv = float(sess.get("ckv") or 0)
            ckr = float(sess.get("ckr") or 0)
            stv = float(sess.get("tong_vao") or 0)
            str_ = float(sess.get("tong_ra") or 0)
            stv_u = float(sess.get("tong_vao_usdt") or 0)
            str_u = float(sess.get("tong_ra_usdt") or 0)
            sdt = float(sess.get("doanh_thu_usdt") or 0)
            lines.append(
                f"  ▸ <b>Phiên #{sid}</b> — {label}\n"
                f"     CKV: <code>{_format_ck_pct(ckv)}</code> | CKR: <code>{_format_ck_pct(ckr)}</code>\n"
                f"     💰 Vào: <code>{Session._format_vnd(stv)}</code> (<code>{stv_u:,.2f}</code> U)\n"
                f"     💸 Chi: <code>{Session._format_vnd(str_)}</code> (<code>{str_u:,.2f}</code> U)\n"
                f"     📊 Doanh thu: <code>{sdt:,.2f}</code> USDT"
            )

        day_tot = day_block.get("day_totals") or {}
        if day_tot:
            dtv = float(day_tot.get("tong_vao", 0) or 0)
            dtr = float(day_tot.get("tong_ra", 0) or 0)
            dtv_u = float(day_tot.get("tong_vao_usdt_vnd", 0) or 0)
            dtr_u = float(day_tot.get("tong_ra_usdt_vnd", 0) or 0)
            ddt = float(day_tot.get("doanh_thu_usdt", 0) or 0)
            lines.append(
                f"  📊 <b>Tổng ngày {ds}</b>: "
                f"vào <code>{Session._format_vnd(dtv)}</code> (<code>{dtv_u:,.2f}</code> U) — "
                f"chi <code>{Session._format_vnd(dtr)}</code> (<code>{dtr_u:,.2f}</code> U) — "
                f"doanh thu <code>{ddt:,.2f}</code> USDT"
            )
        lines.append("")

    lines.append("<i>Gõ /tong_thang để chọn khoảng khác.</i>")
    return lines


async def _reply_tong_thang_result(q, agg: dict) -> None:
    lines = _format_tong_thang_summary(agg)
    text = "\n".join(lines)
    chunks = split_telegram_text_chunks(text)
    await _safe_edit(q, chunks[0], reply_markup=None, parse_mode="HTML")
    for part in chunks[1:]:
        await q.message.reply_text(part, parse_mode="HTML")


def build_tong_thang_start_keyboard(page: int) -> InlineKeyboardMarkup:
    kb = build_date_keyboard(page, prefix="tt:s")
    rows = list(kb.inline_keyboard)
    rows.append([InlineKeyboardButton("❌ Hủy", callback_data="tt:x")])
    return InlineKeyboardMarkup(rows)


def build_tong_thang_end_keyboard(page: int) -> InlineKeyboardMarkup:
    kb = build_date_keyboard(page, prefix="tt:e")
    rows = list(kb.inline_keyboard)
    rows.append([InlineKeyboardButton("❌ Hủy", callback_data="tt:x")])
    return InlineKeyboardMarkup(rows)


async def cmd_tong_thang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    context.user_data.pop("tong_thang_wait", None)
    context.user_data.pop("tongket_wait", None)
    tp = total_pages()
    try:
        await msg.reply_text(
            "📅 <b>Tổng kết tháng — phiên chuẩn (closeday)</b>\n\n"
            "Bước 1/2: Chọn <b>ngày bắt đầu</b> khoảng tính toán.\n"
            f"<i>Danh sách ~{MONTHS_BACK} tháng gần nhất, trang 1/{tp}</i>",
            reply_markup=build_tong_thang_start_keyboard(0),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("cmd_tong_thang reply failed")
        await msg.reply_text("⚠️ Không gửi được menu tổng kết tháng. Thử lại sau vài giây.")


async def tong_thang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data:
        return
    await q.answer()
    parts = q.data.split(":")
    if len(parts) < 2 or parts[0] != "tt":
        return
    tag = parts[1]

    if tag == "x":
        context.user_data.pop("tong_thang_wait", None)
        await _safe_edit(q, "Đã hủy tổng kết tháng. Gõ /tong_thang hoặc /start.", reply_markup=None)
        return

    if tag == "s" and parts[2] == "p" and len(parts) == 4:
        page = int(parts[3])
        tp = total_pages()
        page = max(0, min(page, tp - 1))
        await _safe_edit(
            q,
            "📅 <b>Tổng kết tháng — phiên chuẩn</b>\n\n"
            "Bước 1/2: Chọn <b>ngày bắt đầu</b>.\n"
            f"<i>Trang {page + 1}/{tp}</i>",
            reply_markup=build_tong_thang_start_keyboard(page),
            parse_mode="HTML",
        )
        return

    if tag == "s" and parts[2] == "d" and len(parts) == 4:
        start_d = _parse_ymd(parts[3])
        context.user_data["tong_thang_wait"] = {"start": start_d}
        tp = total_pages()
        await _safe_edit(
            q,
            "📅 <b>Tổng kết tháng — phiên chuẩn</b>\n\n"
            f"Đã chọn bắt đầu: <b>{_fmt_d(start_d)}</b>\n\n"
            "Bước 2/2: Chọn <b>ngày kết thúc</b>.\n"
            f"<i>Trang 1/{tp}</i>",
            reply_markup=build_tong_thang_end_keyboard(0),
            parse_mode="HTML",
        )
        return

    if tag == "e" and parts[2] == "p" and len(parts) == 4:
        w = context.user_data.get("tong_thang_wait") or {}
        if not w.get("start"):
            await _safe_edit(q, "⚠️ Phiên chọn đã hết hạn. Gõ /tong_thang để bắt đầu lại.", reply_markup=None)
            return
        page = int(parts[3])
        tp = total_pages()
        page = max(0, min(page, tp - 1))
        start_d = w["start"]
        await _safe_edit(
            q,
            "📅 <b>Tổng kết tháng — phiên chuẩn</b>\n\n"
            f"Ngày bắt đầu: <b>{_fmt_d(start_d)}</b>\n\n"
            "Bước 2/2: Chọn <b>ngày kết thúc</b>.\n"
            f"<i>Trang {page + 1}/{tp}</i>",
            reply_markup=build_tong_thang_end_keyboard(page),
            parse_mode="HTML",
        )
        return

    if tag == "e" and parts[2] == "d" and len(parts) == 4:
        w = context.user_data.get("tong_thang_wait") or {}
        start_d = w.get("start")
        if not start_d:
            await _safe_edit(q, "⚠️ Phiên chọn đã hết hạn. Gõ /tong_thang để bắt đầu lại.", reply_markup=None)
            return
        end_d = _parse_ymd(parts[3])
        if end_d < start_d:
            await _safe_edit(
                q,
                f"⚠️ Ngày kết thúc <b>{_fmt_d(end_d)}</b> phải ≥ ngày bắt đầu <b>{_fmt_d(start_d)}</b>.\n"
                "Chọn lại ngày kết thúc:",
                reply_markup=build_tong_thang_end_keyboard(0),
                parse_mode="HTML",
            )
            return
        try:
            agg = await asyncio.to_thread(aggregate_standard_sessions_in_range, start_d, end_d)
        except Exception:
            logger.exception("aggregate_standard_sessions_in_range")
            await _safe_edit(q, "⚠️ Lỗi khi đọc DB / tính phiên chuẩn.", reply_markup=None)
            return
        context.user_data.pop("tong_thang_wait", None)
        await _reply_tong_thang_result(q, agg)
        return
