"""Pesan pembayaran dari owner melalui reply ke pemeriksaan pesanan VIP."""

import asyncio
import logging
import re
import time

from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import ApplicationHandlerStop, MessageHandler, filters

if __package__:
    from .vip_mcm import PaymentError, PLANS
else:
    from vip_mcm import PaymentError, PLANS

logger = logging.getLogger(__name__)
COMMAND = re.compile(r"^(kurang|pesan|ingatkan)(?:\s+(.*))?$", re.I | re.S)


def uang(amount):
    return f"Rp{amount:,}".replace(",", ".")


def nominal(text):
    value = re.sub(r"^rp\s*", "", text.strip(), flags=re.I)
    if re.fullmatch(r"\d{1,9}k", value, re.I):
        return int(value[:-1]) * 1000
    if re.fullmatch(r"\d{1,12}|[1-9]\d{0,2}(?:\.\d{3}){1,3}", value):
        return int(value.replace(".", ""))
    raise PaymentError(
        "Tulis total uang yang sudah masuk, bos. "
        "Contoh: kurang 30000 atau kurang 30k."
    )


class ResponPembayaran:
    def __init__(self, controller):
        self.c = controller
        self.store = controller.vip_store
        self.payments = controller.vip.payments
        with self.store.lock, self.store.connection:
            self.store.connection.execute("""
                CREATE TABLE IF NOT EXISTS mcm_payment_responses (
                    bot_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
                    request_id INTEGER NOT NULL, review_id INTEGER NOT NULL,
                    order_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    kind TEXT NOT NULL, received_total INTEGER, body TEXT NOT NULL,
                    state TEXT NOT NULL, sent_message_id INTEGER, error_type TEXT,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(bot_id,owner_id,request_id))
            """)

    def resolve(self, message, bot):
        quoted = message.reply_to_message
        if (
            quoted is None
            or quoted.from_user is None
            or quoted.from_user.id != bot.id
            or quoted.chat.id != message.chat.id
        ):
            raise PaymentError(
                "Pakai Reply/Balas pada pesan pemeriksaan "
                "bukti pembayaran dari bot, bos."
            )
        with self.store.lock:
            row = self.store.connection.execute(
                "SELECT order_id,revision FROM manual_reviews "
                "WHERE owner_id=? AND bot_id=? AND message_id=?",
                (
                    self.c.settings.owner_id,
                    bot.id,
                    quoted.message_id,
                ),
            ).fetchone()
        if row is None:
            raise PaymentError(
                "Itu bukan pesan pemeriksaan pembayaran yang terdaftar, bos."
            )
        order = self.payments.order(row["order_id"])
        if order["state"] != "pending":
            raise PaymentError(
                "Pesanan ini sudah selesai. "
                "Pemberitahuan pembayaran tertunda tidak dikirim."
            )
        if order["revision"] != row["revision"]:
            raise PaymentError(
                "Bukti sudah diperbarui. "
                "Balas pesan pemeriksaan yang paling baru, bos."
            )
        return order

    def shortfall(self, order, received):
        expected = order["amount"]
        if received >= expected:
            raise PaymentError(
                f"Nominal itu sudah mencapai harga pesanan {uang(expected)}, bos. "
                "Kalau uangnya benar-benar lengkap dan masuk, "
                "balas done pada pemeriksaannya."
            )
        plan = PLANS.get(order["plan_id"])
        name = plan.name if plan else order["plan_id"]
        instruction = (
            "Lengkapi kekurangannya sesuai QRIS pesanan. "
            "Tidak perlu membayar harga penuh lagi. "
            if received
            else "Cek status transfer sebelumnya dahulu agar tidak terbayar dua kali. "
            "Jika belum ada pembayaran berhasil, bayar sesuai nominal pesanan. "
        )
        return (
            f"Le, pembayaran paketmu belum lengkap.\n\n"
            f"Paket: {name}\n"
            f"Harga pesanan: {uang(expected)}\n"
            f"Total masuk menurut pengecekan owner: {uang(received)}\n"
            f"Kekurangan: {uang(expected - received)}\n"
            f"Pesanan: {order['order_id']}\n\n"
            + instruction
            + "Balas pesan ini dengan bukti pembayaran tambahan dan Nama pengirim. "
            "Akses menunggu owner memeriksa pembayaran lengkap."
        )

    def compose(self, kind, argument, order, bot):
        if kind == "kurang":
            received = nominal(argument)
            return self.shortfall(order, received), received

        if kind == "ingatkan":
            if argument:
                raise PaymentError(
                    "Cukup tulis ingatkan sebagai balasan "
                    "ke pemeriksaan pesanan, bos."
                )
            with self.store.lock:
                row = self.store.connection.execute(
                    "SELECT received_total FROM mcm_payment_responses "
                    "WHERE bot_id=? AND owner_id=? "
                    "AND order_id=? AND revision=? AND kind='kurang' "
                    "ORDER BY rowid DESC LIMIT 1",
                    (
                        bot.id,
                        self.c.settings.owner_id,
                        order["order_id"],
                        order["revision"],
                    ),
                ).fetchone()
            if row is None:
                raise PaymentError(
                    "Belum ada catatan kekurangan pada bukti ini. "
                    "Pakai kurang 30000 sesuai total yang sudah masuk."
                )
            return self.shortfall(order, row[0]), row[0]

        if not argument or len(argument.encode("utf-16-le")) // 2 > 3000:
            raise PaymentError(
                "Tulis pesan di belakang kata pesan, bos. "
                "Maksimal 3000 karakter Telegram."
            )
        plan = PLANS.get(order["plan_id"])
        name = plan.name if plan else order["plan_id"]
        return (
            f"Pesan dari owner untuk pesanan VIP kamu.\n"
            f"Paket: {name}\n"
            f"Pesanan: {order['order_id']}\n\n"
            f"{argument}\n\n"
            "Pesan ini bukan konfirmasi lunas. "
            "Akses tetap menunggu pengecekan pembayaran."
        ), None

    async def handle(self, update, context):
        message = update.effective_message
        user = getattr(message, "from_user", None)
        if (
            message is None
            or update.edited_message is not None
            or message.chat.type != "private"
            or user is None
            or user.is_bot
            or user.id != self.c.settings.owner_id
            or message.chat.id != user.id
        ):
            return
        match = COMMAND.fullmatch((message.text or "").strip())
        if match is None:
            return
        kind = match.group(1).casefold()
        argument = (match.group(2) or "").strip()
        try:
            async with self.c.vip_private_locks.setdefault(
                user.id, asyncio.Lock()
            ):
                await self.respond(message, context.bot, kind, argument)
        except PaymentError as error:
            await self.ack(message, str(error))
        except TelegramError as error:
            logger.warning(
                "Laporan pembayaran ke owner tertunda: jenis=%s",
                type(error).__name__,
            )
        raise ApplicationHandlerStop

    async def ack(self, message, text):
        try:
            await message.reply_text(text, parse_mode=None)
        except TelegramError as error:
            logger.warning(
                "Laporan pembayaran ke owner tertunda: jenis=%s",
                type(error).__name__,
            )

    async def respond(self, message, bot, kind, argument):
        order = self.resolve(message, bot)
        key = (
            bot.id,
            self.c.settings.owner_id,
            message.message_id,
        )
        with self.store.lock:
            previous = self.store.connection.execute(
                "SELECT state FROM mcm_payment_responses "
                "WHERE bot_id=? AND owner_id=? AND request_id=?",
                key,
            ).fetchone()
        if previous:
            await self.ack(
                message,
                "Permintaan ini sudah dicatat, bos. "
                "Tidak dikirim ulang otomatis; cek hasil sebelumnya.",
            )
            return

        body, received = self.compose(kind, argument, order, bot)
        with self.store.lock, self.store.connection:
            claimed = self.store.connection.execute(
                "INSERT OR IGNORE INTO mcm_payment_responses "
                "(bot_id,owner_id,request_id,review_id,order_id,revision,"
                "kind,received_total,body,state,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'sending',?)",
                (
                    *key,
                    message.reply_to_message.message_id,
                    order["order_id"],
                    order["revision"],
                    kind,
                    received,
                    body,
                    int(time.time()),
                ),
            ).rowcount
        if not claimed:
            await self.ack(
                message,
                "Permintaan ini sudah dicatat, bos. Cek hasil sebelumnya.",
            )
            return

        try:
            sent = await bot.send_message(
                chat_id=order["user_id"],
                text=body,
                parse_mode=None,
                disable_web_page_preview=True,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=15,
                pool_timeout=10,
            )
        except TelegramError as error:
            uncertain = (
                isinstance(error, NetworkError)
                and not isinstance(error, BadRequest)
            )
            with self.store.lock, self.store.connection:
                self.store.connection.execute(
                    "UPDATE mcm_payment_responses SET state=?,error_type=? "
                    "WHERE bot_id=? AND owner_id=? AND request_id=?",
                    (
                        "uncertain" if uncertain else "failed",
                        type(error).__name__,
                        *key,
                    ),
                )
            logger.warning(
                "Pesan pembayaran ke pembeli tertunda: jenis=%s",
                type(error).__name__,
            )
            await self.ack(
                message,
                "Hasil kirim belum pasti, bos. "
                "Cek dulu sebelum meminta kirim ulang."
                if uncertain
                else "Pesan belum berhasil dikirim, bos. "
                "Pembeli mungkin memblokir bot; periksa akses chatnya.",
            )
            return

        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "UPDATE mcm_payment_responses "
                "SET state='sent',sent_message_id=? "
                "WHERE bot_id=? AND owner_id=? AND request_id=?",
                (sent.message_id, *key),
            )
            self.payments.prompt(
                order["user_id"], sent.message_id, order["order_id"]
            )
        self.c.vip_modes[order["user_id"]] = "vip"
        summary = (
            f" Total masuk {uang(received)}, "
            f"kekurangan {uang(order['amount'] - received)}."
            if received is not None
            else ""
        )
        await self.ack(
            message,
            f"Pesan terkirim ke pembeli pesanan {order['order_id']}, bos."
            f"{summary} Akses belum dibuka.",
        )


def pasang_respon_pembayaran(application, controller):
    existing = getattr(controller, "payment_responses", None)
    if existing is not None:
        return existing
    service = ResponPembayaran(controller)

    async def receive(update, context):
        await service.handle(update, context)

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.UpdateType.MESSAGE
            & filters.TEXT
            & ~filters.COMMAND
            & filters.User(user_id=controller.settings.owner_id),
            receive,
        ),
        group=-4,
    )
    controller.payment_responses = service
    logger.info(
        "Respon pembayaran owner aktif: kurang, pesan, ingatkan."
    )
    return service
