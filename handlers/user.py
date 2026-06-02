from telegram import Update
from telegram.ext import ContextTypes
from database.models import DB, set_user_role
from utils import is_super_admin, auth_required, SUPER_ADMIN, SUPER_ADMIN2


class User:
    @staticmethod
    @auth_required
    async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = list(context.args or [])
        if not args:
            return await update.message.reply_text(
                "⚠️ Dùng lệnh: /add_user <tên> [user|admin]\n"
                "Ví dụ:\n"
                "• /add_user linganh  → user thường\n"
                "• /add_user ling anh admin  → admin tổng (bảng admins, như add_admin)\n"
                "Token cuối nếu là user / admin thì là role; mặc định không truyền = user."
            )

        valid_roles = frozenset(("user", "admin"))
        if args[-1].strip().lower() in valid_roles and len(args) >= 2:
            role_arg = args[-1].strip().lower()
            name_tokens = args[:-1]
        else:
            role_arg = "user"
            name_tokens = args

        username = " ".join(name_tokens).replace("@", "").strip()
        if not username:
            return await update.message.reply_text("⚠️ Username không hợp lệ.")

        if not is_super_admin(update.effective_user.username):
            return await update.message.reply_text("❌ Bạn không có quyền thực hiện lệnh này.")

        existing = DB.table("users").where("username", username).first()
        if not existing:
            DB.table("users").insert({"username": username})

        if role_arg == "admin":
            set_user_role(username, "user")
            if not DB.table("admins").where("username", username).first():
                DB.table("admins").insert({"username": username})
            await update.message.reply_text(
                f"✅ @{username} — <b>admin tổng</b> (đã ghi bảng admins, quyền như /add_admin).",
                parse_mode="HTML",
            )
        else:
            set_user_role(username, "user")
            DB.table("admins").where("username", username).delete()
            await update.message.reply_text(
                f"✅ @{username} — role <b>user</b> (đã gỡ admin tổng nếu có).",
                parse_mode="HTML",
            )

    @staticmethod
    @auth_required
    async def list(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Chỉ super admin được phép xem danh sách user
        if not is_super_admin(update.effective_user.username):
            return await update.message.reply_text("❌ Bạn không có quyền thực hiện lệnh này.")

        # Lấy danh sách tất cả user
        all_users = DB.table("users").get()
        
        # Lấy danh sách admin để loại trừ
        admins = DB.table("admins").get()
        admin_usernames = {admin['username'] for admin in admins if 'username' in admin}
        
        # Lọc ra các user không phải admin
        regular_users = [u for u in all_users if 'username' in u and u['username'] not in admin_usernames]

        if not regular_users:
            return await update.message.reply_text("📭 Chưa có user nào (không tính admin).")

        text = "👑 Danh sách user:\n" + "\n".join(f"- @{u['username']}" for u in regular_users)

        await update.message.reply_text(text)

    @staticmethod
    @auth_required
    async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if len(context.args) != 1:
            return await update.message.reply_text("⚠️ Dùng lệnh: /delete_user <@username>")

        username = context.args[0].replace("@", "")

        # Chỉ super admin được phép xóa user
        if not is_super_admin(update.effective_user.username):
            return await update.message.reply_text("❌ Bạn không có quyền thực hiện lệnh này.")

        # Check if user exists
        db_user = DB.table("users").where("username", username).first()
        if not db_user:
            return await update.message.reply_text(f"⚠️ Không tìm thấy user @{username}.")

        user_id = db_user["id"]

        # Check if user is being used in sessions or transactions
        sessions_count = DB.table("sessions").where("user_id", user_id).count()
        transactions_count = DB.table("transactions").where("user_id", user_id).count()

        if sessions_count > 0 or transactions_count > 0:
            return await update.message.reply_text(
                f"⚠️ Không thể xóa user @{username} vì đang có dữ liệu liên quan:\n"
                f"- Số phiên (sessions): {sessions_count}\n"
                f"- Số giao dịch (transactions): {transactions_count}\n\n"
                f"💡 Để xóa user này, bạn cần xóa tất cả phiên và giao dịch liên quan trước."
            )

        # Safe to delete
        deleted = DB.table("users").where("username", username).delete()

        if deleted:
            await update.message.reply_text(f"✅ @{username} đã bị xóa khỏi hệ thống")
        else:
            await update.message.reply_text(f"⚠️ Không thể xóa user @{username}.")

    @staticmethod
    @auth_required
    async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if len(context.args) != 1:
            return await update.message.reply_text("⚠️ Dùng lệnh: /add_admin <@username>")

        username = context.args[0].replace("@", "")

        # Chỉ super admin được phép thêm admin tổng
        if not is_super_admin(update.effective_user.username):
            return await update.message.reply_text("❌ Bạn không có quyền thực hiện lệnh này.")

        # Check if user exists first
        db_user = DB.table("users").where("username", username).first()
        if not db_user:
            return await update.message.reply_text(f"⚠️ User @{username} chưa tồn tại trong hệ thống. Vui lòng thêm user trước.")

        # Add admin (if not exists)
        existing_admin = DB.table("admins").where("username", username).first()
        if existing_admin:
            return await update.message.reply_text(f"⚠️ @{username} đã là admin tổng rồi.")

        DB.table("admins").insert({"username": username})
        await update.message.reply_text(f"✅ @{username} đã được thêm làm admin tổng")

    @staticmethod
    @auth_required
    async def list_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Chỉ super admin được phép xem danh sách admin
        if not is_super_admin(update.effective_user.username):
            return await update.message.reply_text("❌ Bạn không có quyền thực hiện lệnh này.")

        # Lấy danh sách admin từ database
        admins = DB.table("admins").get()
        
        # Super admin từ .env (có thể nhiều biến)
        env_supers: list[tuple[str, str]] = []
        if SUPER_ADMIN:
            env_supers.append((SUPER_ADMIN.lower(), SUPER_ADMIN))
        if SUPER_ADMIN2:
            low2 = SUPER_ADMIN2.lower()
            if not any(low2 == e[0] for e in env_supers):
                env_supers.append((low2, SUPER_ADMIN2))

        if not admins and not env_supers:
            return await update.message.reply_text("📭 Chưa có admin tổng nào.")

        text_lines = ["👑 Danh sách admin tổng:"]

        if len(env_supers) == 1:
            text_lines.append(f"- @{env_supers[0][1]} (Super Admin từ cấu hình .env)")
        else:
            for i, (_, disp) in enumerate(env_supers, start=1):
                text_lines.append(f"- @{disp} (Super Admin {i} từ cấu hình .env)")

        env_lower = {e[0] for e in env_supers}

        # Thêm các admin từ database
        for admin in admins:
            if "username" in admin:
                username = admin["username"]
                if username.lower() not in env_lower:
                    text_lines.append(f"- @{username}")

        await update.message.reply_text("\n".join(text_lines))
