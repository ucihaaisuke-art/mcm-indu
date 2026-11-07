"""Mode upload video pribadi owner ke dua tujuan VIP sesuai genre."""

import asyncio
import logging
import time

from telegram.error import BadRequest, NetworkError, TelegramError

if __package__:
    from .unduh_x import CAPTION, GENRE_TARGETS, resolve_routes
else:
    from unduh_x import CAPTION, GENRE_TARGETS, resolve_routes

logger = logging.getLogger(__name__)


class KirimVideoVIP:
    def __init__(self, controller, *, clock=time.time):
        self.c = controller
        self.store = controller.vip_store
        self.clock = clock
        self.tasks = set()
        self.lock = asyncio.Lock()
        self.blocked_albums = set()

        with self.store.lock, self.store.connection:
            self.store.connection.executescript("""
                CREATE TABLE IF NOT EXISTS mcm_upload_modes (
                    bot_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
                    genre TEXT NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY(bot_id,owner_id));

                CREATE TABLE IF NOT EXISTS mcm_upload_jobs (
                    bot_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
                    request_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
                    genre TEXT NOT NULL, state TEXT NOT NULL,
                    PRIMARY KEY(bot_id,owner_id,request_id));

                CREATE TABLE IF NOT EXISTS mcm_upload_routes (
                    bot_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
                    request_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL, state TEXT NOT NULL,
                    sent_message_id INTEGER,
                    PRIMARY KEY(
                        bot_id,owner_id,request_id,target_id,thread_id
                    ));
            """)

    def owner(self, message):
        user = getattr(message, "from_user", None)
        return bool(
            message
            and message.chat.type == "private"
            and user
            and not user.is_bot
            and user.id == self.c.settings.owner_id
            and message.chat.id == user.id
        )

    def mode(self, bot_id):
        with self.store.lock:
            return self.store.connection.execute(
                "SELECT genre,expires FROM mcm_upload_modes "
                "WHERE bot_id=? AND owner_id=?",
                (bot_id, self.c.settings.owner_id),
            ).fetchone()

    def set_mode(self, bot_id, genre=None):
        with self.store.lock, self.store.connection:
            if genre is None:
                self.store.connection.execute(
                    "DELETE FROM mcm_upload_modes "
                    "WHERE bot_id=? AND owner_id=?",
                    (bot_id, self.c.settings.owner_id),
                )
            else:
                self.store.connection.execute(
                    "INSERT OR REPLACE INTO mcm_upload_modes VALUES(?,?,?,?)",
                    (
                        bot_id,
                        self.c.settings.owner_id,
                        genre,
                        self.clock() + 900,
                    ),
                )

    @staticmethod
    def video(message):
        document = getattr(message, "document", None)
        return bool(
            getattr(message, "video", None)
            or (
                document
                and (document.mime_type or "").startswith("video/")
            )
        )

    async def command(self, update, context):
        message = update.effective_message
        if not self.owner(message) or update.edited_message is not None:
            return

        text = message.text or ""
        caption = CAPTION.search(text)
        prefix = text[:caption.start()] if caption else text
        tokens = prefix.split(maxsplit=1)
        genre = (
            tokens[1] if len(tokens) == 2 else ""
        ).strip().casefold()

        if genre in ("batal", "off"):
            self.set_mode(context.bot.id)
            await message.reply_text(
                "Mode upload VIP dimatikan, le. "
                "Unggahan berikut kembali mengikuti alur grup MCB utama. "
                "Kiriman yang sudah masuk antrean tetap diproses."
            )
            return

        if genre not in ("indo", "gay", "ai"):
            await message.reply_text(
                "Pilih /kirim INDO, /kirim GAY, atau /kirim AI, le. "
                "Lalu unggah video satu per pesan. "
                "/kirim batal untuk keluar dari mode VIP."
            )
            return

        if self.c.qris_setup(context.bot.id):
            await message.reply_text(
                "Selesaikan pemasangan QRIS atau ketik /batal dulu, le."
            )
            return

        quoted = getattr(message, "reply_to_message", None)
        if caption and not (
            quoted
            and quoted.chat.id == message.chat.id
            and self.video(quoted)
        ):
            await message.reply_text(
                "Caption ditulis saat mengunggah video, le. "
                "Untuk video yang sudah ada, balas videonya dengan "
                "kode INDO, GAY, atau AI dan baris caption:."
            )
            return

        try:
            self.routes(genre)
        except (ValueError, KeyError):
            await message.reply_text(
                "Tujuan VIP belum siap, le. Periksa ID grup dan topiknya."
            )
            return

        self.set_mode(context.bot.id, genre)
        await message.reply_text(
            f"Mode upload {genre.upper()} aktif 15 menit, le. "
            "Kirim video satu per pesan; caption ditulis biasa. "
            "/kirim batal untuk keluar dari mode VIP."
        )

        if (
            quoted
            and quoted.chat.id == message.chat.id
            and self.video(quoted)
        ):
            user = getattr(quoted, "from_user", None)
            if user and user.id in (
                self.c.settings.owner_id,
                context.bot.id,
            ):
                value = caption.group(1).rstrip() if caption else None
                await self.schedule(
                    quoted, message, context.bot, genre, value
                )

    async def reply_text(self, update, context):
        message = update.effective_message
        if not self.owner(message) or update.edited_message is not None:
            return False

        quoted = getattr(message, "reply_to_message", None)
        if (
            not quoted
            or quoted.chat.id != message.chat.id
            or not self.video(quoted)
        ):
            return False

        user = getattr(quoted, "from_user", None)
        if not user or user.id not in (
            self.c.settings.owner_id,
            context.bot.id,
        ):
            return False

        text = message.text or ""
        caption = CAPTION.search(text)
        prefix = text[:caption.start()] if caption else text
        genre = prefix.strip().casefold()

        if genre not in ("indo", "gay", "ai"):
            return False

        if self.c.qris_setup(context.bot.id):
            await message.reply_text(
                "Selesaikan pemasangan QRIS atau ketik /batal dulu, le."
            )
            return True

        value = caption.group(1).rstrip() if caption else None
        await self.schedule(
            quoted, message, context.bot, genre, value
        )
        return True

    def routes(self, genre):
        routes = resolve_routes(GENRE_TARGETS[genre])
        identities = {
            (chat_id, thread) for _, chat_id, thread in routes
        }

        if (
            len(routes) != 2
            or len(identities) != 2
            or len({chat_id for _, chat_id, _ in routes}) != 2
            or any(
                chat_id == self.c.settings.target_group_id
                for _, chat_id, _ in routes
            )
        ):
            raise ValueError("Tujuan VIP tidak valid.")

        return routes

    async def private_media(self, update, context):
        message = update.effective_message
        if not self.owner(message):
            return await self.c.private_media(update, context)

        if update.edited_message is not None:
            return

        if self.c.qris_setup(context.bot.id):
            return await self.c.private_media(update, context)

        album = getattr(message, "media_group_id", None)
        identity = (context.bot.id, message.chat.id, album)

        if album and identity in self.blocked_albums:
            return

        mode = self.mode(context.bot.id)
        if mode is None:
            return await self.c.private_media(update, context)

        if album:
            if len(self.blocked_albums) >= 2000:
                self.blocked_albums.clear()
            self.blocked_albums.add(identity)
            await message.reply_text(
                "Mode ini untuk satu video per pesan, le. "
                "Kirim ulang tanpa album."
            )
            return

        genre, expires = mode
        if expires <= self.clock():
            await message.reply_text(
                "Mode upload VIP sudah kedaluwarsa, le. "
                "Pilih /kirim INDO, /kirim GAY, atau /kirim AI lagi. "
                "/kirim batal untuk keluar."
            )
            return

        if not self.video(message):
            await message.reply_text(
                "Mode upload VIP sedang aktif, le. "
                "Kirim file video atau /kirim batal."
            )
            return

        await self.schedule(
            message, message, context.bot, genre
        )

    def state(self, key, value):
        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "UPDATE mcm_upload_jobs SET state=? "
                "WHERE bot_id=? AND owner_id=? AND request_id=?",
                (value, *key),
            )

    async def schedule(self, source, notice, bot, genre, caption=None):
        if getattr(source, "has_protected_content", False):
            await notice.reply_text(
                "Telegram membatasi penyalinan video ini, le."
            )
            return

        if (
            caption is not None
            and len(caption.encode("utf-16-le")) // 2 > 1024
        ):
            await notice.reply_text(
                "Caption terlalu panjang, le. "
                "Maksimal 1024 karakter Telegram."
            )
            return

        if len(self.tasks) >= 10:
            await notice.reply_text(
                "Antrean upload VIP penuh, le. "
                "Tunggu hasil kiriman sebelumnya."
            )
            return

        try:
            routes = self.routes(genre)
        except (ValueError, KeyError):
            await notice.reply_text(
                "Tujuan VIP belum siap, le. Periksa ID grup dan topiknya."
            )
            return

        key = (
            bot.id,
            self.c.settings.owner_id,
            notice.message_id,
        )
        with self.store.lock, self.store.connection:
            added = self.store.connection.execute(
                "INSERT OR IGNORE INTO mcm_upload_jobs VALUES(?,?,?,?,?,?)",
                (*key, source.message_id, genre, "queued"),
            ).rowcount

        if not added:
            await notice.reply_text(
                "Permintaan ini sudah dicatat, le. "
                "Cek hasilnya sebelum mengirim ulang."
            )
            return

        task = asyncio.create_task(
            self.deliver(
                source, notice, bot, routes, key, caption
            )
        )
        self.tasks.add(task)
        task.add_done_callback(self.finished)

    def finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning(
                "Upload VIP tertunda: jenis=%s",
                type(task.exception()).__name__,
            )

    async def deliver(
        self, source, notice, bot, routes, key, caption=None
    ):
        try:
            async with self.lock:
                self.state(key, "sending")

                try:
                    await notice.reply_text(
                        "Bentar le, aku salin videonya.\nTujuan: "
                        + ", ".join(name for name, _, _ in routes)
                    )
                except TelegramError:
                    pass

                results = []
                for name, chat_id, thread in routes:
                    with self.store.lock, self.store.connection:
                        added = self.store.connection.execute(
                            "INSERT OR IGNORE INTO mcm_upload_routes "
                            "VALUES(?,?,?,?,?,?,NULL)",
                            (*key, chat_id, thread or 0, "sending"),
                        ).rowcount

                    if not added:
                        continue

                    state, sent_id = "sent", None
                    try:
                        options = dict(
                            chat_id=chat_id,
                            from_chat_id=source.chat.id,
                            message_id=source.message_id,
                            read_timeout=120,
                            write_timeout=120,
                            connect_timeout=20,
                            pool_timeout=10,
                        )
                        if thread is not None:
                            options["message_thread_id"] = thread
                        if caption is not None:
                            options["caption"] = caption
                            options["parse_mode"] = None

                        sent = await bot.copy_message(**options)
                        sent_id = sent.message_id
                        results.append(f"✅ {name}")

                    except TelegramError as error:
                        state = (
                            "uncertain"
                            if isinstance(error, NetworkError)
                            and not isinstance(error, BadRequest)
                            else "failed"
                        )
                        reason = (
                            "hasil kirim belum pasti; cek tujuan dulu"
                            if state == "uncertain"
                            else (
                                f"ditolak Telegram ({type(error).__name__}); "
                                "cek izin dan ID tujuan"
                            )
                        )
                        results.append(f"⚠️ {name}: {reason}")
                        logger.warning(
                            "Salin video VIP tertunda: jenis=%s",
                            type(error).__name__,
                        )

                    with self.store.lock, self.store.connection:
                        self.store.connection.execute(
                            "UPDATE mcm_upload_routes "
                            "SET state=?,sent_message_id=? "
                            "WHERE bot_id=? AND owner_id=? AND request_id=? "
                            "AND target_id=? AND thread_id=?",
                            (
                                state,
                                sent_id,
                                *key,
                                chat_id,
                                thread or 0,
                            ),
                        )

                self.state(key, "finished")
                await notice.reply_text(
                    "Hasil kirim video VIP:\n"
                    + "\n".join(results)
                )

        except asyncio.CancelledError:
            self.state(key, "interrupted")
            raise

        except TelegramError as error:
            logger.warning(
                "Laporan upload VIP tertunda: jenis=%s",
                type(error).__name__,
            )

    async def stop(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
