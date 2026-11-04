import asyncio
import json
import logging
import os
import re
import sqlite3
from collections import deque
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

logger = logging.getLogger("mcm_induk")

URL = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/[^\s<>]+",
    re.I,
)

RELAY = re.compile(
    r"^\s*tolong\s+kirim\s+ke\s+grup\b",
    re.I,
)

ASK_VIDEO = re.compile(
    r"\b(?:kirim\w*|minta|ambil\w*|download)\b"
    r".*\b(?:video|vid|file)\b"
    r"|\b(?:video|vid)\b"
    r".*\b(?:kirim\w*|minta|ambil\w*|download)\b",
    re.I,
)


class KatalogVideo:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        with closing(
            sqlite3.connect(self.path, timeout=10)
        ) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS video (
                    bot_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    entities TEXT NOT NULL,
                    PRIMARY KEY (
                        bot_id, owner_id, group_id, message_id
                    )
                )
            """)

    def simpan(self, bot_id, owner_id, group_id, rows):
        values = [
            (
                bot_id,
                owner_id,
                group_id,
                message_id,
                data["kind"],
                data["file_id"],
                data["caption"],
                json.dumps(data["entities"], ensure_ascii=False),
            )
            for message_id, data in rows
        ]

        with closing(
            sqlite3.connect(self.path, timeout=10)
        ) as db, db:
            db.executemany(
                "INSERT OR REPLACE INTO video VALUES "
                "(?,?,?,?,?,?,?,?)",
                values,
            )

    def ambil(self, bot_id, owner_id, group_id, message_id):
        with closing(
            sqlite3.connect(self.path, timeout=10)
        ) as db:
            return db.execute(
                "SELECT kind,file_id,caption,entities FROM video "
                "WHERE bot_id=? AND owner_id=? "
                "AND group_id=? AND message_id=?",
                (bot_id, owner_id, group_id, message_id),
            ).fetchone()


def parse_tautan(text):
    links = URL.findall(text)

    if len(links) != 1:
        return None

    value = links[0].rstrip(".,!?)]}")

    if "://" not in value:
        value = "https://" + value

    parsed = urlsplit(value)

    if (
        parsed.scheme != "https"
        or parsed.hostname not in ("t.me", "telegram.me")
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        return None

    parts = parsed.path.strip("/").split("/")

    # Grup privat, termasuk pesan di dalam topik.
    if (
        len(parts) in (3, 4)
        and parts[0] == "c"
        and all(part.isdigit() for part in parts[1:])
    ):
        group_id = int("-100" + parts[1])
        message_id = int(parts[-1])

        if -(2**63) < group_id < 0 and 0 < message_id < 2**63:
            return group_id, message_id

    # Grup publik dengan username.
    if (
        len(parts) in (2, 3)
        and re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{3,31}", parts[0]
        )
        and all(part.isdigit() for part in parts[1:])
    ):
        message_id = int(parts[-1])

        if 0 < message_id < 2**63:
            return "@" + parts[0], message_id

    return None


class _ChatPribadi:
    async def ngobrol_pribadi(
        self, message, context, history, generation
    ):
        uid = message.from_user.id
        typing = None

        def aktif():
            return (
                self.dm_generation.get(uid, 0) == generation
                and getattr(
                    context.application, "running", True
                )
            )

        try:
            if not aktif():
                return

            if hasattr(self, "start_typing"):
                typing = self.start_typing(
                    message.chat.id, context
                )

            try:
                async with asyncio.timeout(75):
                    answer = await self.ai.answer(
                        history,
                        wait_for_budget=True,
                        enabled=aktif,
                    )
            except TimeoutError:
                answer = None

            if not aktif():
                return

            if answer is None:
                await message.reply_text(
                    "Emak lagi menunggu sambungan atau giliran AI, "
                    "le. Coba lagi sebentar ya."
                )
                return

            await message.reply_text(
                answer,
                parse_mode=None,
                disable_web_page_preview=True,
            )

            if uid in self.dm_history:
                self.dm_history[uid].append({
                    "speaker": "bot",
                    "text": answer,
                })

        except TelegramError as error:
            logger.warning(
                "Chat pribadi belum terkonfirmasi: jenis=%s",
                type(error).__name__,
            )

        finally:
            if typing is not None:
                await self.stop_typing(
                    message.chat.id, typing
                )

            self.dm_pending.discard(uid)

    async def kirim_video_member(
        self, message, context, reference
    ):
        uid = message.from_user.id
        target = self.settings.target_group_id

        try:
            if target is None or self.settings.owner_id is None:
                await message.reply_text(
                    "Katalog video belum diatur admin, le."
                )
                return

            async with self.dm_video_slots:
                chat = await context.bot.get_chat(target)

                owner = await context.bot.get_chat_member(
                    target, self.settings.owner_id
                )

                bot_member = await context.bot.get_chat_member(
                    target, context.bot.id
                )

                member = await context.bot.get_chat_member(
                    target, uid
                )

                if (
                    chat.type not in ("group", "supergroup")
                    or owner.status not in (
                        "creator", "administrator"
                    )
                    or bot_member.status not in (
                        "creator", "administrator"
                    )
                ):
                    await message.reply_text(
                        "Pengiriman video belum tersedia. "
                        "Minta admin cek izin bot, le."
                    )
                    return

                joined = member.status in (
                    "member", "administrator", "creator"
                ) or (
                    member.status == "restricted"
                    and getattr(member, "is_member", False)
                )

                if not joined:
                    await message.reply_text(
                        "Fitur video ini khusus member yang masih "
                        "bergabung di grup MCM, le."
                    )
                    return

                source, message_id = reference

                aliases = [
                    getattr(chat, "username", "")
                ] + list(
                    getattr(chat, "active_usernames", None) or []
                )

                if isinstance(source, int):
                    valid = source == target
                else:
                    valid = source[1:].casefold() in {
                        value.casefold()
                        for value in aliases
                        if value
                    }

                if not valid:
                    await message.reply_text(
                        "Pakai tautan pesan dari grup MCM "
                        "ini saja ya, le."
                    )
                    return

                row = await asyncio.to_thread(
                    self.katalog.ambil,
                    context.bot.id,
                    self.settings.owner_id,
                    target,
                    message_id,
                )

                if row is None:
                    await message.reply_text(
                        "Video itu belum ada di katalog, le. "
                        "Yang bisa Emak kirim hanya video baru "
                        "yang admin kirim lewat bot sejak "
                        "fitur ini dipasang."
                    )
                    return

                kind, file_id, caption, raw_entities = row

                entities = [
                    MessageEntity.de_json(item, context.bot)
                    for item in json.loads(raw_entities)
                ]

                if kind == "video":
                    send = context.bot.send_video
                    file_arg = {"video": file_id}
                else:
                    send = context.bot.send_document
                    file_arg = {"document": file_id}

                for attempt in range(2):
                    try:
                        await send(
                            chat_id=message.chat.id,
                            **file_arg,
                            caption=caption,
                            caption_entities=entities,
                            parse_mode=None,
                        )
                        return

                    except RetryAfter as error:
                        wait = error.retry_after
                        wait = (
                            wait.total_seconds()
                            if hasattr(wait, "total_seconds")
                            else float(wait)
                        )

                        if attempt or wait > 30:
                            raise

                        await asyncio.sleep(wait + 1)

        except (sqlite3.Error, ValueError, TypeError) as error:
            logger.warning(
                "Katalog video gagal dibaca: jenis=%s",
                type(error).__name__,
            )
            await self.dm_notice(
                message,
                "Katalog video sedang bermasalah, le. "
                "Minta admin cek dulu ya.",
            )

        except TelegramError as error:
            logger.warning(
                "Kirim video member belum terkonfirmasi: jenis=%s",
                type(error).__name__,
            )

            if (
                isinstance(error, NetworkError)
                and not isinstance(error, BadRequest)
            ):
                text = (
                    "Koneksi putus saat mengirim, le. "
                    "Cek chat ini sebelum mengulang; "
                    "videonya mungkin sudah masuk."
                )
            else:
                text = (
                    "Video belum berhasil dikirim, le. "
                    "Bisa jadi izin Telegram berubah "
                    "atau file belum tersedia."
                )

            await self.dm_notice(message, text)

        finally:
            self.dm_video_pending.discard(uid)

    async def dm_notice(self, message, text):
        try:
            await message.reply_text(text)
        except TelegramError:
            pass


def pasang_chat_pribadi(bot_class):
    if bot_class.__dict__.get(
        "_chat_pribadi_terpasang", False
    ):
        return

    original_init = bot_class.__init__
    original_start = bot_class.start
    original_text = bot_class.private_text
    original_media = bot_class.private_media
    original_copy = bot_class.copy_media_to_group

    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

        self.katalog = KatalogVideo(
            os.environ.get(
                "MCM_MEDIA_DB", "mcm_media.sqlite3"
            )
        )

        self.dm_history = {}
        self.dm_pending = set()
        self.dm_generation = {}
        self.dm_video_pending = set()
        self.dm_seen = set()
        self.dm_sources = {}
        self.dm_video_slots = asyncio.Semaphore(2)

        logger.info(
            "Chat pribadi v14 aktif: AI privat dan "
            "katalog salinan asli video pemilik."
        )

    async def start(self, update, context):
        message = update.effective_message
        args = getattr(context, "args", None) or []

        if (
            message.chat.type != "private"
            or (args and args[0].lower() == "backup")
        ):
            return await original_start(
                self, update, context
            )

        uid = message.from_user.id

        self.dm_generation[uid] = (
            self.dm_generation.get(uid, 0) + 1
        )
        self.dm_history.pop(uid, None)

        await message.reply_text(
            "Ada apa le?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "Link cadangan grup",
                    url=self.settings.backup_url,
                )
            ]]),
        )

    async def private_text(self, update, context):
        message = update.effective_message

        if (
            message is None
            or message.chat.type != "private"
            or update.edited_message is not None
            or message.from_user is None
            or message.from_user.is_bot
        ):
            return

        text = message.text or ""

        # Perintah kirim pengumuman tetap khusus owner.
        if RELAY.match(text):
            return await original_text(
                self, update, context
            )

        if text.strip().lower() in ("start", "star"):
            return await self.start(update, context)

        if text.strip().lower() in (
            "link cadangan", "link grup", "backup"
        ):
            return await original_start(
                self, update, context
            )

        identity = (
            message.chat.id, message.message_id
        )

        if identity in self.dm_seen:
            return

        if len(self.dm_seen) >= 10000:
            self.dm_seen.clear()

        self.dm_seen.add(identity)
        uid = message.from_user.id

        if self.forbidden_request(message):
            await message.reply_text(
                "Emak tidak membantu pencarian "
                "konten seksual anak, le."
            )
            return

        try:
            reference = parse_tautan(text)
        except ValueError:
            reference = None

        if reference is not None:
            if (
                uid in self.dm_video_pending
                or len(self.dm_video_pending) >= 20
            ):
                await message.reply_text(
                    "Tunggu kiriman video sebelumnya "
                    "selesai dulu ya, le."
                )
                return

            self.dm_video_pending.add(uid)

            context.application.create_task(
                self.kirim_video_member(
                    message, context, reference
                ),
                update=update,
            )
            return

        if ASK_VIDEO.search(text):
            await message.reply_text(
                "Kirim tautan pesan video dari grup MCM "
                "ke sini ya, le. Yang bisa dikirim adalah "
                "video baru yang admin unggah lewat bot "
                "sejak fitur ini aktif."
            )
            return

        if self.ai is None:
            await message.reply_text(
                "Sambungan AI Emak belum diatur admin, le. "
                "Permintaan video tetap bisa memakai "
                "tautan pesan grup."
            )
            return

        if (
            uid in self.dm_pending
            or len(self.dm_pending) >= 10
        ):
            await message.reply_text(
                "Sebentar le, Emak masih menyiapkan jawaban. "
                "Coba lagi setelah balasan sebelumnya masuk ya."
            )
            return

        if uid not in self.dm_history:
            if len(self.dm_history) >= 1000:
                self.dm_history.pop(
                    next(iter(self.dm_history))
                )

            self.dm_history[uid] = deque(maxlen=8)

        self.dm_history[uid].append({
            "speaker": "member",
            "text": text[:500],
        })

        self.dm_pending.add(uid)

        context.application.create_task(
            self.ngobrol_pribadi(
                message,
                context,
                list(self.dm_history[uid]),
                self.dm_generation.get(uid, 0),
            ),
            update=update,
        )

    async def private_media(self, update, context):
        message = update.effective_message

        if (
            message is not None
            and message.chat.type == "private"
            and update.edited_message is None
            and message.from_user is not None
            and message.from_user.id == self.settings.owner_id
            and not getattr(
                message, "has_protected_content", False
            )
        ):
            video = message.video
            document = message.document

            if video or (
                document
                and (document.mime_type or "").startswith(
                    "video/"
                )
            ):
                if len(self.dm_sources) >= 2000:
                    self.dm_sources.clear()

                source = video or document

                self.dm_sources[
                    (message.chat.id, message.message_id)
                ] = {
                    "kind": "video" if video else "document",
                    "file_id": source.file_id,
                    "caption": message.caption or "",
                    "entities": [
                        entity.to_dict()
                        for entity in (
                            message.caption_entities or ()
                        )
                    ],
                }

        return await original_media(
            self, update, context
        )

    async def copy_media_to_group(
        self, message, message_ids, context, target
    ):
        copied = await original_copy(
            self, message, message_ids, context, target
        )

        ids = sorted(set(message_ids))

        if copied and len(copied) != len(ids):
            await self.dm_notice(
                message,
                "Hanya sebagian media terkirim, le. "
                "Batch ini belum dicatat di katalog; "
                "kirim ulang video yang diperlukan "
                "satu per satu.",
            )

        if (
            message.chat.type == "private"
            and message.from_user.id == self.settings.owner_id
            and target == self.settings.target_group_id
            and len(copied) == len(ids)
        ):
            rows = []

            for source_id, output in zip(ids, copied):
                data = self.dm_sources.pop(
                    (message.chat.id, source_id), None
                )

                if data:
                    rows.append(
                        (output.message_id, data)
                    )

            if rows:
                try:
                    await asyncio.to_thread(
                        self.katalog.simpan,
                        context.bot.id,
                        self.settings.owner_id,
                        target,
                        rows,
                    )

                except (sqlite3.Error, OSError) as error:
                    logger.warning(
                        "Media terkirim tetapi katalog "
                        "gagal disimpan: jenis=%s",
                        type(error).__name__,
                    )

                    await self.dm_notice(
                        message,
                        "Media sudah masuk grup, tetapi katalog "
                        "kirim pribadinya belum tersimpan, le. "
                        "Minta admin cek penyimpanan.",
                    )

        return copied

    bot_class.__init__ = initialize
    bot_class.start = start
    bot_class.private_text = private_text
    bot_class.private_media = private_media
    bot_class.copy_media_to_group = copy_media_to_group

    for name in (
        "ngobrol_pribadi",
        "kirim_video_member",
        "dm_notice",
    ):
        setattr(
            bot_class,
            name,
            getattr(_ChatPribadi, name),
        )

    bot_class._chat_pribadi_terpasang = True
