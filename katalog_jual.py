"""Katalog stok privat; pembayaran belum diaktifkan."""

import asyncio
import json
import logging
import os
import re
import time

from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop

log = logging.getLogger(__name__)
DEFAULT_CHANNEL = -1004388249648

CODE = re.compile(r"MCB[A-Z0-9]{1,20}", re.I)
CODE_LINE = re.compile(
    r"\s*(?:\d{1,3}[.)]\s*)?(?:kode\s*:\s*)?"
    r"(MCB[A-Z0-9]{1,20})\s*",
    re.I,
)
COMMAND = re.compile(
    r"\s*katalog(?:\s+(MCB[A-Z0-9]{1,20}))?\s*",
    re.I,
)


def product_code(caption):
    codes = {
        match[1].upper()
        for line in (caption or "").splitlines()
        if (match := CODE_LINE.fullmatch(line))
    }
    if len(codes) != 1:
        raise ValueError(
            "Caption harus memiliki tepat satu kode, "
            "misalnya MCB1697, pada baris sendiri."
        )
    return codes.pop()


class KatalogJual:
    def __init__(self, controller, downloads):
        self.c = controller
        self.store = controller.vip_store

        value = os.getenv(
            "MCM_CATALOG_CHAT_ID", str(DEFAULT_CHANNEL)
        ).strip()
        if not re.fullmatch(r"-[1-9][0-9]*", value):
            raise ValueError(
                "MCM_CATALOG_CHAT_ID harus ID channel negatif."
            )

        self.channel = int(value)
        self.check_lock = asyncio.Lock()
        self.checked_at = None
        self.ready = False

        with self.store.lock, self.store.connection:
            self.store.connection.executescript("""
                CREATE TABLE IF NOT EXISTS mcm_sale_items (
                    bot_id INTEGER,
                    owner_id INTEGER,
                    channel_id INTEGER,
                    message_id INTEGER,
                    kind TEXT,
                    file_id TEXT,
                    unique_id TEXT,
                    code TEXT,
                    caption TEXT,
                    entities TEXT,
                    state TEXT,
                    reason TEXT,
                    updated_at REAL,
                    PRIMARY KEY(
                        bot_id,owner_id,channel_id,message_id
                    )
                );

                CREATE TABLE IF NOT EXISTS mcm_sale_codes (
                    bot_id INTEGER,
                    owner_id INTEGER,
                    code TEXT,
                    channel_id INTEGER,
                    message_id INTEGER,
                    PRIMARY KEY(bot_id,owner_id,code)
                );
            """)

        original_owner = downloads.owner_text
        original_private = controller.private_text

        async def owner_text(update, context):
            if not await self.command(update, context):
                return await original_owner(update, context)

        async def private_text(update, context):
            if not await self.command(update, context):
                return await original_private(update, context)

        downloads.owner_text = owner_text
        controller.private_text = private_text

    async def ensure(self, bot, *, force=False):
        async with self.check_lock:
            if (
                not force
                and self.ready
                and self.checked_at is not None
                and time.monotonic() - self.checked_at < 300
            ):
                return True

            self.ready = False

            try:
                chat, owner, member = await asyncio.gather(
                    bot.get_chat(self.channel),
                    bot.get_chat_member(
                        self.channel, self.c.settings.owner_id
                    ),
                    bot.get_chat_member(self.channel, bot.id),
                )

                self.ready = (
                    chat.type == "channel"
                    and owner.status in (
                        "creator", "administrator"
                    )
                    and member.status == "administrator"
                )

            except TelegramError as error:
                log.warning(
                    "Izin channel katalog belum terkonfirmasi: %s",
                    type(error).__name__,
                )

            self.checked_at = time.monotonic()
            return self.ready

    async def notice(self, bot, text):
        try:
            await bot.send_message(
                chat_id=self.c.settings.owner_id,
                text=text,
                parse_mode=None,
            )
        except TelegramError as error:
            log.warning(
                "Notifikasi katalog tertunda: %s",
                type(error).__name__,
            )

    def record(self, bot_id, message):
        key = (
            bot_id,
            self.c.settings.owner_id,
            self.channel,
            message.message_id,
        )

        video = getattr(message, "video", None)
        document = getattr(message, "document", None)

        media = video or (
            document
            if document
            and (document.mime_type or "").startswith("video/")
            else None
        )

        with self.store.lock, self.store.connection:
            old = self.store.connection.execute(
                "SELECT kind,file_id,unique_id,code,caption,"
                "entities,state,reason FROM mcm_sale_items "
                "WHERE bot_id=? AND owner_id=? "
                "AND channel_id=? AND message_id=?",
                key,
            ).fetchone()

            if media is None:
                if old is None or old[6] == "invalid":
                    return None

                self.store.connection.execute(
                    "UPDATE mcm_sale_items SET state='invalid',"
                    "reason='Media bukan video',updated_at=? "
                    "WHERE bot_id=? AND owner_id=? "
                    "AND channel_id=? AND message_id=?",
                    (time.time(), *key),
                )
                return (
                    "Produk dinonaktifkan: "
                    "media pesan katalog bukan video."
                )

            if (
                old is not None
                and old[2]
                and getattr(media, "file_unique_id", "") != old[2]
            ):
                if (
                    old[6] == "invalid"
                    and old[7] == "Isi video berubah"
                ):
                    return None

                self.store.connection.execute(
                    "UPDATE mcm_sale_items SET state='invalid',"
                    "reason='Isi video berubah',updated_at=? "
                    "WHERE bot_id=? AND owner_id=? "
                    "AND channel_id=? AND message_id=?",
                    (time.time(), *key),
                )
                return (
                    "Isi video produk berubah. Unggah sebagai "
                    "pesan baru dengan kode baru, le."
                )

            caption = message.caption or ""
            code = ""
            state = "draft"
            reason = ""

            try:
                code = product_code(caption)

                taken = self.store.connection.execute(
                    "SELECT channel_id,message_id "
                    "FROM mcm_sale_codes "
                    "WHERE bot_id=? AND owner_id=? AND code=?",
                    (bot_id, self.c.settings.owner_id, code),
                ).fetchone()

                if (
                    taken is not None
                    and tuple(taken) != key[2:]
                ):
                    raise ValueError(
                        f"Kode {code} sudah dipakai video lain. "
                        "Gunakan kode baru."
                    )

                self.store.connection.execute(
                    "INSERT OR IGNORE INTO mcm_sale_codes "
                    "VALUES(?,?,?,?,?)",
                    (
                        bot_id,
                        self.c.settings.owner_id,
                        code,
                        *key[2:],
                    ),
                )

            except ValueError as error:
                state = "invalid"
                reason = str(error)

            entities = json.dumps(
                [
                    entity.to_dict()
                    for entity in message.caption_entities or ()
                ],
                ensure_ascii=False,
            )

            values = (
                "video" if video else "document",
                media.file_id,
                getattr(media, "file_unique_id", ""),
                code,
                caption,
                entities,
                state,
                reason,
            )

            if old is not None and tuple(old) == values:
                return None

            self.store.connection.execute(
                "INSERT OR REPLACE INTO mcm_sale_items "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*key, *values, time.time()),
            )

        if state == "invalid":
            return "Video katalog belum siap: " + reason

        return (
            f"Katalog tersimpan: {code}\n"
            "Harga acuan: Rp15.000/video.\n"
            "Status: draf privat; pembelian belum aktif."
        )

    async def observe(self, update, context):
        message = (
            getattr(update, "channel_post", None)
            or getattr(update, "edited_channel_post", None)
        )

        if message is None or message.chat.id != self.channel:
            return

        if not await self.ensure(context.bot):
            await self.notice(
                context.bot,
                "Katalog belum terhubung. Pastikan bot MCB "
                "admin di channel katalog milik owner.",
            )
            raise ApplicationHandlerStop

        text = self.record(context.bot.id, message)
        if text:
            await self.notice(context.bot, text)

        raise ApplicationHandlerStop

    async def command(self, update, context):
        message = update.effective_message

        if (
            message is None
            or message.chat.type != "private"
            or update.edited_message is not None
            or message.from_user is None
            or message.from_user.is_bot
        ):
            return False

        command = COMMAND.fullmatch(message.text or "")
        bare_code = CODE.fullmatch(
            (message.text or "").strip()
        )

        if command is None and bare_code is None:
            return False

        if (
            message.from_user.id != self.c.settings.owner_id
            or message.chat.id != self.c.settings.owner_id
        ):
            await message.reply_text(
                "Pembelian video per kode belum dibuka. "
                "Hubungi admin untuk informasi, le."
            )
            return True

        if not await self.ensure(context.bot, force=True):
            await message.reply_text(
                "Katalog belum terhubung. Tambahkan bot MCB "
                "sebagai admin channel katalog, le."
            )
            return True

        code = (
            (command[1] or "")
            if command
            else bare_code[0]
        ).upper()

        with self.store.lock:
            if not code:
                rows = self.store.connection.execute(
                    "SELECT code,state FROM mcm_sale_items "
                    "WHERE bot_id=? AND owner_id=? AND channel_id=? "
                    "ORDER BY updated_at DESC LIMIT 15",
                    (
                        context.bot.id,
                        self.c.settings.owner_id,
                        self.channel,
                    ),
                ).fetchall()
            else:
                item = self.store.connection.execute(
                    "SELECT i.kind,i.file_id,i.state "
                    "FROM mcm_sale_codes c "
                    "JOIN mcm_sale_items i "
                    "ON i.bot_id=c.bot_id "
                    "AND i.owner_id=c.owner_id "
                    "AND i.channel_id=c.channel_id "
                    "AND i.message_id=c.message_id "
                    "WHERE c.bot_id=? AND c.owner_id=? "
                    "AND c.channel_id=? AND c.code=? "
                    "AND i.code=c.code",
                    (
                        context.bot.id,
                        self.c.settings.owner_id,
                        self.channel,
                        code,
                    ),
                ).fetchone()

        if not code:
            listing = "\n".join(
                f"{row[0] or '(tanpa kode)'} — {row[1]}"
                for row in rows
            ) or "Belum ada video tercatat."

            await message.reply_text(
                "Katalog privat terhubung. "
                "Harga acuan Rp15.000/video.\n"
                + listing
                + "\n\nKirim KATALOG MCB1697 untuk pratinjau "
                "owner. Pembelian belum aktif.",
                parse_mode=None,
            )

        elif item is None or item[2] != "draft":
            await message.reply_text(
                "Kode itu belum memiliki video "
                "yang siap di katalog, le."
            )

        else:
            send = (
                context.bot.send_video
                if item[0] == "video"
                else context.bot.send_document
            )
            await send(
                chat_id=message.chat.id,
                **{item[0]: item[1]},
                protect_content=True,
                parse_mode=None,
                caption=(
                    f"Kode: {code}\n"
                    "Harga acuan: Rp15.000\n"
                    "Draf privat; pembelian belum aktif."
                ),
            )

        return True


def pasang_katalog_jual(controller, downloads):
    if not hasattr(controller, "katalog_jual"):
        controller.katalog_jual = KatalogJual(
            controller, downloads
        )
    return controller.katalog_jual
