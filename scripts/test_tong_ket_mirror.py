#!/usr/bin/env python3
"""
Gửi một tin thử vào nhóm tổng kế toán (đọc TONG_KET_TOAN_* từ .env).

Chạy từ thư mục gốc repo:
  python scripts/test_tong_ket_mirror.py
"""
from __future__ import annotations

import asyncio
import os
import sys

# Thư mục gốc repo trên sys.path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


async def main() -> None:
    env_path = os.path.join(ROOT, ".env")
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
    except ModuleNotFoundError:
        print("Cảnh báo: chưa cài python-dotenv; chỉ dùng biến môi trường đã export sẵn.")

    from tong_ket_mirror import mirror_group_close_to_summary_hub

    token = (os.getenv("TONG_KET_TOAN_TOKEN") or "").strip()
    chat = (os.getenv("TONG_KET_TOAN_CHAT_ID") or "").strip()
    if not token or not chat:
        print("Thiếu TONG_KET_TOAN_TOKEN hoặc TONG_KET_TOAN_CHAT_ID trong .env")
        sys.exit(1)

    await mirror_group_close_to_summary_hub(
        chat_id=-1,
        telegram_chat_title="TEST_SCRIPT",
        logical_group_name="(tin thử)",
        close_body="🧪 Đây là tin thử từ scripts/test_tong_ket_mirror.py — nếu thấy tin này là gửi OK.",
    )
    print("Đã gọi mirror_group_close_to_summary_hub xong. Kiểm tra nhóm tổng kế toán.")


if __name__ == "__main__":
    asyncio.run(main())
