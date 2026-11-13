"""Bantuan privat ke owner utama, tanpa username admin eksternal."""

import asyncio
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

log = logging.getLogger(__name__)
LABEL = "Chat diteruskan ke owner"
ENTER = re.compile(
    r"\s*(?:hubungi\s+(?:admin|owner)|keluhan|"
    r"chat diteruskan ke owner|/bantuan(?:@[A-Za-z0-9_]+)?)\s*",
    re.I,
)


def button():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(LABEL, callback_data="mcm:support")
    ]])


class HubungiOwner:
    def __init__(self, controller):
        self.c = controller
        self.store = controller.vip_store
        self.owner = controller.settings.owner_id
        self.lock = asyncio.Lock()

        with self.store.lock, self.store.connection:
            self.store.connection.executescript("""
                CREATE TABLE IF NOT EXISTS mcm_help_modes (
                    bot_id INTEGER,
                    owner_id INTEGER,
                    user_id INTEGER,
                    active INTEGER,
                    PRIMARY KEY(bot_id,owner_id,user_id)
                );

                CREATE TABLE IF NOT EXISTS mcm_help_jobs (
                    bot_id INTEGER,
                    owner_id INTEGER,
                    source_id INTEGER,
                    message_id INTEGER,
                    recipient_id INTEGER,
                    member_id INTEGER,
                    state TEXT,
                    header_id INTEGER,
                    copied_id INTEGER,
                    PRIMARY KEY(
                        bot_id,owner_id,source_id,message_id
                    )
                );

                CREATE TABLE IF NOT EXISTS mcm_help_routes (
                    bot_id INTEGER,
                    owner_id INTEGER,
                    chat_id INTEGER,
                    message_id INTEGER,
                    member_id INTEGER,
                    PRIMARY KEY(
                        bot_id,owner_id,chat_id,message_id
                    )
                );
            """)

    def mode(self, bot_id, user_id, active=None):
        key = (bot_id, self.owner, user_id)

        with self.store.lock, self.store.connection:
            if active is not None:
                self.store.connection.execute(
                    "INSERT OR REPLACE INTO mcm_help_modes "
                    "VALUES(?,?,?,?)",
                    (*key, int(active)),
                )

            row = self.store.connection.execute(
                "SELECT active FROM mcm_help_modes "
                "WHERE bot_id=? AND owner_id=? AND user_id=?",
                key,
            ).fetchone()

        return bool(row and row[0])

    def linked(self, bot_id, chat_id, mid):
        with self.store.lock:
            row = self.store.connection.execute(
                "SELECT member_id FROM mcm_help_routes "
                "WHERE bot_id=? AND owner_id=? "
                "AND chat_id=? AND message_id=?",
                (bot_id, self.owner, chat_id, mid),
            ).fetchone()

        return row[0] if row else None

    async def notice(self, message, text, **kwargs):
        try:
            await message.reply_text(
                text, parse_mode=None, **kwargs
            )
        except TelegramError:
            pass

    async def prompt(self, message):
        await self.notice(
            message,
            "Mode chat ke owner aktif. Tulis keluhanmu atau "
            "kirim foto, dokumen, atau video di chat ini. "
            "Pesan berikutnya akan diteruskan ke owner. "
            "Untuk kendala pembayaran, sertakan nomor pesanan; "
            "kalau sudah transfer, jangan bayar ulang.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "Selesai chat",
                    callback_data="mcm:support:close",
                )
            ]]),
        )

    async def callback(self, update, context):
        query = update.callback_query
        m = query.message
        user = query.from_user

        if not query.data.startswith("mcm:support"):
            if (
                m is not None
                and m.chat.type == "private"
                and m.chat.id == user.id
                and user.id != self.owner
            ):
                self.mode(context.bot.id, user.id, False)
            return

        await query.answer()

        if (
            m is None
            or m.chat.type != "private"
            or m.chat.id != user.id
            or user.is_bot
        ):
            raise ApplicationHandlerStop

        if user.id == self.owner:
            await self.notice(
                m,
                "Kamu owner. Keluhan member masuk ke chat ini; "
                "balas pesan bantuan untuk menjawab melalui bot.",
            )
            raise ApplicationHandlerStop

        active = query.data != "mcm:support:close"
        self.mode(context.bot.id, user.id, active)

        if active:
            await self.prompt(m)
        else:
            await self.notice(
                m,
                "Mode chat ke owner selesai. "
                "Gunakan /start untuk kembali ke menu bot.",
            )

        raise ApplicationHandlerStop

    async def receive(self, update, context):
        try:
            return await self.handle(update, context)
        except ApplicationHandlerStop:
            raise
        except Exception as error:
            log.error(
                "Mode bantuan tertunda: %s",
                type(error).__name__,
            )
            if update.effective_message is not None:
                await self.notice(
                    update.effective_message,
                    "Pesan bantuan belum bisa diproses. "
                    "Kalau sudah transfer, jangan bayar ulang.",
                )
            raise ApplicationHandlerStop

    async def handle(self, update, context):
        m = update.effective_message

        if (
            m is None
            or update.edited_message is not None
            or m.chat.type != "private"
            or m.from_user is None
            or m.from_user.is_bot
            or m.chat.id != m.from_user.id
        ):
            return

        uid = m.from_user.id
        text = m.text or ""
        quoted = getattr(m, "reply_to_message", None)

        if uid == self.owner:
            if quoted is None:
                return

            peer = self.linked(
                context.bot.id,
                self.owner,
                quoted.message_id,
            )
            if peer is None:
                return

            recipient = peer

        else:
            if ENTER.fullmatch(text):
                self.mode(context.bot.id, uid, True)
                await self.prompt(m)
                raise ApplicationHandlerStop

            if text.strip().upper() == "SELESAI CHAT":
                self.mode(context.bot.id, uid, False)
                await self.notice(
                    m,
                    "Mode chat ke owner selesai. "
                    "Gunakan /start untuk kembali ke menu bot.",
                )
                raise ApplicationHandlerStop

            if re.match(
                r"^/(?:start|star|vip|batal|akses|cek|pending)"
                r"(?:@[A-Za-z0-9_]+)?(?:\s|$)",
                text,
                re.I,
            ):
                self.mode(context.bot.id, uid, False)
                return

            reply_support = (
                quoted is not None
                and self.linked(
                    context.bot.id, uid, quoted.message_id
                ) == uid
            )

            if (
                not self.mode(context.bot.id, uid)
                and not reply_support
            ):
                return

            if reply_support:
                self.mode(context.bot.id, uid, True)

            peer = uid
            recipient = self.owner

        try:
            result = await self.relay(
                m, context.bot, recipient, peer
            )

            if result == "sent":
                await self.notice(
                    m,
                    "Balasan dikirim ke member."
                    if uid == self.owner
                    else "Pesan sudah diteruskan ke owner.",
                )
            elif result != "duplicate":
                await self.notice(
                    m,
                    "Pengiriman belum terkonfirmasi. "
                    "Jangan bayar ulang untuk kendala pembayaran; "
                    "coba bantuan lagi nanti.",
                )

        except Exception as error:
            log.error(
                "Pesan bantuan tertunda: %s",
                type(error).__name__,
            )
            await self.notice(
                m,
                "Pesan bantuan belum berhasil diteruskan. "
                "Kalau sudah transfer, jangan bayar ulang.",
            )

        raise ApplicationHandlerStop

    async def send(self, method, **kwargs):
        for attempt in range(2):
            try:
                return await method(
                    **kwargs,
                    read_timeout=30,
                    write_timeout=30,
                    connect_timeout=10,
                )
            except RetryAfter as error:
                delay = error.retry_after
                delay = (
                    delay.total_seconds()
                    if hasattr(delay, "total_seconds")
                    else float(delay)
                )
                if attempt or delay > 15:
                    raise
                await asyncio.sleep(delay + 1)

    async def relay(self, message, bot, recipient, peer):
        key = (
            bot.id,
            self.owner,
            message.chat.id,
            message.message_id,
        )

        async with self.lock:
            with self.store.lock, self.store.connection:
                inserted = self.store.connection.execute(
                    "INSERT OR IGNORE INTO mcm_help_jobs "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        *key, recipient, peer,
                        "sending", None, None,
                    ),
                ).rowcount

            if not inserted:
                return "duplicate"

            inbound = recipient == self.owner
            name = (
                getattr(message.from_user, "full_name", None)
                or getattr(
                    message.from_user, "first_name", "Member"
                )
            )

            snippet = (
                ""
                if getattr(message, "has_protected_content", False)
                else (message.text or message.caption or "")[:700]
            )

            header = (
                f"Pesan bantuan member\n"
                f"Nama: {str(name)[:100]}\n"
                f"ID member: {peer}\n\n"
                f"{snippet}\n\n"
                "Balas pesan ini atau salinannya untuk "
                "menjawab lewat bot. Persetujuan pembayaran "
                "tetap melalui pesan verifikasi pembayaran."
                if inbound
                else "Balasan owner untuk pesan bantuanmu:"
            )

            try:
                sent = await self.send(
                    bot.send_message,
                    chat_id=recipient,
                    text=header,
                    parse_mode=None,
                )

                self.link(
                    bot.id, recipient, sent.message_id, peer
                )

                with self.store.lock, self.store.connection:
                    self.store.connection.execute(
                        "UPDATE mcm_help_jobs SET header_id=? "
                        "WHERE bot_id=? AND owner_id=? "
                        "AND source_id=? AND message_id=?",
                        (sent.message_id, *key),
                    )

                if getattr(
                    message, "has_protected_content", False
                ):
                    raise BadRequest(
                        "Protected message cannot be relayed"
                    )

                copied = await self.send(
                    bot.copy_message,
                    chat_id=recipient,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    protect_content=not inbound,
                )

                self.link(
                    bot.id, recipient, copied.message_id, peer
                )
                state = "sent"
                copy_id = copied.message_id

            except TelegramError as error:
                state = (
                    "uncertain"
                    if isinstance(error, NetworkError)
                    and not isinstance(error, BadRequest)
                    else "failed"
                )
                copy_id = None
                log.warning(
                    "Penerusan bantuan belum selesai: %s",
                    type(error).__name__,
                )

            with self.store.lock, self.store.connection:
                self.store.connection.execute(
                    "UPDATE mcm_help_jobs SET state=?,copied_id=? "
                    "WHERE bot_id=? AND owner_id=? "
                    "AND source_id=? AND message_id=?",
                    (state, copy_id, *key),
                )

            return state

    def link(self, bot_id, chat_id, mid, peer):
        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "INSERT OR IGNORE INTO mcm_help_routes "
                "VALUES(?,?,?,?,?)",
                (bot_id, self.owner, chat_id, mid, peer),
            )


def pasang_hubungi_owner(application, controller):
    if hasattr(controller, "hubungi_owner"):
        return controller.hubungi_owner

    support = HubungiOwner(controller)
    controller.hubungi_owner = support

    application.add_handler(
        CallbackQueryHandler(
            support.callback,
            pattern=(
                r"^(?:mcm:(?:support(?::(?:open|close))?"
                r"|backup|video|vip)|vip:(?:buy|proof|check):.+)$"
            ),
        ),
        group=-10,
    )

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.UpdateType.MESSAGE,
            support.receive,
        ),
        group=-10,
    )

    return support
