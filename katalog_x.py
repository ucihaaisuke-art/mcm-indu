"""Daftarkan hasil unduh X ke katalog permintaan video grup utama."""

import asyncio
import json
import logging
import sqlite3

from telegram.error import BadRequest, RetryAfter

log = logging.getLogger(__name__)


class KatalogX:
    def __init__(self, controller, downloads):
        self.c = controller
        self.store = downloads.store
        self.original_send = downloads.send_batch
        downloads.send_batch = self.send_batch
        controller.kirim_video_x_lama = self.kirim_lama

    async def simpan(self, bot, messages, *, mids=None, caption=None):
        rows = []

        for offset, message in enumerate(messages):
            video = getattr(message, "video", None)
            file_id = getattr(video, "file_id", None)
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

        if not rows:
            return False

        try:
            await asyncio.to_thread(
                self.c.katalog.simpan,
                bot.id,
                self.c.settings.owner_id,
                self.c.settings.target_group_id,
                rows,
            )
            return True
        except (sqlite3.Error, OSError) as error:
            log.warning(
                "Video X terkirim; katalog belum tersimpan: %s",
                type(error).__name__,
            )
            return False

    async def send_batch(
        self, bot, message, request, group_id, thread_id,
        paths, file_ids, indices,
    ):
        sent = await self.original_send(
            bot, message, request, group_id, thread_id,
            paths, file_ids, indices,
        )

        if (
            request.publish
            and group_id == self.c.settings.target_group_id
            and len(sent) == len(indices)
        ):
            saved = await self.simpan(
                bot, sent, caption=request.caption
            )

            if not saved:
                await self.c.dm_notice(
                    message,
                    "Video sudah dikirim ke MCB, tetapi katalog "
                    "Minta Video belum tersimpan. Minta admin cek "
                    "penyimpanan; jangan ulangi kiriman grup dulu, le.",
                )

        return sent

    def dikenal(self, bot_id, mid):
        key = (
            bot_id,
            self.c.settings.owner_id,
            self.c.settings.target_group_id,
        )

        with self.store.lock:
            first = self.store.connection.execute(
                "SELECT 1 FROM mcm_x_routes "
                "WHERE bot_id=? AND owner_id=? AND target_id=? "
                "AND sent_message_id=? LIMIT 1",
                (*key, mid),
            ).fetchone()

            if first is not None:
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

    async def kirim_lama(self, message, context, mid):
        # Dipanggil setelah pemeriksaan grup, anggota dan admin.
        if not self.dikenal(context.bot.id, mid):
            return False

        try:
            for attempt in range(2):
                try:
                    sent = await context.bot.forward_message(
                        chat_id=message.chat.id,
                        from_chat_id=self.c.settings.target_group_id,
                        message_id=mid,
                    )
                    break
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

        except BadRequest:
            await self.c.dm_notice(
                message,
                "Video X lama itu belum bisa diambil dari grup, le. "
                "Pesannya mungkin sudah dihapus atau penerusan "
                "dibatasi. Admin perlu mengirim ulang sumber X lewat "
                "bot setelah pembaruan agar kiriman barunya masuk katalog.",
            )
            return True

        # Timeout tidak memicu pengiriman kedua.
        await self.simpan(context.bot, (sent,), mids=(mid,))
        return True


def pasang_katalog_x(controller, downloads):
    if not hasattr(downloads, "katalog_member"):
        downloads.katalog_member = KatalogX(controller, downloads)

    return downloads.katalog_member
