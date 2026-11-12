"""Pasangkan pesan grup MCB dengan sumber X untuk permintaan video anggota."""

import asyncio
import json
import logging
import re
import shutil
import sqlite3
import tempfile

if __package__:
    from .unduh_x import DownloadError, Request, parse_request
else:
    from unduh_x import DownloadError, Request, parse_request

log = logging.getLogger(__name__)
RESTORE = re.compile(
    r"\s*pulihkan\s+(?:sumber\s+)?x\s*[.!]?\s*", re.I
)


class KatalogX:
    def __init__(self, controller, downloads):
        self.c = controller
        self.x = downloads
        self.store = downloads.store
        self.original_send = downloads.send_batch
        self.original_owner = downloads.owner_text
        self.original_download = downloads.downloader
        self.download_lock = asyncio.Lock()

        downloads.send_batch = self.send_batch
        downloads.owner_text = self.owner_text
        downloads.downloader = self.download
        controller.kirim_video_x_lama = self.kirim_lama

        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "CREATE TABLE IF NOT EXISTS mcm_x_sources ("
                "bot_id INTEGER, owner_id INTEGER, group_id INTEGER, "
                "message_id INTEGER, url TEXT NOT NULL, "
                "caption TEXT NOT NULL, owner_message_id INTEGER NOT NULL, "
                "PRIMARY KEY(bot_id,owner_id,group_id,message_id))"
            )

    def key(self, bot_id):
        return (
            bot_id,
            self.c.settings.owner_id,
            self.c.settings.target_group_id,
        )

    def source(self, bot_id, mid):
        with self.store.lock:
            return self.store.connection.execute(
                "SELECT url,caption FROM mcm_x_sources "
                "WHERE bot_id=? AND owner_id=? "
                "AND group_id=? AND message_id=?",
                (*self.key(bot_id), mid),
            ).fetchone()

    def sources(self, bot_id, owner_mid, rows):
        values = [
            (*self.key(bot_id), mid, url, caption, owner_mid)
            for mid, url, caption in rows
        ]

        with self.store.lock, self.store.connection:
            for value in values:
                old = self.store.connection.execute(
                    "SELECT url FROM mcm_x_sources "
                    "WHERE bot_id=? AND owner_id=? "
                    "AND group_id=? AND message_id=?",
                    value[:4],
                ).fetchone()

                if old is not None and old[0] != value[4]:
                    raise ValueError(
                        "Sumber berbeda dari pemetaan yang sudah tersimpan."
                    )

            self.store.connection.executemany(
                "INSERT OR IGNORE INTO mcm_x_sources VALUES(?,?,?,?,?,?,?)",
                values,
            )

    async def simpan(self, bot, messages, *, mids=None, caption=None):
        rows = []

        for offset, message in enumerate(messages):
            file_id = getattr(
                getattr(message, "video", None), "file_id", None
            )
            mid = (
                mids[offset]
                if mids is not None
                else getattr(message, "message_id", None)
            )

            if (
                type(mid) is not int
                or mid <= 0
                or not isinstance(file_id, str)
                or not file_id
            ):
                return False

            text = getattr(message, "caption", None)
            if text is None:
                text = (caption or "") if offset == 0 else ""

            entities = getattr(message, "caption_entities", None) or ()

            rows.append((
                mid,
                {
                    "kind": "video",
                    "file_id": file_id,
                    "caption": text,
                    "entities": [
                        entity.to_dict() for entity in entities
                    ],
                },
            ))

        try:
            if not rows:
                return False

            await asyncio.to_thread(
                self.c.katalog.simpan,
                *self.key(bot.id),
                rows,
            )
            return True

        except (sqlite3.Error, OSError) as error:
            log.warning(
                "Video terkirim; katalog belum tersimpan: %s",
                type(error).__name__,
            )
            return False

    async def download(self, url, folder):
        async with self.download_lock:
            return await self.original_download(url, folder)

    async def send_batch(
        self, bot, message, request, group_id, thread_id,
        paths, file_ids, indices,
    ):
        sent = await self.original_send(
            bot, message, request, group_id, thread_id,
            paths, file_ids, indices,
        )

        if (
            not request.publish
            or group_id != self.c.settings.target_group_id
        ):
            return sent

        if len(sent) != len(indices) or any(
            type(getattr(item, "message_id", None)) is not int
            for item in sent
        ):
            return sent

        try:
            rows = [
                (
                    item.message_id,
                    request.links[index],
                    (
                        getattr(item, "caption", None)
                        or request.caption
                        or ""
                    ) if offset == 0 else "",
                )
                for offset, (index, item)
                in enumerate(zip(indices, sent))
            ]

            self.sources(bot.id, message.message_id, rows)
            saved = await self.simpan(
                bot, sent, caption=request.caption
            )

        except (sqlite3.Error, ValueError, OSError) as error:
            log.warning(
                "Video X terkirim; sumber belum tersimpan: %s",
                type(error).__name__,
            )
            saved = False

        if not saved:
            await self.c.dm_notice(
                message,
                "Video sudah masuk MCB, tetapi pencatatan sumber "
                "belum lengkap. Admin perlu memeriksa penyimpanan; "
                "jangan ulangi kiriman grup dulu, le.",
            )

        return sent

    def dikenal(self, bot_id, mid):
        key = self.key(bot_id)

        with self.store.lock:
            first = self.store.connection.execute(
                "SELECT 1 FROM mcm_x_routes "
                "WHERE bot_id=? AND owner_id=? AND target_id=? "
                "AND sent_message_id=? LIMIT 1",
                (*key, mid),
            ).fetchone()

            if first:
                return True

            rows = self.store.connection.execute(
                "SELECT sent_message_ids FROM mcm_x_batches "
                "WHERE bot_id=? AND owner_id=? AND target_id=? "
                "AND state='sent' AND sent_message_ids IS NOT NULL",
                key,
            )

            for row in rows:
                try:
                    ids = json.loads(row[0])
                except (TypeError, ValueError):
                    continue

                if isinstance(ids, list) and any(
                    type(value) is int and value == mid
                    for value in ids
                ):
                    return True

        return False

    def restore_rows(self, bot_id, owner_mid, request):
        key = (
            bot_id,
            self.c.settings.owner_id,
            owner_mid,
            self.c.settings.target_group_id,
        )

        with self.store.lock:
            batches = self.store.connection.execute(
                "SELECT batch_index,sent_message_ids "
                "FROM mcm_x_batches WHERE bot_id=? "
                "AND owner_id=? AND message_id=? AND target_id=? "
                "AND state='sent' ORDER BY batch_index",
                key,
            ).fetchall()

            if not batches:
                row = self.store.connection.execute(
                    "SELECT sent_message_id FROM mcm_x_routes "
                    "WHERE bot_id=? AND owner_id=? "
                    "AND message_id=? AND target_id=? "
                    "AND sent_message_id IS NOT NULL",
                    key,
                ).fetchone()

                if row is not None and len(request.links) == 1:
                    return [(
                        row[0],
                        request.links[0],
                        request.caption or "",
                    )]

                raise ValueError(
                    "Catatan kiriman ke MCB untuk pesan asli ini "
                    "tidak ditemukan."
                )

        result = []
        position = 0
        used = set()

        for expected, (batch, raw_ids) in enumerate(batches, 1):
            if batch != expected:
                raise ValueError(
                    "Urutan catatan album tidak lengkap; "
                    "pemulihan dihentikan."
                )

            try:
                ids = json.loads(raw_ids)
            except (TypeError, ValueError):
                raise ValueError(
                    "Catatan album tidak valid."
                ) from None

            if (
                not isinstance(ids, list)
                or not ids
                or position + len(ids) > len(request.links)
                or any(
                    type(mid) is not int
                    or mid <= 0
                    or mid in used
                    for mid in ids
                )
                or len(ids) != len(set(ids))
            ):
                raise ValueError(
                    "Jumlah atau urutan link tidak cocok "
                    "dengan kiriman lama."
                )

            for offset, mid in enumerate(ids):
                result.append((
                    mid,
                    request.links[position],
                    (request.caption or "") if offset == 0 else "",
                ))
                position += 1
                used.add(mid)

        return result

    async def owner_text(self, update, context):
        message = update.effective_message

        if (
            message is None
            or not RESTORE.fullmatch(message.text or "")
        ):
            return await self.original_owner(update, context)

        user = message.from_user

        if (
            update.edited_message is not None
            or message.chat.type != "private"
            or user is None
            or user.is_bot
            or user.id != self.c.settings.owner_id
            or message.chat.id != user.id
        ):
            return

        quoted = message.reply_to_message

        if (
            quoted is None
            or quoted.chat.id != user.id
            or quoted.from_user is None
            or quoted.from_user.id != user.id
            or getattr(quoted, "edit_date", None)
        ):
            await self.c.dm_notice(
                message,
                "Balas pesan link X ASLI yang dulu kamu kirim "
                "ke bot dengan PULIHKAN X, le. Gunakan pesan asli "
                "yang belum diedit, bukan salinan pesan baru.",
            )
            return

        try:
            request = parse_request(quoted.text or "")

            if request is None:
                raise ValueError(
                    "Pesan yang dibalas tidak berisi link X "
                    "yang dikenali."
                )

            rows = self.restore_rows(
                context.bot.id, quoted.message_id, request
            )
            self.sources(
                context.bot.id, quoted.message_id, rows
            )

        except (ValueError, sqlite3.Error) as error:
            text = (
                str(error)
                if isinstance(error, ValueError)
                else "Penyimpanan sumber sedang bermasalah."
            )
            await self.c.dm_notice(
                message, "Pemulihan belum selesai: " + text
            )
            return

        await self.c.dm_notice(
            message,
            f"Sumber X untuk {len(rows)} video lama sudah "
            "dipulihkan, le. Member sekarang bisa meminta "
            "lewat tautan video grup yang sama.",
        )

    async def kirim_lama(self, message, context, mid):
        row = self.source(context.bot.id, mid)

        if row is None:
            if not self.dikenal(context.bot.id, mid):
                return False

            text = (
                "Sumber video lama ini belum dipulihkan admin, le. "
                "Minta admin memulihkannya dulu."
            )

            if message.from_user.id == self.c.settings.owner_id:
                text += (
                    " Balas pesan link X asli yang dulu dikirim "
                    "ke bot dengan PULIHKAN X."
                )

            await self.c.dm_notice(message, text)
            return True

        url, caption = row
        parsed = parse_request(url)

        if (
            parsed is None
            or parsed.publish
            or parsed.links != (url,)
        ):
            await self.c.dm_notice(
                message,
                "Sumber video belum valid. "
                "Minta admin memeriksanya, le.",
            )
            return True

        await self.c.dm_notice(
            message,
            "Siap, le. Video sedang disiapkan dari sumber X. "
            "Tunggu sebentar.",
        )

        try:
            with tempfile.TemporaryDirectory(
                prefix="mcm-member-x-"
            ) as folder:
                if shutil.disk_usage(folder).free < 256 * 1024 * 1024:
                    raise DownloadError(
                        "Ruang server belum cukup untuk "
                        "menyiapkan video, le.",
                        code="DISK_FULL",
                    )

                path = await self.download(url, folder)

                member = await context.bot.get_chat_member(
                    self.c.settings.target_group_id,
                    message.from_user.id,
                )

                joined = member.status in (
                    "creator", "administrator", "member"
                ) or (
                    member.status == "restricted"
                    and getattr(member, "is_member", False)
                )

                if not joined:
                    await self.c.dm_notice(
                        message,
                        "Video hanya tersedia untuk anggota "
                        "yang masih bergabung di MCB, le.",
                    )
                    return True

                sent = await self.original_send(
                    context.bot,
                    message,
                    Request(url, False, caption),
                    message.chat.id,
                    None,
                    [path],
                    [None],
                    [0],
                )

                if len(sent) != 1:
                    await self.c.dm_notice(
                        message,
                        "Hasil pengiriman belum pasti. "
                        "Periksa chat ini sebelum mengulang, le.",
                    )
                    return True

                await self.simpan(
                    context.bot,
                    sent,
                    mids=(mid,),
                    caption=caption,
                )

        except DownloadError as error:
            await self.c.dm_notice(message, str(error))
        except OSError:
            await self.c.dm_notice(
                message,
                "Video belum bisa disiapkan di server. "
                "Coba lagi nanti, le.",
            )

        return True


def pasang_katalog_x(controller, downloads):
    if not hasattr(downloads, "katalog_member"):
        downloads.katalog_member = KatalogX(
            controller, downloads
        )

    return downloads.katalog_member
