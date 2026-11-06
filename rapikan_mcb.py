"""Pengingat VIP, penghapusan chat sementara, dan gaya dialog teman Emak."""

import asyncio
import logging
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import ApplicationHandlerStop

if __package__:
    from .teman_ngobrol import ALIASES, NAMES, TemanNgobrol
    from .vip_mcm import PLANS
else:
    from teman_ngobrol import ALIASES, NAMES, TemanNgobrol
    from vip_mcm import PLANS

logger = logging.getLogger(__name__)

VIP_PATTERN = re.compile(
    r"(?<!\w)/(?:vip|akses|cek|pending|setqris|batal)(?:@\w+)?\b|"
    r"\b(?:vip|qris|paket\s+(?:grup|premium|lengkap)|"
    r"bukti\s+(?:bayar|transfer))\b",
    re.I,
)

NO_NEED = re.compile(
    r"^\s*"
    r"(?:(?:dit+|adit+|nis+|den{1,2}is+)\s*[,!]?\s*)?"
    r"(?:ga|gak|nggak|enggak|tidak|tak)\s*"
    r"(?:ada|perlu|usah)"
    r"(?:\s+(?:apa|apa-apa|apaapa|lagi|kok|aja|doang|"
    r"makasih|mksh|bang|bangku|abangku|le|"
    r"dit+|adit+|nis+|den{1,2}is+))*"
    r"[\s,.!?]*$",
    re.I,
)

DAILY_DIALOGS = (
    (
        "Den, orang bilang duit ga dibawa mati.",
        "Tapi kalau ga ada duit, hidup juga sering dibawa pusing, Dit 😂",
        "Makanya atur pengeluaran, bang. Bukan tiap diskon dianggap panggilan jiwa.",
    ),
    (
        "Bah, aku baru sadar: notifikasi paling ditunggu itu beda tiap umur.",
        "Kalau aku tetap sama, Dit. Saldo masuk. Konsisten kali aku 😂",
        "Yang penting bukan notifikasi pinjaman jatuh tempo, kawan.",
    ),
    (
        "Kalau kita bisa baca pikiran orang, hidup bakal lebih gampang ga?",
        "Jangan, Dit. Pikiranku sendiri aja kadang ga jelas isinya 😂",
        "Baca petunjuk dulu aja masih suka salah, udah mau baca pikiran orang.",
    ),
    (
        "Den, kenapa sandal gampang kali ketukar pas rame?",
        "Karena sandalnya lebih gampang akrab daripada yang punya, Dit 😂",
        "Kasih tanda lah, bang. Jangan tiap pulang malah tukar identitas sandal.",
    ),
    (
        "Bah, kalau lemari bisa protes, yang pertama dibilang apa ya?",
        "Bajumu banyak, tapi tiap keluar bilang ga punya baju, Dit 😂",
        "Itu bukan kurang baju. Kebanyakan mikir komentar orang, kawan.",
    ),
    (
        "Katanya ide bagus datang waktu kita santai.",
        "Pantes pas dikejar deadline yang datang malah keringat, Dit 😂",
        "Kerjain dikit-dikit lah, bang. Jangan nunggu ide turun naik angkot.",
    ),
    (
        "Den, kalau kucing punya grup chat, bahas apa ya?",
        "Review manusia: makannya telat, tapi minta disayang terus 😂",
        "Bisa jadi Emak yang dikasih rating, padahal Emak yang beli makanannya.",
    ),
    (
        "Aku heran, niat tidur cepat sering kalah sama satu video lagi.",
        "Satu video itu satuan waktu yang bisa berubah jadi dua jam, Dit 😂",
        "Letak HP-nya jauh dikit, bang. Bukan bantal yang disalahkan.",
    ),
    (
        "Kalau dompet bisa kasih notifikasi, hidup bakal lebih tertib ga ya?",
        "Dompetku paling cuma bilang: jangan dulu, jangan dulu, jangan dulu 😂",
        "Bagus juga. Biar ada yang berani ngelarang selain Emak.",
    ),
    (
        "Bah, ada orang hafal semua promo tapi lupa tanggal penting.",
        "Kalau promo tanggalnya merah gede, Dit. Bantu desain dulu tanggal pentingnya 😂",
        "Pasang pengingat lah, kawan. Ingatan jangan cuma dipakai nyimpen menu makan.",
    ),
    (
        "Den, kalau alien mendarat di pasar, bakal kaget lihat apa?",
        "Harga pertama sama harga setelah ditawar beda jauh kali 😂",
        "Belajar sopan nawar dulu, bang. Jangan turun dari piring terbang langsung minta bonus.",
    ),
    (
        "Aku rasa antre itu ujian sabar yang paling sering datang.",
        "Apalagi kalau antrean sebelah maju terus. Rasanya kayak diprank kehidupan 😂",
        "Lihat yang benar-benar dibutuhkan, bang. Bukan semua barisan harus kau ikutin.",
    ),
    (
        "Kalau robot disuruh jaga warung, bisa hafal utang tetangga ga?",
        "Bisa, Dit. Tapi pas tagihannya ditanya, tetangganya langsung mode pesawat 😂",
        "Catatan yang rapi membantu. Tetap tagih baik-baik, kawan.",
    ),
    (
        "Bah, kenapa ya kalimat terserah sering bikin orang mikir lama?",
        "Karena jawabannya bebas, tapi belum tentu lolos penilaian, Dit 😂",
        "Kalau punya pilihan, bilang aja lah. Orang ga punya kunci jawaban pikiran kau.",
    ),
    (
        "Den, kalau kita buka warung di Mars, nama menunya apa?",
        "Nasi padang antariksa. Tapi ongkirnya bikin pensiun sebelum makan 😂",
        "Cari pembelinya dulu, bang. Jangan warungnya buka, planetnya masih sepi.",
    ),
    (
        "Katanya langkah kecil yang konsisten lebih berguna daripada niat besar.",
        "Kalau langkah kecil ke dapur tiap malam, itu konsisten juga ga, Dit? 😂",
        "Konsisten boleh, tapi lihat arahnya juga lah, kawan.",
    ),
    (
        "Den, kenapa orang suka bilang cuma lihat-lihat di toko?",
        "Biar keranjang belanjanya ga merasa dituntut, Dit 😂",
        "Bikin daftar dulu lah, bang. Dompet ga selalu sekuat rasa penasaran kau.",
    ),
    (
        "Bah, kalau GPS bisa ngomel, perjalanan bakal gimana?",
        "Dia bilang: tadi udah kubilang belok, sekarang kau putar balik sana 😂",
        "GPS aja ngasih arah. Yang nyetir tetap mesti lihat jalan, kawan.",
    ),
    (
        "Den, kalau lemari es bisa menyimpan kenangan, apa yang kau masukin?",
        "Harga jajanan dulu, Dit. Biar ga cepat cair pas lihat harga sekarang 😂",
        "Nostalgia boleh. Belanja hari ini tetap pakai hitungan hari ini, bang.",
    ),
    (
        "Aku suka penjelasan yang gampang dipahami, ga harus panjang.",
        "Sama, Dit. Kecuali penjelasan alasan aku telat, itu suka kepanjangan sendiri 😂",
        "Jujur aja lah, kawan. Kadang alasan simpel lebih bisa dipercaya.",
    ),
    (
        "Kalau burung bisa review manusia, apa penilaiannya ya?",
        "Bisa terbang pakai mesin, tapi parkirnya tetap ribet, Dit 😂",
        "Yang penting belajar beresin urusan sendiri dulu, bang.",
    ),
    (
        "Bah, kadang rencana paling rapi kalah sama ajakan makan.",
        "Soalnya perut ga ngerti kalender, Dit 😂",
        "Makan dulu boleh. Habis itu balik kerjain yang tadi, kawan.",
    ),
    (
        "Den, kalau ada lomba cari barang yang dipegang sendiri, kita menang ga?",
        "Aku juara, Dit. Pernah cari HP sambil nyalain senter dari HP itu 😂",
        "Itu tandanya perlu jeda sebentar lah, bang. Otaknya kebanyakan tab terbuka.",
    ),
    (
        "Katanya ngobrol enak itu sama-sama didengerin.",
        "Betul, Dit. Jangan aku baru mulai cerita, kau udah buka bab cerita kau 😂",
        "Kasih giliran lah, kawan. Obrolan bukan balapan siapa paling banyak bunyi.",
    ),
)


def vip_text(*texts):
    return any(VIP_PATTERN.search(text or "") for text in texts)


class RapikanMcb:
    def __init__(self, controller, *, clock=time.time):
        self.c = controller
        self.store = controller.vip_store
        self.clock = clock
        self.bot = None
        self.friends = None
        self.task = None
        self.wake = asyncio.Event()

        with self.store.lock, self.store.connection:
            self.store.connection.executescript("""
                CREATE TABLE IF NOT EXISTS mcm_rapi_meta (
                    bot_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    installed_at INTEGER NOT NULL,
                    PRIMARY KEY(bot_id, owner_id)
                );

                CREATE TABLE IF NOT EXISTS mcm_reminders (
                    bot_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    order_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    due_at INTEGER NOT NULL,
                    attempted_at INTEGER,
                    delivered_at INTEGER,
                    error_type TEXT,
                    PRIMARY KEY(bot_id, owner_id, order_id, number)
                );

                CREATE TABLE IF NOT EXISTS mcm_cleanup (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    sender_bot INTEGER NOT NULL,
                    due_at INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    finished INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(chat_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS mcm_dialog_progress (
                    group_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    PRIMARY KEY(group_id, day)
                );
            """)

        self.install_handlers()

    def install_handlers(self):
        original_menu = self.c.vip_menu
        original_send = self.c.send_group_text

        async def private_menu(update, context):
            if update.effective_chat.type != "private":
                return

            rows = [
                [
                    InlineKeyboardButton(
                        f"{plan.name} — Rp{plan.amount:,}".replace(",", "."),
                        callback_data="vip:buy:" + key,
                    )
                ]
                for key, plan in PLANS.items()
            ]

            await update.effective_message.reply_text(
                "Mau paket yang mana, pak/ibu bos? 😄\n\n"
                + "\n".join(
                    f"{plan.name}: Rp{plan.amount:,}".replace(",", ".")
                    for plan in PLANS.values()
                )
                + "\n\nAkses permanen, bayar sekali. "
                "TAKE ALL masuk ke satu grup gabungan.\n"
                "Bayar QRIS, kirim bukti dan nama pengirim, "
                "lalu tunggu owner cek uang masuk.\n"
                "Pakai /akses untuk mengambil akses yang sudah lunas. "
                "/batal untuk kembali ngobrol.",
                reply_markup=InlineKeyboardMarkup(rows),
                parse_mode=None,
            )

        async def menu(update, context):
            message = update.effective_message

            if update.effective_chat.id == self.c.settings.target_group_id:
                sent = await message.reply_text(
                    "Pak/ibu bos, buka chat pribadi Emak "
                    "lalu ketik /vip ya 😄",
                    parse_mode=None,
                )
                self.expire(
                    context.bot, message.chat.id, message.message_id, 60
                )
                self.expire(
                    context.bot, message.chat.id, sent.message_id, 60
                )
                return

            return await original_menu(update, context)

        async def send(message, text, **kwargs):
            result = await original_send(message, text, **kwargs)
            quote = getattr(message, "reply_to_message", None)

            if (
                message.chat.id == self.c.settings.target_group_id
                and vip_text(
                    text,
                    message.text or message.caption,
                    (quote.text or quote.caption) if quote else "",
                )
            ):
                bot = self.bot or self.c.vip.bot
                self.expire(
                    bot, message.chat.id, message.message_id, 60
                )
                if result is not None:
                    self.expire(
                        bot, message.chat.id, result.message_id, 60
                    )

            return result

        self.c.vip.start = private_menu
        self.c.vip_menu = menu
        self.c.send_group_text = send

    def expire(self, bot, chat_id, message_id, seconds):
        if bot is None or chat_id != self.c.settings.target_group_id:
            return

        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "INSERT INTO mcm_cleanup "
                "(chat_id,message_id,sender_bot,due_at) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(chat_id,message_id) DO UPDATE "
                "SET due_at=MIN(due_at,excluded.due_at)",
                (
                    chat_id,
                    message_id,
                    bot.id,
                    int(self.clock() + seconds),
                ),
            )

        self.wake.set()

    async def observe(self, update, context):
        m = update.effective_message

        if (
            m is None
            or m.chat.id != self.c.settings.target_group_id
            or not m.text
        ):
            return

        quote = m.reply_to_message

        if vip_text(
            m.text,
            (quote.text or quote.caption) if quote else "",
        ):
            self.expire(
                context.bot, m.chat.id, m.message_id, 60
            )

    async def start(self, application, friends):
        self.bot = application.bot
        self.friends = friends

        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "INSERT OR IGNORE INTO mcm_rapi_meta VALUES(?,?,?)",
                (
                    self.bot.id,
                    self.c.settings.owner_id,
                    int(self.clock()),
                ),
            )

        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def enroll_reminders(self):
        owner = self.c.settings.owner_id
        bot_id = self.bot.id

        with self.store.lock, self.store.connection:
            installed = self.store.connection.execute(
                "SELECT installed_at FROM mcm_rapi_meta "
                "WHERE bot_id=? AND owner_id=?",
                (bot_id, owner),
            ).fetchone()[0]

            for number, delay in ((1, 300), (2, 3600)):
                self.store.connection.execute(
                    "INSERT OR IGNORE INTO mcm_reminders "
                    "(bot_id,owner_id,order_id,number,due_at) "
                    "SELECT ?,?,o.order_id,?,m.approved_at+? "
                    "FROM orders o "
                    "JOIN manual_orders m USING(order_id) "
                    "WHERE o.state='paid' "
                    "AND m.owner_id=? "
                    "AND m.approved_by=? "
                    "AND m.approved_at>=? "
                    "AND EXISTS("
                    "SELECT 1 FROM manual_reviews v "
                    "WHERE v.order_id=o.order_id "
                    "AND v.owner_id=? AND v.bot_id=?)",
                    (
                        bot_id,
                        owner,
                        number,
                        delay,
                        owner,
                        owner,
                        installed,
                        owner,
                        bot_id,
                    ),
                )

    async def process(self):
        self.enroll_reminders()
        now = int(self.clock())

        with self.store.lock:
            reminders = self.store.connection.execute(
                "SELECT r.*,o.user_id,m.plan_id "
                "FROM mcm_reminders r "
                "JOIN orders o USING(order_id) "
                "JOIN manual_orders m USING(order_id) "
                "WHERE r.bot_id=? AND r.owner_id=? "
                "AND r.attempted_at IS NULL "
                "AND r.due_at<=? AND o.state='paid' "
                "ORDER BY r.due_at LIMIT 20",
                (self.bot.id, self.c.settings.owner_id, now),
            ).fetchall()

        for row in reminders:
            key = (
                row["bot_id"],
                row["owner_id"],
                row["order_id"],
                row["number"],
            )

            with self.store.lock, self.store.connection:
                claimed = self.store.connection.execute(
                    "UPDATE mcm_reminders SET attempted_at=? "
                    "WHERE bot_id=? AND owner_id=? AND order_id=? "
                    "AND number=? AND attempted_at IS NULL "
                    "AND due_at<=?",
                    (now, *key, now),
                ).rowcount

                if claimed and row["number"] == 1:
                    self.store.connection.execute(
                        "UPDATE mcm_reminders "
                        "SET due_at=MAX(due_at,?) "
                        "WHERE bot_id=? AND owner_id=? AND order_id=? "
                        "AND number=2 AND attempted_at IS NULL",
                        (
                            now + 3300,
                            row["bot_id"],
                            row["owner_id"],
                            row["order_id"],
                        ),
                    )

            if not claimed:
                continue

            plan = PLANS.get(row["plan_id"])
            name = plan.name if plan else "VIP"

            action = (
                "Gas buka websitenya, pak bos."
                if plan and plan.kind == "website"
                else "Gas join grupnya, pak bos."
            )

            try:
                await self.bot.send_message(
                    chat_id=row["user_id"],
                    parse_mode=None,
                    text=(
                        "Hallo pak bos 👋\n"
                        "Paket VIP yang diorder sudah di-ACC.\n"
                        f"Paket: {name}\n\n"
                        + action
                        + " Ketik /akses di chat ini "
                        "untuk mengambil link aksesnya."
                    ),
                )

                with self.store.lock, self.store.connection:
                    self.store.connection.execute(
                        "UPDATE mcm_reminders SET delivered_at=? "
                        "WHERE bot_id=? AND owner_id=? "
                        "AND order_id=? AND number=?",
                        (int(self.clock()), *key),
                    )

            except TelegramError as error:
                with self.store.lock, self.store.connection:
                    self.store.connection.execute(
                        "UPDATE mcm_reminders SET error_type=? "
                        "WHERE bot_id=? AND owner_id=? "
                        "AND order_id=? AND number=?",
                        (type(error).__name__, *key),
                    )

                logger.warning(
                    "Pengingat VIP tertunda: nomor=%s; jenis=%s",
                    row["number"],
                    type(error).__name__,
                )

        await self.cleanup(now)

    async def cleanup(self, now):
        with self.store.lock:
            rows = self.store.connection.execute(
                "SELECT * FROM mcm_cleanup "
                "WHERE chat_id=? AND finished=0 AND due_at<=? "
                "ORDER BY due_at LIMIT 50",
                (self.c.settings.target_group_id, now),
            ).fetchall()

        clients = {self.bot.id: self.bot}
        clients.update({
            bot.id: bot
            for bot in getattr(self.friends, "bots", ())
        })

        for row in rows:
            bot = clients.get(row["sender_bot"], self.bot)
            finished = True
            delay = 30

            try:
                await bot.delete_message(
                    chat_id=row["chat_id"],
                    message_id=row["message_id"],
                )

            except BadRequest as error:
                if "message to delete not found" not in str(error).lower():
                    logger.warning(
                        "Hapus chat belum berhasil: jenis=BadRequest. "
                        "Periksa izin hapus bot."
                    )

            except RetryAfter as error:
                wait = error.retry_after
                seconds = (
                    wait.total_seconds()
                    if hasattr(wait, "total_seconds")
                    else wait
                )
                delay = max(30, int(seconds) + 1)
                finished = False

            except TelegramError as error:
                finished = row["attempts"] >= 4
                logger.warning(
                    "Hapus chat ditunda: jenis=%s",
                    type(error).__name__,
                )

            with self.store.lock, self.store.connection:
                self.store.connection.execute(
                    "UPDATE mcm_cleanup "
                    "SET finished=?,attempts=attempts+1,due_at=? "
                    "WHERE chat_id=? AND message_id=?",
                    (
                        int(finished),
                        now + delay,
                        row["chat_id"],
                        row["message_id"],
                    ),
                )

    async def run(self):
        while True:
            self.wake.clear()

            try:
                await self.process()
            except (sqlite3.Error, TelegramError) as error:
                logger.warning(
                    "Pemeliharaan MCB ditunda: jenis=%s",
                    type(error).__name__,
                )

            try:
                await asyncio.wait_for(
                    self.wake.wait(), timeout=10
                )
            except TimeoutError:
                pass


class TemanTerawat(TemanNgobrol):
    def __init__(self, controller, maintenance):
        super().__init__(controller)
        self.maintenance = maintenance
        self.contacts = {}

    def local_answer(self, index, text):
        if NO_NEED.fullmatch(text):
            return (
                "Ok baiklah, abangku."
                if index == 0
                else "Oke, bang. Santai dulu lah 😄"
            )

        rest = (
            ALIASES[index].sub("", text).strip(" ,!.?")
            if index < 2 else text
        )

        if not rest or re.fullmatch(
            r"(?:woi|woy|oi|oy|eh|hei|hey|bang|bro)[\s!,.?]*",
            rest,
            re.I,
        ):
            pools = (
                (
                    "Hadir, abangku.",
                    "Iya, aku denger, bang.",
                    "Ada aku di sini, kawan.",
                    "Siap, abangku 😄",
                ),
                (
                    "Hadir, bang. Jangan langsung disuruh bayar parkir lah 😂",
                    "Nyaut aku, kawan. Bukan pajangan grup ini 😂",
                    "Ada aku di sini, bah 😄",
                ),
                ("Emak denger, abangku.",),
            )
            return self.pick_local(index, pools[index])

        return super().local_answer(index, text)

    async def observe(self, update, context):
        m = update.effective_message

        if (
            m is not None
            and m.chat.id == self.group
            and update.edited_message is None
            and m.from_user is not None
            and not m.from_user.is_bot
            and m.sender_chat is None
            and NO_NEED.fullmatch(m.text or "")
            and self.allowed()
        ):
            key = (m.chat.id, m.message_id)
            if key in self.seen:
                raise ApplicationHandlerStop

            contact = self.contacts.get(m.from_user.id)
            quoted = getattr(
                m.reply_to_message, "from_user", None
            )

            index = next(
                (
                    i for i, bot in enumerate(self.bots)
                    if quoted and bot.id == quoted.id
                ),
                None,
            )

            if index is None:
                index = next(
                    (
                        i for i, pattern in enumerate(ALIASES)
                        if pattern.search(m.text)
                    ),
                    None,
                )

            if (
                index is None
                and contact
                and self.clock() - contact[1] <= 120
            ):
                index = contact[0]

            if index is not None:
                self.seen.add(key)
                self.last_activity = self.clock()
                self.finish()

                await self.send_text(
                    index,
                    self.local_answer(index, m.text),
                    message=m,
                )

                self.contacts.pop(m.from_user.id, None)
                raise ApplicationHandlerStop

        return await super().observe(update, context)

    def prepare_local(self):
        if (
            self.turn % 3 == 0
            or not hasattr(self, "daily_pack")
        ):
            day = datetime.fromtimestamp(
                self.maintenance.clock(),
                timezone(timedelta(hours=7)),
            ).date().isoformat()

            store = self.maintenance.store

            with store.lock, store.connection:
                row = store.connection.execute(
                    "SELECT position FROM mcm_dialog_progress "
                    "WHERE group_id=? AND day=?",
                    (self.group, day),
                ).fetchone()

                position = row[0] if row else 0

                store.connection.execute(
                    "INSERT INTO mcm_dialog_progress VALUES(?,?,?) "
                    "ON CONFLICT(group_id,day) "
                    "DO UPDATE SET position=excluded.position",
                    (self.group, day, position + 1),
                )

            order = list(range(len(DAILY_DIALOGS)))
            random.Random(
                f"{day}:{self.group}"
            ).shuffle(order)

            self.daily_pack = DAILY_DIALOGS[
                order[position % len(order)]
            ]

        self.prepared.extend(
            (
                (self.turn + offset) % 3,
                self.daily_pack[(self.turn + offset) % 3],
            )
            for offset in range(
                min(
                    3 - self.turn % 3,
                    self.max_turns - self.turn,
                )
            )
        )

    async def send_text(
        self,
        index,
        text,
        *,
        message=None,
        revision=None,
    ):
        c = self.controller

        def regional_word(match):
            original = match.group()
            replacement = {
                "gue": "aku",
                "lu": "kau",
            }[original.lower()]

            return (
                replacement.capitalize()
                if original[0].isupper()
                else replacement
            )

        text = re.sub(
            r"\b(gue|lu)\b",
            regional_word,
            text,
            flags=re.I,
        )
        text = re.sub(
            r"\ble\b",
            "abangku" if index == 0 else "bang",
            text,
        )

        if not vip_text(text):
            text = " ".join(
                re.split(r"(?<=[.!?])\s+", text)[:2]
            )

        async with c.outgoing_locks.setdefault(
            self.group, asyncio.Lock()
        ):
            await c.wait_outgoing(self.group)

            if (
                not self.allowed()
                or (
                    revision is not None
                    and revision != self.revision
                )
                or (
                    message is not None
                    and (self.group, message.message_id)
                    in c.blocked_messages
                )
            ):
                return False

            bot = (
                self.bots[index]
                if index < 2
                else self.application.bot
            )

            try:
                sent = await bot.send_message(
                    chat_id=self.group,
                    text=text[:350],
                    parse_mode=None,
                    reply_parameters=(
                        ReplyParameters(
                            message_id=message.message_id,
                            allow_sending_without_reply=True,
                        )
                        if message is not None else None
                    ),
                )

                self.send_errors.pop(index, None)

                seconds = (
                    60
                    if vip_text(
                        text,
                        message.text if message is not None else "",
                    )
                    else 300
                )

                self.maintenance.expire(
                    bot,
                    self.group,
                    sent.message_id,
                    seconds,
                )

                if message is not None:
                    if len(self.contacts) >= 5000:
                        self.contacts.clear()

                    self.contacts[message.from_user.id] = (
                        index, self.clock()
                    )

                    if seconds == 60:
                        self.maintenance.expire(
                            self.application.bot,
                            self.group,
                            message.message_id,
                            60,
                        )

                return True

            except TelegramError as error:
                self.send_errors[index] = type(error).__name__
                logger.warning(
                    "Kiriman %s gagal: jenis=%s",
                    NAMES[index],
                    type(error).__name__,
                )
                return False

            finally:
                c.record_wait(self.group, 3.5)
