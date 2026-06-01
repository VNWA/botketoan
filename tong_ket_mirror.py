"""
Gửi bản sao nội dung chốt phiên (close) sang nhóm tổng kế toán qua bot token riêng (HTTP Bot API).

Chỉ cần TONG_KET_TOAN_TOKEN + TONG_KET_TOAN_CHAT_ID trong .env — không cần chạy process bot thứ hai.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

TG_TEXT_LIMIT = 4096


def _chunk_plain_text(text: str, chunk_size: int = TG_TEXT_LIMIT) -> List[str]:
    """Chia tin theo dòng để không vượt giới hạn Telegram."""
    if len(text) <= chunk_size:
        return [text]
    lines = text.split("\n")
    chunks: List[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= chunk_size:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(line) <= chunk_size:
            current = line
        else:
            for i in range(0, len(line), chunk_size):
                chunks.append(line[i : i + chunk_size])
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _build_header(
    *,
    chat_id: int,
    telegram_chat_title: Optional[str],
    logical_group_name: Optional[str],
) -> str:
    title = (telegram_chat_title or "").strip() or "(chat không có tên)"
    lines = [
        "📢 Chốt phiên — bot kế toán nhóm",
        f"🗂 Nhóm Telegram: {title}",
        f"🆔 chat_id: {chat_id}",
    ]
    if logical_group_name:
        lines.append(f"📌 Nhóm kế toán (đã gán /start_group): {logical_group_name}")
    lines.append("")
    lines.append("──────────")
    lines.append("")
    return "\n".join(lines)


async def mirror_group_close_to_summary_hub(
    *,
    chat_id: int,
    telegram_chat_title: Optional[str],
    logical_group_name: Optional[str],
    close_body: str,
) -> None:
    """
    Gửi nội dung close (đã format) sang chat tổng kế toán.
    Nếu thiếu TOKEN hoặc CHAT_ID thì bỏ qua im lặng.
    """
    token = (os.getenv("TONG_KET_TOAN_TOKEN") or "").strip()
    raw_dest = (os.getenv("TONG_KET_TOAN_CHAT_ID") or "").strip()
    if not token or not raw_dest:
        return
    try:
        dest_id = int(raw_dest)
    except ValueError:
        logger.warning("TONG_KET_TOAN_CHAT_ID không phải số hợp lệ, bỏ qua gửi tổng kế toán")
        return

    header = _build_header(
        chat_id=chat_id,
        telegram_chat_title=telegram_chat_title,
        logical_group_name=logical_group_name,
    )
    full = header + (close_body or "").strip()
    chunks = _chunk_plain_text(full)
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    async with httpx.AsyncClient(timeout=45.0) as client:
        for idx, chunk in enumerate(chunks):
            part = chunk
            if len(chunks) > 1:
                part = f"(phần {idx + 1}/{len(chunks)})\n\n" + chunk
                if len(part) > TG_TEXT_LIMIT:
                    part = part[: TG_TEXT_LIMIT - 20] + "\n…(cắt bớt)"
            try:
                r = await client.post(
                    url,
                    json={
                        "chat_id": dest_id,
                        "text": part,
                        "disable_web_page_preview": True,
                    },
                )
            except httpx.HTTPError:
                logger.exception("Lỗi mạng khi gửi tổng kế toán (phần %s)", idx + 1)
                return
            if r.status_code != 200:
                logger.error(
                    "Telegram tổng kế toán trả lỗi HTTP %s: %s",
                    r.status_code,
                    (r.text or "")[:500],
                )
                return
            data = r.json()
            if not data.get("ok"):
                logger.error("Telegram tổng kế toán ok=false: %s", data)
                return


def split_telegram_text_chunks(text: str, chunk_size: int = TG_TEXT_LIMIT) -> list[str]:
    """Chia tin để gửi nhiều message (dùng chung bot tổng / mirror)."""
    return _chunk_plain_text(text, chunk_size)
