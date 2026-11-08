"""Pesan pembayaran dan laporan transaksi DONE khusus owner."""

import asyncio
import csv
import io
import logging
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

from telegram import InputFile
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import ApplicationHandlerStop, MessageHandler, filters

if __package__:
    from .vip_mcm import PaymentError, PLANS
else:
    from vip_mcm import PaymentError, PLANS

logger = logging.getLogger(__name__)

COMMAND = re.compile(
    r"^(kurang|pesan|ingatkan)(?:\s+(.*))?$",
    re.I | re.S,
)

REPORT = re.compile(
    r"^(?:(?:mak|emak)[,\s]+)?"
    r"(?:total\s+(?:dana\s+masuk|pembayaran|transaksi|pemasukan)"
    r"|(?:cek\s+)?mutasi"
    r"|rekap(?:\s+(?:pembayaran|transaksi))?)"
    r"(?:\s+(.*))?$",
    re.I | re.S,
)

WIB = timezone(timedelta(hours=7))
MAX_REPORT_ROWS = 5000


def report_period(argument, now):
    """Rentang tanggal WIB, awal inklusif dan akhir eksklusif."""
    today = datetime.fromtimestamp(now, WIB).date()
    text = re.sub(
        r"\s+", " ", argument.casefold()
    ).strip().rstrip(".!?")

    if text in ("", "hari ini", "hariini", "sekarang"):
        first = last = today

    elif text == "kemarin":
        first = last = today - timedelta(days=1)

    else:
        match = re.fullmatch(
            r"([1-9]\d{0,2})"
            r"(?:\s*hari(?:\s+(?:terakhir|ke\s*belakang|belakangan|"
            r"(?:yang\s+)?lalu))?)?",
            text,
        )

        if match:
            days = int(match.group(1))

            if days > 366:
                raise PaymentError(
                    "Maksimal 366 hari per laporan, bos."
                )

            first = today - timedelta(days=days - 1)
            last = today

        else:
            parts = re.sub(
                r"\b(?:tanggal|sampai|sd)\b|s/d",
                " ",
                text,
            ).split()

            if not 1 <= len(parts) <= 2:
                raise PaymentError(
                    "Pakai hari ini, kemarin, 7 hari ke belakang, "
                    "atau tanggal 01-11-2026 sampai 07-11-2026, bos."
                )

            parsed = []

            for value in parts:
                try:
                    if re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}",
                        value,
                    ):
                        parsed.append(
                            date.fromisoformat(value)
                        )

                    elif re.fullmatch(
                        r"\d{2}-\d{2}-\d{4}",
                        value,
                    ):
                        parsed.append(
                            datetime.strptime(
                                value,
                                "%d-%m-%Y",
                            ).date()
                        )

                    else:
                        raise ValueError

                except ValueError:
                    raise PaymentError(
                        "Tanggal harus DD-MM-YYYY "
                        "atau YYYY-MM-DD, bos."
                    ) from None

            first = parsed[0]
            last = parsed[-1]

    if (
        last < first
        or last > today
        or (last - first).days >= 366
    ):
        raise PaymentError(
            "Periksa urutan tanggal, bos. "
            "Maksimal 366 hari dan tanggal akhir "
            "tidak melewati hari ini."
        )

    start = int(
        datetime.combine(
            first,
            datetime.min.time(),
            WIB,
        ).timestamp()
    )

    end = min(
        int(
            datetime.combine(
                last + timedelta(days=1),
                datetime.min.time(),
                WIB,
            ).timestamp()
        ),
        int(now) + 1,
    )

    return first, last, start, end


def csv_cell(value):
    text = str(value or "")

    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text

    return text


def uang(amount):
    return f"Rp{amount:,}".replace(",", ".")


def nominal(text):
    value = re.sub(
        r"^rp\s*",
        "",
        text.strip(),
        flags=re.I,
    )

    if re.fullmatch(r"\d{1,9}k", value, re.I):
        return int(value[:-1]) * 1000

    if re.fullmatch(
        r"\d{1,12}|[1-9]\d{0,2}(?:\.\d{3}){1,3}",
        value,
    ):
        return int(value.replace(".", ""))

    raise PaymentError(
        "Tulis total uang yang sudah masuk, bos. "
        "Contoh: kurang 30000 atau kurang 30k."
    )


class ResponPembayaran:
    def __init__(self, controller, *, clock=time.time):
        self.c = controller
        self.store = controller.vip_store
        self.payments = controller.vip.payments
        self.clock = clock

        with self.store.lock, self.store.connection:
            self.store.connection.execute("""
                CREATE TABLE IF NOT EXISTS mcm_payment_responses (
                    bot_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    request_id INTEGER NOT NULL,
                    review_id INTEGER NOT NULL,
                    order_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    received_total INTEGER,
                    body TEXT NOT NULL,
                    state TEXT NOT NULL,
                    sent_message_id INTEGER,
                    error_type TEXT,
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
                "Itu bukan pesan pemeriksaan pembayaran "
                "yang terdaftar, bos."
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
                f"Nominal itu sudah mencapai harga pesanan "
                f"{uang(expected)}, bos. Kalau uangnya "
                "benar-benar lengkap dan masuk, "
                "balas done pada pemeriksaannya."
            )

        plan = PLANS.get(order["plan_id"])
        name = plan.name if plan else order["plan_id"]

        instruction = (
            "Lengkapi kekurangannya sesuai QRIS pesanan. "
            "Tidak perlu membayar harga penuh lagi. "
            if received
            else "Cek status transfer sebelumnya dahulu "
            "agar tidak terbayar dua kali. "
            "Jika belum ada pembayaran berhasil, "
            "bayar sesuai nominal pesanan. "
        )

        return (
            f"Le, pembayaran paketmu belum lengkap.\n\n"
            f"Paket: {name}\n"
            f"Harga pesanan: {uang(expected)}\n"
            f"Total masuk menurut pengecekan owner: "
            f"{uang(received)}\n"
            f"Kekurangan: {uang(expected - received)}\n"
            f"Pesanan: {order['order_id']}\n\n"
            + instruction
            + "Balas pesan ini dengan bukti pembayaran tambahan "
            "dan Nama pengirim. Akses menunggu owner "
            "memeriksa pembayaran lengkap."
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
                    "SELECT received_total "
                    "FROM mcm_payment_responses "
                    "WHERE bot_id=? AND owner_id=? "
                    "AND order_id=? AND revision=? "
                    "AND kind='kurang' "
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
                    "Pakai kurang 30000 sesuai total "
                    "yang sudah masuk."
                )

            return self.shortfall(order, row[0]), row[0]

        if (
            not argument
            or len(argument.encode("utf-16-le")) // 2 > 3000
        ):
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

        text = (message.text or "").strip()
        match = COMMAND.fullmatch(text)
        report = REPORT.fullmatch(text)

        if match is None and report is None:
            return

        try:
            async with self.c.vip_private_locks.setdefault(
                user.id,
                asyncio.Lock(),
            ):
                if report is not None:
                    await self.report(
                        message,
                        context.bot,
                        (report.group(1) or "").strip(),
                    )

                else:
                    kind = match.group(1).casefold()
                    argument = (match.group(2) or "").strip()

                    await self.respond(
                        message,
                        context.bot,
                        kind,
                        argument,
                    )

        except PaymentError as error:
            await self.ack(message, str(error))

        except TelegramError as error:
            logger.warning(
                "Laporan pembayaran ke owner tertunda: jenis=%s",
                type(error).__name__,
            )

        raise ApplicationHandlerStop

    def report_data(self, bot_id, start, end):
        params = {
            "owner": self.c.settings.owner_id,
            "bot": bot_id,
            "start": start,
            "end": end,
        }

        scope = """
            WITH reviews AS (
                SELECT
                    order_id,
                    revision,
                    COUNT(DISTINCT bot_id) AS bot_count,
                    MAX(
                        CASE WHEN bot_id=:bot THEN 1 ELSE 0 END
                    ) AS this_bot
                FROM manual_reviews
                WHERE owner_id=:owner
                GROUP BY order_id,revision
            ), approved AS (
                SELECT
                    o.order_id,
                    o.user_id,
                    o.amount,
                    m.sender_name,
                    m.plan_id,
                    m.approved_at,
                    v.bot_count
                FROM orders o
                JOIN manual_orders m USING(order_id)
                JOIN reviews v
                    ON v.order_id=o.order_id
                    AND v.revision=m.revision
                WHERE o.state='paid'
                    AND m.owner_id=:owner
                    AND m.approved_by=:owner
                    AND o.charge_id='manual:' || o.order_id
                    AND m.approved_at>=:start
                    AND m.approved_at<:end
                    AND EXISTS(
                        SELECT 1
                        FROM manual_decisions d
                        WHERE d.order_id=o.order_id
                            AND d.owner_id=:owner
                            AND d.decision='done'
                            AND d.revision=m.revision
                    )
                    AND v.this_bot=1
            )
        """

        cte = (
            scope
            + ", paid AS ("
            "SELECT * FROM approved WHERE bot_count=1"
            ") "
        )

        with self.store.lock:
            summary = self.store.connection.execute(
                cte
                + "SELECT COUNT(*) AS count,"
                "COALESCE(SUM(amount),0) AS amount FROM paid",
                params,
            ).fetchone()

            days = self.store.connection.execute(
                cte
                + "SELECT date(approved_at,'unixepoch','+7 hours') "
                "AS day,COUNT(*) AS count,SUM(amount) AS amount "
                "FROM paid GROUP BY day ORDER BY day",
                params,
            ).fetchall()

            plans = self.store.connection.execute(
                cte
                + "SELECT plan_id,COUNT(*) AS count,"
                "SUM(amount) AS amount FROM paid "
                "GROUP BY plan_id ORDER BY amount DESC,plan_id",
                params,
            ).fetchall()

            rows = self.store.connection.execute(
                cte
                + "SELECT * FROM paid "
                "ORDER BY approved_at DESC,order_id DESC "
                "LIMIT :maximum",
                dict(params, maximum=MAX_REPORT_ROWS),
            ).fetchall()

            uncertain = self.store.connection.execute(
                scope
                + " SELECT COUNT(*) FROM approved "
                "WHERE bot_count>1",
                params,
            ).fetchone()[0]

        return summary, days, plans, rows, uncertain

    async def report(self, message, bot, argument):
        first, last, start, end = report_period(
            argument,
            self.clock(),
        )

        try:
            summary, days, plans, rows, uncertain = (
                self.report_data(bot.id, start, end)
            )

        except sqlite3.Error as error:
            logger.warning(
                "Laporan transaksi belum terbaca: jenis=%s",
                type(error).__name__,
            )
            await self.ack(
                message,
                "Catatan transaksi belum bisa dibaca, bos. "
                "Periksa database VIP dan Deploy Logs.",
            )
            return

        count = summary["count"]
        total = summary["amount"]
        period = first.strftime("%d-%m-%Y")

        if first != last:
            period += " sampai " + last.strftime("%d-%m-%Y")

        lines = [
            "REKAP PEMBAYARAN DONE",
            "Periode: " + period + " (WIB)",
            f"Jumlah: {count} transaksi",
            f"Total: {uang(total)}",
        ]

        if first != last:
            lines.append(
                "\nPer hari"
                + (
                    " (7 hari terakhir pada rentang ini):"
                    if (last - first).days >= 7
                    else ":"
                )
            )

            by_day = {
                row["day"]: row
                for row in days
            }

            day = max(
                first,
                last - timedelta(days=6),
            )

            while day <= last:
                row = by_day.get(day.isoformat())
                day_count = row["count"] if row else 0
                day_amount = row["amount"] if row else 0

                lines.append(
                    f"{day:%d-%m}: {day_count} transaksi "
                    f"— {uang(day_amount)}"
                )
                day += timedelta(days=1)

        if plans:
            lines.append("\nPer paket:")

            for row in plans[:10]:
                plan = PLANS.get(row["plan_id"])
                name = (
                    plan.name
                    if plan
                    else str(row["plan_id"])[:45]
                )

                lines.append(
                    f"{name}: {row['count']} transaksi "
                    f"— {uang(row['amount'])}"
                )

        if rows:
            lines.append("\nTransaksi terbaru:")

            for row in rows[:5]:
                stamp = datetime.fromtimestamp(
                    row["approved_at"],
                    WIB,
                )

                name = " ".join(
                    str(
                        row["sender_name"] or "Tanpa nama"
                    ).split()
                )[:32]

                lines.append(
                    f"{stamp:%d-%m %H:%M} | "
                    f"{name} | {uang(row['amount'])}"
                )

        if uncertain:
            lines.append(
                f"\n{uncertain} pesanan diperiksa lewat beberapa bot; "
                "dikecualikan agar tidak salah mengaitkan transaksi."
            )

        lines.append(
            "\nNominal mengikuti harga pesanan yang di-DONE owner. "
            "Ini catatan bot, bukan mutasi rekening bank."
        )

        await message.reply_text(
            "\n".join(lines),
            parse_mode=None,
        )

        if rows:
            output = io.StringIO(newline="")
            writer = csv.writer(output)

            writer.writerow((
                "Waktu DONE (WIB)",
                "ID Pesanan",
                "ID Member",
                "Nama Pengirim",
                "Paket",
                "Nominal Rupiah",
            ))

            for row in rows:
                plan = PLANS.get(row["plan_id"])

                writer.writerow((
                    datetime.fromtimestamp(
                        row["approved_at"],
                        WIB,
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                    row["order_id"],
                    row["user_id"],
                    csv_cell(row["sender_name"]),
                    csv_cell(
                        plan.name
                        if plan
                        else row["plan_id"]
                    ),
                    row["amount"],
                ))

            filename = (
                f"transaksi-done-"
                f"{first:%Y%m%d}-{last:%Y%m%d}.csv"
            )

            caption = (
                f"Rincian {len(rows)} dari {count} transaksi DONE. "
                f"Total seluruh periode: {uang(total)}."
            )

            if count > MAX_REPORT_ROWS:
                caption += (
                    " CSV dibatasi 5.000 transaksi terbaru; "
                    "gunakan rentang tanggal lebih pendek "
                    "untuk sisanya."
                )

            await bot.send_document(
                chat_id=message.from_user.id,
                document=InputFile(
                    output.getvalue().encode("utf-8-sig"),
                    filename=filename,
                ),
                caption=caption,
                parse_mode=None,
            )

    async def ack(self, message, text):
        try:
            await message.reply_text(
                text,
                parse_mode=None,
            )

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
                "Tidak dikirim ulang otomatis; "
                "cek hasil sebelumnya.",
            )
            return

        body, received = self.compose(
            kind,
            argument,
            order,
            bot,
        )

        with self.store.lock, self.store.connection:
            claimed = self.store.connection.execute(
                "INSERT OR IGNORE INTO mcm_payment_responses "
                "(bot_id,owner_id,request_id,review_id,order_id,"
                "revision,kind,received_total,body,state,created_at) "
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
                "Permintaan ini sudah dicatat, bos. "
                "Cek hasil sebelumnya.",
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
                    "UPDATE mcm_payment_responses "
                    "SET state=?,error_type=? "
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
                "Pembeli mungkin memblokir bot; "
                "periksa akses chatnya.",
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
                order["user_id"],
                sent.message_id,
                order["order_id"],
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
            f"Pesan terkirim ke pembeli pesanan "
            f"{order['order_id']}, bos."
            f"{summary} Akses belum dibuka.",
        )


def pasang_respon_pembayaran(application, controller):
    existing = getattr(
        controller,
        "payment_responses",
        None,
    )

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
            & filters.User(
                user_id=controller.settings.owner_id
            ),
            receive,
        ),
        group=-4,
    )

    controller.payment_responses = service

    logger.info(
        "Respon pembayaran owner aktif: "
        "kurang, pesan, ingatkan, rekap DONE."
    )

    return service
