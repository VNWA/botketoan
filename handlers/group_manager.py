from telegram import Update
from telegram.ext import ContextTypes
from database.models import (
    add_group, list_groups, get_group, update_group_field, delete_group,
    set_current_group, get_current_group, get_user
)
from utils import role_middleware, get_user_role

@role_middleware(["super_admin","admin"])
async def new_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.username)
    if len(context.args) < 1:
        await update.message.reply_text("⚠️ Cú pháp: /new_group <tên nhóm>")
        return
    name = " ".join(context.args)
    add_group(db_user[0], name)
    await update.message.reply_text(f"✅ Đã tạo nhóm: {name}")

@role_middleware(["super_admin","admin"])
async def list_groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    role = get_user_role(user.username)
    db_user = get_user(user.username)

    groups = list_groups(db_user[0] if role=="admin" else None)
    if not groups:
        await update.message.reply_text("📭 Chưa có nhóm nào.")
        return

    text = "📂 Danh sách nhóm:\n"
    for g in groups:
        text += f"- ID {g[0]}: {g[2]} (Admin ID: {g[1]})\n"
    await update.message.reply_text(text)

@role_middleware(["super_admin","admin"])
async def start_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.username)
    if len(context.args) != 1:
        await update.message.reply_text("⚠️ Cú pháp: /start_group <group_id>")
        return
    
    group_id = int(context.args[0])
    g = get_group(group_id)
    if not g:
        await update.message.reply_text("⚠️ Nhóm không tồn tại.")
        return

    role = get_user_role(user.username)
    if role=="admin" and g[1] != db_user[0]:
        await update.message.reply_text("❌ Bạn không có quyền thao tác nhóm này.")
        return

    chat_id = update.effective_chat.id
    set_current_group(db_user[0], chat_id, group_id)
    await update.message.reply_text(f"✅ Đã chọn nhóm: {g[2]}")

@role_middleware(["super_admin","admin"])
async def edit_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.username)
    chat_id = update.effective_chat.id
    group_id = get_current_group(db_user[0], chat_id)

    if not group_id:
        await update.message.reply_text("⚠️ Chưa chọn nhóm. Dùng /start_group <id> trước.")
        return

    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Cú pháp: /edit_group <field> <value>")
        return

    field = context.args[0]
    value = context.args[1]
    allowed_fields = ["name","chiet_khau_vao","chiet_khau_ra","ti_gia_mua","ti_gia_ban"]
    if field not in allowed_fields:
        await update.message.reply_text(f"⚠️ Field không hợp lệ. Cho phép: {', '.join(allowed_fields)}")
        return

    update_group_field(group_id, field, value)
    await update.message.reply_text(f"✅ Đã cập nhật {field} = {value}")
    
@role_middleware(["super_admin","admin"])
async def view_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xem thông tin chi tiết của group hiện tại (hoặc theo ID truyền vào)"""
    user = update.effective_user
    db_user = get_user(user.username)
    chat_id = update.effective_chat.id

    # Nếu truyền group_id trực tiếp: /view_group <id>
    if len(context.args) == 1:
        try:
            group_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("⚠️ ID nhóm không hợp lệ.")
            return
    else:
        # Nếu không truyền, lấy group đang active trong session
        group_id = get_current_group(db_user[0], chat_id)
        if not group_id:
            await update.message.reply_text("⚠️ Chưa chọn nhóm. Dùng /start_group <id> trước hoặc truyền ID nhóm.")
            return

    g = get_group(group_id)
    if not g:
        await update.message.reply_text("⚠️ Nhóm không tồn tại.")
        return

    # Kiểm tra quyền admin
    role = get_user_role(user.username)
    if role == "admin" and g[1] != db_user[0]:
        await update.message.reply_text("❌ Bạn không có quyền xem nhóm này.")
        return

    # Hiển thị thông tin chi tiết
    text = (
        f"📌 Thông tin nhóm:\n"
        f"- ID: {g[0]}\n"
        f"- Tên nhóm: {g[2]}\n"
        f"- Admin ID: {g[1]}\n"
        f"- Chiết khấu vào: {g[3]}\n"
        f"- Chiết khấu ra: {g[4]}\n"
        f"- Tỉ giá mua: {g[5]}\n"
        f"- Tỉ giá bán: {g[6]}"
    )
    await update.message.reply_text(text)
@role_middleware(["super_admin","admin"])
async def delete_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.username)
    chat_id = update.effective_chat.id
    group_id = get_current_group(db_user[0], chat_id)

    if not group_id:
        await update.message.reply_text("⚠️ Chưa chọn nhóm. Dùng /start_group <id> trước.")
        return

    delete_group(group_id)
    await update.message.reply_text("✅ Đã xóa nhóm.")

