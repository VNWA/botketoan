from telegram import Update
from telegram.ext import ContextTypes
from database.models import DB
from utils import is_super_admin, auth_required, SUPER_ADMIN


class User:
    @staticmethod
    @auth_required
    async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if len(context.args) != 1:
            return await update.message.reply_text("⚠️ Dùng lệnh: /add_user <@username>")

        username = context.args[0].replace("@", "")

        # Chỉ super admin mới được thêm user
        if not is_super_admin(update.effective_user.username):
            return await update.message.reply_text("❌ Bạn không có quyền thực hiện lệnh này.")

        # Thêm user mới (nếu chưa tồn tại)
        user = DB.table("users").first_or_create({"username": username})

        await update.message.reply_text(f"✅ @{username} đã được thêm vào hệ thống")

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

        text = "👑 Danh sách user:\n" + "\n".join(
            [f"- @{u['username']}" for u in regular_users]
        )

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
        
        # Lấy super admin từ env
        super_admin_username = None
        if SUPER_ADMIN:
            super_admin_username = SUPER_ADMIN.lower()

        if not admins and not super_admin_username:
            return await update.message.reply_text("📭 Chưa có admin tổng nào.")

        text_lines = ["👑 Danh sách admin tổng:"]
        
        # Thêm super admin từ env nếu có
        if super_admin_username:
            text_lines.append(f"- @{SUPER_ADMIN} (Super Admin từ cấu hình)")
        
        # Thêm các admin từ database
        for admin in admins:
            if 'username' in admin:
                username = admin['username']
                # Không hiển thị trùng nếu đã có trong super admin
                if not super_admin_username or username.lower() != super_admin_username:
                    text_lines.append(f"- @{username}")

        await update.message.reply_text("\n".join(text_lines))
