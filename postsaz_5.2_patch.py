#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import py_compile
import re
import shutil
from datetime import datetime
from pathlib import Path


ARCHIVE_HELPERS_AND_FUNCTION = r'''def require_archive_bot_api():
    """Return the running Aiogram Bot instance used by the panel."""
    for name in ("bot", "api_bot", "panel_bot"):
        candidate = globals().get(name)
        if candidate is not None and hasattr(candidate, "copy_message"):
            return candidate
    raise RuntimeError("نمونه Bot API پنل پیدا نشد؛ سرویس را ری‌استارت کن.")


def archive_bot_api_error_hint(error: Exception) -> str:
    raw = str(error).strip() or error.__class__.__name__
    low = raw.lower()
    if "chat not found" in low:
        return "Bot API کانال مبدا یا کانال آرشیو را پیدا نکرد. ربات را در هر دو کانال عضو/ادمین کن."
    if "forbidden" in low or "not enough rights" in low or "not permitted" in low or "can't write" in low or "cannot write" in low:
        return "Bot API مجوز لازم را ندارد. ربات باید در کانال آرشیو اجازه ارسال و در کانال مبدا دسترسی خواندن داشته باشد."
    if "message to copy not found" in low or "message_id_invalid" in low or "message identifier is not specified" in low:
        return "پیام مبدا برای Bot API قابل مشاهده نیست. ربات API را به کانال مبدا اضافه کن."
    return raw


async def cleanup_partial_archive_bot_api_copy(api_bot, archive_chat_id: int, sent_ids: list[int], job_id: int):
    """Best-effort rollback if copying the package stops halfway."""
    for sent_id in reversed(sent_ids):
        try:
            await api_bot.delete_message(chat_id=archive_chat_id, message_id=int(sent_id))
        except Exception as cleanup_error:
            logging.warning(
                "ARCHIVE_BOT_API_PARTIAL_CLEANUP_FAILED job=%s archive_chat_id=%s message_id=%s error=%s",
                job_id,
                archive_chat_id,
                sent_id,
                cleanup_error,
            )


async def send_original_package_to_archive(
    job_id: int,
    source: sqlite3.Row,
    banner_msg,
    file_messages: list[Any],
    flow_type: Optional[str],
    owner_id: Optional[int] = None,
):
    """
    v5.2: Archive the original package only with Bot API copy_message.

    No Telethon/user-account fallback is allowed here. The API bot must be able
    to read the source channel and write to the archive channel.
    """
    owner_id = int(owner_id if owner_id is not None else current_owner_id())
    settings = get_flow_archive_settings(flow_type, owner_id=owner_id)
    if int(settings["archive_enabled"] or 0) != 1 or not settings["archive_chat_id"]:
        return

    item = get_archive_item(int(source["id"]), int(banner_msg.id), owner_id=owner_id)
    if item and item["archive_sent_at"]:
        logging.info("ARCHIVE_POST_PACKAGE_SKIPPED_ALREADY_SENT job=%s item=%s", job_id, item["id"])
        return

    archive_chat_id = int(settings["archive_chat_id"])
    destination_id = get_flow_destination_chat_id(flow_type, owner_id=owner_id)
    if destination_id is not None and int(destination_id) == archive_chat_id:
        error = "archive channel must be separate from destination channel"
        update_job(job_id, archive_error=error)
        update_archive_item_archive_status(job_id, archive_error=error)
        logging.error("ARCHIVE_POST_PACKAGE_FAILED job=%s reason=%s", job_id, error)
        return

    source_chat_id = int(source["peer_id"])
    source_messages = [banner_msg] + list(file_messages or [])
    sent_ids: list[int] = []

    try:
        api_bot = require_archive_bot_api()

        for position, source_message in enumerate(source_messages, start=1):
            source_message_id = int(getattr(source_message, "id", 0) or 0)
            if source_message_id <= 0:
                raise RuntimeError(f"شناسه پیام مبدا در ردیف {position} نامعتبر است.")

            copied = await api_bot.copy_message(
                chat_id=archive_chat_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
                disable_notification=True,
            )
            copied_id = int(
                getattr(copied, "message_id", 0)
                or getattr(copied, "id", 0)
                or 0
            )
            if copied_id <= 0:
                raise RuntimeError(f"Bot API برای ردیف {position} شناسه پیام خروجی برنگرداند.")
            sent_ids.append(copied_id)

            logging.info(
                "ARCHIVE_BOT_API_COPY_MESSAGE job=%s position=%s source_chat_id=%s source_message_id=%s archive_chat_id=%s copied_message_id=%s",
                job_id,
                position,
                source_chat_id,
                source_message_id,
                archive_chat_id,
                copied_id,
            )
            await asyncio.sleep(0.15)

        archive_sent_at = now_iso()
        sent_ids_json = json.dumps(sent_ids, ensure_ascii=False)
        update_job(
            job_id,
            archive_enabled=1,
            archive_chat_id=archive_chat_id,
            archive_sent_at=archive_sent_at,
            archive_sent_message_ids_json=sent_ids_json,
            archive_error=None,
        )
        update_archive_item_archive_status(
            job_id,
            archive_sent_at=archive_sent_at,
            archive_sent_message_ids_json=sent_ids_json,
            archive_error=None,
        )
        logging.info(
            "ARCHIVE_POST_PACKAGE_SENT ARCHIVE_BOT_API_COPY_SENT job=%s flow_type=%s source_chat_id=%s archive_chat_id=%s sent_ids=%s",
            job_id,
            normalize_flow_type(flow_type),
            source_chat_id,
            archive_chat_id,
            sent_ids,
        )
    except Exception as error_obj:
        try:
            api_bot = require_archive_bot_api()
            await cleanup_partial_archive_bot_api_copy(api_bot, archive_chat_id, sent_ids, job_id)
        except Exception:
            logging.exception("ARCHIVE_BOT_API_PARTIAL_CLEANUP_ABORTED job=%s", job_id)

        error = archive_bot_api_error_hint(error_obj)
        update_job(job_id, archive_enabled=1, archive_chat_id=archive_chat_id, archive_error=error)
        update_archive_item_archive_status(job_id, archive_error=error)
        logging.exception(
            "ARCHIVE_POST_PACKAGE_FAILED ARCHIVE_BOT_API_COPY_FAILED job=%s flow_type=%s source_chat_id=%s archive_chat_id=%s error=%s",
            job_id,
            normalize_flow_type(flow_type),
            source_chat_id,
            archive_chat_id,
            error_obj,
        )
        try:
            await admin_status(
                "⚠️ ارسال بسته اصلی به کانال آرشیو با Bot API خطا خورد، اما ساخت پست ادامه پیدا می‌کند.\n\n"
                f"Job #{job_id}\n"
                f"مبدا: {source_chat_id}\n"
                f"آرشیو: {archive_chat_id}\n"
                f"خطا: {error}\n\n"
                "ربات API باید در کانال مبدا دسترسی خواندن و در کانال آرشیو اجازه ارسال داشته باشد.",
                owner_id=owner_id,
            )
        except Exception:
            logging.exception("Could not notify archive Bot API failure for job=%s", job_id)
'''


ARCHIVE_TEST_HANDLER = r'''@dp.callback_query(F.data.startswith("flow_archive_test:"))
async def cb_flow_archive_test(callback: CallbackQuery):
    if await reject_non_admin_callback(callback):
        return
    flow_type = normalize_flow_type(callback.data.split(":", 1)[1])
    settings = get_flow_archive_settings(flow_type)
    if not settings["archive_chat_id"]:
        await safe_edit_message(
            callback.message,
            f"❌ {flow_archive_label(flow_type)} تنظیم نشده است.",
            reply_markup=flow_archive_keyboard(flow_type),
        )
        await callback.answer()
        return

    archive_chat_id = int(settings["archive_chat_id"])
    probe_message_id = None
    try:
        api_bot = require_archive_bot_api()
        chat = await api_bot.get_chat(archive_chat_id)
        probe = await api_bot.send_message(
            chat_id=archive_chat_id,
            text="🧪 تست دسترسی Bot API به کانال آرشیو",
            disable_notification=True,
        )
        probe_message_id = int(getattr(probe, "message_id", 0) or 0)
        if probe_message_id:
            try:
                await api_bot.delete_message(chat_id=archive_chat_id, message_id=probe_message_id)
            except Exception as delete_error:
                logging.warning(
                    "ARCHIVE_BOT_API_TEST_MESSAGE_DELETE_FAILED chat=%s msg=%s error=%s",
                    archive_chat_id,
                    probe_message_id,
                    delete_error,
                )
        title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(archive_chat_id)
        text = (
            "✅ دسترسی ارسال Bot API به آرشیو برقرار است.\n\n"
            f"{flow_archive_label(flow_type)}\n"
            f"کانال: {title}\n"
            f"ID: {archive_chat_id}\n\n"
            "برای کپی بنر و فیلم‌ها، همین ربات API باید به کانال مبدا هم دسترسی داشته باشد."
        )
        logging.info("ARCHIVE_BOT_API_ACCESS_TEST_OK flow_type=%s archive_chat_id=%s", flow_type, archive_chat_id)
    except Exception as error_obj:
        error = archive_bot_api_error_hint(error_obj)
        text = (
            "❌ تست ارسال Bot API به آرشیو خطا داد.\n\n"
            f"{error}\n\n"
            "ربات API را در کانال آرشیو ادمین کن و اجازه ارسال پیام/مدیا بده."
        )
        logging.exception(
            "ARCHIVE_BOT_API_ACCESS_TEST_FAILED flow_type=%s archive_chat_id=%s error=%s",
            flow_type,
            archive_chat_id,
            error_obj,
        )
    await safe_edit_message(callback.message, text, reply_markup=flow_archive_keyboard(flow_type))
    await callback.answer()
'''


def replace_one(source: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement.rstrip() + "\n\n", source, count=1, flags=re.M | re.S)
    if count != 1:
        raise RuntimeError(f"بخش {label} پیدا نشد یا ساختارش تغییر کرده است.")
    return updated


def function_nodes(source: str):
    tree = ast.parse(source)
    return [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def add_accounts_button(source: str, exact_names: tuple[str, ...], keyword: str, label: str) -> tuple[str, str]:
    nodes = function_nodes(source)
    node = next((n for n in nodes if n.name in exact_names), None)
    if node is None:
        node = next((n for n in nodes if keyword in n.name.lower() and "keyboard" in n.name.lower()), None)
    if node is None:
        raise RuntimeError(f"تابع کیبورد {label} پیدا نشد.")

    lines = source.splitlines(keepends=True)
    start = node.lineno - 1
    end = node.end_lineno
    block = "".join(lines[start:end])
    if 'callback_data="accounts"' in block or "callback_data='accounts'" in block:
        return source, node.name

    button = f'[InlineKeyboardButton(text="{label}", callback_data="accounts")],'
    for index in range(start, end):
        line = lines[index]
        marker = "InlineKeyboardMarkup(inline_keyboard=["
        if marker in line:
            base_indent = re.match(r"\s*", line).group(0)
            row_indent = base_indent + "    "
            lines[index] = line.replace(marker, marker + "\n" + row_indent + button, 1)
            return "".join(lines), node.name

        if re.search(r"\b(?:rows|buttons)\s*=\s*\[\s*$", line):
            base_indent = re.match(r"\s*", line).group(0)
            lines.insert(index + 1, base_indent + "    " + button + "\n")
            return "".join(lines), node.name

    raise RuntimeError(f"لیست دکمه‌های تابع {node.name} پیدا نشد.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Patch Postsaz 5.1 to 5.2")
    parser.add_argument("--project", default="/root/postsaz/telegram_inline_panel_worker")
    args = parser.parse_args()

    project = Path(args.project).resolve()
    main_path = project / "main.py"
    if not main_path.exists():
        raise SystemExit(f"main.py پیدا نشد: {main_path}")

    original = main_path.read_text(encoding="utf-8")
    if "ARCHIVE_BOT_API_COPY_SENT" in original and re.search(r'APP_VERSION\s*=\s*["\']5\.2["\']', original):
        print("Postsaz 5.2 قبلاً نصب شده است.")
        return

    source = original
    source, version_count = re.subn(
        r'APP_VERSION\s*=\s*["\'][^"\']+["\']',
        'APP_VERSION = "5.2"',
        source,
        count=1,
    )
    if version_count != 1:
        raise RuntimeError("APP_VERSION پیدا نشد.")

    source = replace_one(
        source,
        r'^@dp\.callback_query\(F\.data\.startswith\("flow_archive_test:"\)\)\nasync def cb_flow_archive_test\(callback: CallbackQuery\):\n.*?(?=^@dp\.callback_query|\Z)',
        ARCHIVE_TEST_HANDLER,
        "تست کانال آرشیو",
    )

    source = replace_one(
        source,
        r'^async def send_original_package_to_archive\(\n.*?(?=^def cancel_live_group_timer\()',
        ARCHIVE_HELPERS_AND_FUNCTION,
        "ارسال بسته به آرشیو",
    )

    source, settings_keyboard_name = add_accounts_button(
        source,
        ("settings_menu_keyboard", "settings_keyboard", "settings_section_keyboard"),
        "settings",
        "👤 مدیریت اکانت‌های واقعی",
    )
    source, skie_keyboard_name = add_accounts_button(
        source,
        ("skie_menu_keyboard", "skie_keyboard"),
        "skie",
        "👤 اکانت مورد استفاده اسکی",
    )

    # Parse and compile the complete candidate before replacing production main.py.
    ast.parse(source)
    temp_path = project / "main.py.v5.2.tmp"
    temp_path.write_text(source, encoding="utf-8")
    py_compile.compile(str(temp_path), doraise=True)

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_path = project / f"main.py.backup_before_5.2_{stamp}"
    shutil.copy2(main_path, backup_path)
    temp_path.replace(main_path)

    readme = project / "README_FA.md"
    if readme.exists():
        text = readme.read_text(encoding="utf-8")
        if "## تغییرات نسخه 5.2" not in text:
            text += "\n\n## تغییرات نسخه 5.2\n\n- ارسال بسته آرشیو فقط با Bot API و `copy_message`.\n- حذف ارسال آرشیو با اکانت واقعی و `ForwardMessagesRequest`.\n- تست دسترسی آرشیو با پیام آزمایشی Bot API.\n- بازگشت دکمه مدیریت اکانت‌های واقعی در تنظیمات و بخش اسکی.\n"
            readme.write_text(text, encoding="utf-8")

    changelog = project / "CHANGELOG.md"
    if changelog.exists():
        text = changelog.read_text(encoding="utf-8")
        if "## 5.2" not in text:
            changelog.write_text(
                "## 5.2\n- Bot API-only archive copying.\n- Restored real-account management menu buttons.\n\n" + text,
                encoding="utf-8",
            )

    print(f"OK: APP_VERSION=5.2")
    print(f"OK: archive uses Bot API copy_message only")
    print(f"OK: settings accounts button -> {settings_keyboard_name}")
    print(f"OK: skie accounts shortcut -> {skie_keyboard_name}")
    print(f"BACKUP={backup_path}")


if __name__ == "__main__":
    main()
