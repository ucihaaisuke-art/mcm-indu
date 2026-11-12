"""Pembaruan obrolan MCB dan karakter AI Ganjar."""

import asyncio
import logging
import os
import re
import sqlite3
import time
from collections import deque
from contextlib import nullcontext

from telegram import Bot, ReplyParameters
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop
from telegram.request import HTTPXRequest

if __package__:
    from . import jadwal_mcb, rapikan_mcb
    from .bot_mcm import declines_bot_reply
    from .teman_ngobrol import TemanNgobrol
else:
    import jadwal_mcb
    import rapikan_mcb
    from bot_mcm import declines_bot_reply
    from teman_ngobrol import TemanNgobrol

log = logging.getLogger(__name__)

CALL = re.compile(
    r"(?<!\w)(?:pak\s+ganjar|ganjar|njar|jar)(?!\w)", re.I
)
SERVICE = re.compile(
    r"\b(?:vip|premium|paket|qris|bayar|pembayaran|pesanan|harga|akses)\b|/vip",
    re.I,
)

GREETINGS = (
    (
        "Ada apa le? Aku nyimak.",
        "Iya le, ada cerita apa?",
        "Dit hadir. Langsung aja, le.",
        "Bah, dipanggil juga akhirnya.",
    ),
    (
        "Hadir bang, belum sempat pura-pura sibuk.",
        "Kenapa bang? Aku dengar.",
        "Nis di sini. Ada perkara apa pula?",
        "Iya bang, jangan dibuat absen dua kali.",
    ),
    (
        "Iya, Mak di sini.",
        "Ada apa, le? Ceritakan dulu.",
        "Mak dengar, langsung aja.",
    ),
)

CHAT = (
    (
        "Bah, ada lanjutannya rupanya. Terus gimana, le?",
        "Yang ini aku nyimak dulu. Tadi mulainya dari mana?",
        "Kau ceritakan detailnya, jangan cuma kasih judul.",
    ),
    (
        "Sebentar bang, ini perlu kupahami sebelum sok paham.",
        "Lanjut bang. Jangan berhenti pas aku baru penasaran.",
        "Aku ikut nyimak. Kali ini komentarku kutahan dulu.",
    ),
    (
        "Mak simak dulu, le. Ceritakan lanjutannya.",
        "Boleh dibahas. Bagian mana yang mau kau tanyakan?",
        "Iya, lanjutkan. Biar jelas duduk perkaranya.",
    ),
)

UNKNOWN = (
    (
        "Jangan tanya yang itu  le. pening aku nengoknya.",
        "Bagian itu belum bisa kupastikan. Kau maksud yang mana?",
    ),
    (
        "Aku belum tahu pasti, bang. Jangan sampai percaya diriku mendahului datanya.",
        "Bantu jelaskan sedikit lagi, bang. Aku nggak mau asal sambung.",
    ),
    (
        "Mak belum bisa memastikan. Jelaskan bagian yang dimaksud, le.",
        "Konteksnya belum cukup, le. Ceritakan detail yang relevan dulu.",
    ),
)

STYLES = (
    "Adit: spontan, tajam mengamati hal kecil; pakai aku/kau/le, "
    "logat Medan secukupnya. Kadang ikut konyol, bukan selalu "
    "pembuka pertanyaan atau pemberi nasihat.",
    "Denis: suka membela logika konyolnya dengan percaya diri, "
    "lalu bisa menertawakan diri. Pakai aku/kau/bang; jangan selalu "
    "menjadikan miskin, saldo atau utang sebagai punchline.",
    "Emak: hangat, tangkas membalas ledekan, boleh sama konyolnya; "
    "jangan selalu menasihati.",
)

POLICY = (
    "\nAturan obrolan terbaru: " + " ".join(STYLES)
    + " Sambungkan candaan ke pesan nyata terakhir, bukan setiap balasan "
    "menjadi lawakan. Variasikan kosakata dan irama; jangan mengulang "
    "pembuka, punchline, atau analogi riwayat. Jangan menyisipkan promosi "
    "VIP MCB, ajakan beli/join, harga, QRIS, atau perintah /vip dalam "
    "obrolan biasa maupun dialog otomatis. Informasi layanan hanya bila "
    "anggota memang menanyakannya; jawab seperlunya tanpa upselling. "
    "Tetap jawab pertanyaan serius secara serius. Pak Ganjar di riwayat "
    "adalah karakter AI parodi, bukan pejabat asli."
)

PARODY = (
    "Kamu Pak Ganjar, karakter bot AI parodi fiktif komunitas, bukan "
    "Ganjar Pranowo asli dan bukan akun resmi. Jangan mengaku mewakili "
    "beliau atau mengarang kehidupan pribadi dan preferensi seksual "
    "beliau. Tegas, cerdas, profesional tetapi jahil ringan; gunakan "
    "wong/lha/ndan sewajarnya, ledek situasinya bukan merendahkan orang. "
    "Politik dibahas netral berdasarkan pengetahuan yang pasti, jangan "
    "mengarang berita terkini atau mengajak memilih calon. Boleh "
    "membahas hiburan secara non-eksplisit bila ditanya. Pertanyaan "
    "bahan baru ke Mak sudah diatur timer enam jam; jangan menginisiasi "
    "pertanyaan itu di jawaban ini. Jangan mengaku sudah menonton atau "
    "memiliki video, membuat adegan seksual atau ajakan membeli. "
    "Jawab sendiri, 1–2 kalimat maksimal 300 karakter, tanpa nama "
    "pembicara, URL atau mention."
)

DIALOGS = (
    (
        "Den, kau kalau bilang OTW itu sudah keluar rumah belum?",
        "Sudah keluar dari niat rebahan, Dit. Itu juga perjalanan.",
        "Jauh kali perjalananmu. Pintu rumah aja belum sampai.",
    ),
    (
        "Aku buka kulkas tiga kali, isinya tetap sama.",
        "Coba empat kali, Dit. Mana tahu pembaruannya bertahap.",
        "Yang bertambah cuma tagihan kalau pintunya dibuka terus.",
    ),
    (
        "Den, notifikasi grup kau sunyikan semua?",
        "Iya, tapi kubuka tiap dua menit. Biar aku yang cari masalahnya.",
        "Bagus, sekarang notifikasinya punya petugas jemput.",
    ),
    (
        "Kau tadi ngetik panjang kali, Nis. Jadi dikirim apa?",
        "Wkwk doang. Sisanya lewat pertimbangan redaksi.",
        "Redaksimu ketat juga. Pembacanya sudah telanjur penasaran.",
    ),
    (
        "Aku screenshot resep biar nanti dicoba.",
        "Sama, Dit. Galeriku sudah bisa buka rumah makan.",
        "Kompornya dipakai juga, jangan cuma memorinya yang panas.",
    ),
    (
        "Den, siapa yang mindahin remot ke dapur?",
        "Aku cuma memastikan hiburan dekat sumber energi.",
        "Baterainya butuh listrik, Nis, bukan sambal.",
    ),
    (
        "Baru kuberesin meja, kok barangnya balik lagi?",
        "Mereka nyaman sama kau, Dit. Anggap aja reuni.",
        "Kalau reuni, yang punya barang ikut jemput pulang.",
    ),
    (
        "Nis, kau bilang filmnya bagus tapi ketiduran?",
        "Musiknya menenangkan, akting bantal juga kuat.",
        "Ulasanmu cocoknya buat kasur, bukan filmnya.",
    ),
)

TOPICS = tuple(pack[0] for pack in DIALOGS)


def calling(text, username=""):
    return bool(
        CALL.search(text)
        or (
            username
            and re.search(
                r"(?<!\w)@" + re.escape(username) + r"\b", text, re.I
            )
        )
    )


def body(text, username=""):
    if username:
        text = re.sub(
            r"@" + re.escape(username) + r"\b", "", text, flags=re.I
        )
    text = CALL.sub("", text)
    return re.sub(
        r"\b(?:woi|woy|halo|hai|hey|eh|bang|bro|le|pak)\b",
        "",
        text,
        flags=re.I,
    ).strip(" ,.!?:\n")


def prefix(text):
    text = re.sub(
        r"^\s*Hadir\s+ndan[,.!\s]*", "", text, flags=re.I
    ).strip()
    return "Hadir ndan" + (". " + text[:335] if text else "")


class GanjarParodi:
    INTERVAL = 6 * 60 * 60
    ACTIVE_WINDOW = 20 * 60

    def __init__(self, c, friends, maintenance, schedule):
        self.c = c
        self.f = friends
        self.m = maintenance
        self.s = schedule
        self.bot = None
        self.task = None
        self.last_human = None
        self.pending = 0
        self.member_lock = asyncio.Lock()
        self.interjection = None
        self.last_session = None
        self.state = "belum diaktifkan; isi MCM_GANJAR_BOT_TOKEN"

        with self.m.store.lock, self.m.store.connection:
            self.m.store.connection.execute(
                "CREATE TABLE IF NOT EXISTS mcm_ganjar_due ("
                "main_bot INTEGER, owner_id INTEGER, group_id INTEGER, "
                "actor_id INTEGER, next_due REAL NOT NULL, "
                "PRIMARY KEY(main_bot,owner_id,group_id,actor_id))"
            )

    def allowed(self):
        return (
            self.bot is not None
            and self.f.enabled
            and not self.s.night()
            and self.c.chatter_overrides.get(
                self.f.group, self.c.settings.chatter_enabled
            )
        )

    async def start(self):
        token = os.getenv("MCM_GANJAR_BOT_TOKEN", "").strip()
        if token and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

        bot, self.bot = self.bot, None
        if bot is not None:
            try:
                await bot.shutdown()
            except TelegramError:
                log.warning("Penutupan koneksi parodi tertunda.")

    async def connect(self):
        token = os.getenv("MCM_GANJAR_BOT_TOKEN", "").strip()
        if token in [self.f.application.bot.token, *self.f.tokens]:
            self.state = "token harus berbeda dari Emak, Adit dan Denis"
            return

        bot = Bot(
            token,
            request=HTTPXRequest(
                connection_pool_size=4,
                read_timeout=30,
            ),
        )

        try:
            await bot.initialize()
            member = await self.f.application.bot.get_chat_member(
                self.f.group, bot.id
            )

            if member.status not in ("member", "administrator", "creator"):
                raise ValueError(
                    "Bot belum menjadi anggota yang dapat mengirim pesan"
                )

            try:
                await bot.set_my_short_description(
                    "Bot AI parodi fiktif. Bukan Ganjar Pranowo asli "
                    "atau akun resmi."
                )
            except TelegramError:
                log.warning(
                    "Bio karakter AI belum diperbarui; "
                    "atur bio melalui BotFather."
                )

            self.key = (
                self.f.application.bot.id,
                self.c.settings.owner_id,
                self.f.group,
                bot.id,
            )

            with self.m.store.lock, self.m.store.connection:
                self.m.store.connection.execute(
                    "INSERT OR IGNORE INTO mcm_ganjar_due VALUES(?,?,?,?,?)",
                    (*self.key, time.time() + self.INTERVAL),
                )

            self.bot = bot
            self.state = (
                "tersambung; panggilan aktif, pertanyaan Mak maksimal "
                "sekali tiap 6 jam saat grup aktif"
            )
        except BaseException:
            await bot.shutdown()
            raise

    def claim_question(self, now):
        with self.m.store.lock, self.m.store.connection:
            result = self.m.store.connection.execute(
                "UPDATE mcm_ganjar_due SET next_due=? "
                "WHERE main_bot=? AND owner_id=? AND group_id=? "
                "AND actor_id=? AND next_due<=?",
                (now + self.INTERVAL, *self.key, now),
            )
            return result.rowcount == 1

    def remember(self, speaker, text):
        entry = {"speaker": speaker, "text": text}
        self.f.history.append(entry)
        self.c.history.setdefault(
            self.f.group, deque(maxlen=8)
        ).append(entry)

    async def send(self, text, message=None, *, revision=None):
        lock = self.c.outgoing_locks.setdefault(
            self.f.group, asyncio.Lock()
        )

        async with lock:
            await self.c.wait_outgoing(self.f.group)

            if not self.allowed():
                return False
            if revision is not None and revision != self.f.revision:
                return False
            if (
                message is not None
                and (self.f.group, message.message_id)
                in self.c.blocked_messages
            ):
                return False

            try:
                reply = (
                    ReplyParameters(
                        message_id=message.message_id,
                        allow_sending_without_reply=True,
                    )
                    if message is not None
                    else None
                )

                sent = await self.bot.send_message(
                    chat_id=self.f.group,
                    text=text[:350],
                    parse_mode=None,
                    reply_parameters=reply,
                )

                outgoing = (
                    self.m.outgoing(message)
                    if hasattr(self.m, "outgoing")
                    else nullcontext()
                )
                with outgoing:
                    self.m.expire(
                        self.bot, self.f.group, sent.message_id, 86400
                    )

                self.remember("Pak Ganjar", text[:350])
                self.c.last_reply[self.f.group] = self.f.clock()
                return True

            except TelegramError as error:
                log.warning(
                    "Kiriman parodi belum terkonfirmasi: %s; "
                    "tanpa kirim ulang.",
                    type(error).__name__,
                )
                return False
            finally:
                self.c.record_wait(self.f.group, 3.5)

    async def answer(self, history, prompt, *, member):
        ai = self.c.ai
        if ai is None:
            return None

        async with asyncio.timeout(self.c.settings.reply_timeout):
            if await ai.reserve_call(member, self.allowed):
                answer = await TemanNgobrol.generate(
                    self.f, history, PARODY + prompt
                )
                if self.f.valid_text(answer):
                    return answer.strip()

        return None

    async def observe(self, update, context, original):
        m = update.effective_message

        if (
            m is None
            or update.edited_message is not None
            or m.chat.id != self.f.group
            or m.from_user is None
            or m.from_user.is_bot
            or m.sender_chat is not None
        ):
            return await original(update, context)

        text = m.text or m.caption or ""

        if (
            not text
            or text.startswith("/")
            or declines_bot_reply(text)
            or (m.chat.id, m.message_id) in self.c.blocked_messages
            or self.c.forbidden_request(m)
        ):
            return await original(update, context)

        self.last_human = self.f.clock()
        quoted = getattr(m.reply_to_message, "from_user", None)

        if not self.allowed():
            return await original(update, context)

        addressed = calling(text, self.bot.username) or (
            quoted and quoted.id == self.bot.id
        )
        if not addressed:
            return await original(update, context)

        key = (m.chat.id, m.message_id)
        if key in self.f.seen:
            raise ApplicationHandlerStop

        if len(self.f.seen) >= 10000:
            self.f.seen.clear()
        self.f.seen.add(key)

        self.f.last_activity = self.f.clock()
        self.f.revision += 1
        self.f.prepared.clear()
        self.interjection = None
        self.c.cancel_unanswered(self.f.group)

        if self.pending >= 4:
            await self.send(
                prefix(
                    "Antre sebentar, pertanyaan sebelumnya masih kujawab."
                ),
                m,
            )
            raise ApplicationHandlerStop

        self.pending += 1
        self.f.member_pending += 1
        self.c.ai_pending[self.f.group] = (
            self.c.ai_pending.get(self.f.group, 0) + 1
        )

        try:
            async with self.member_lock:
                self.remember("member", text[:1000])

                if not body(text, self.bot.username):
                    answer = ""

                elif re.search(
                    r"\b(?:asli|resmi|parodi|identitas|"
                    r"(?:kamu|kau|ini) siapa|siapa (?:kamu|kau))\b",
                    text,
                    re.I,
                ):
                    answer = (
                        "Aku bot AI parodi fiktif, bukan Ganjar Pranowo "
                        "asli atau akun resmi."
                    )

                else:
                    history = list(self.c.history[self.f.group])

                    if m.reply_to_message:
                        quote = (
                            m.reply_to_message.text
                            or m.reply_to_message.caption
                            or ""
                        )
                        history[-1] = {
                            **history[-1],
                            "reply_to": quote[:500],
                        }

                    try:
                        answer = await self.answer(
                            history,
                            " Jawab pesan anggota terakhir.",
                            member=True,
                        )
                    except TimeoutError:
                        answer = None

                    if answer is None:
                        answer = (
                            "Jawabannya belum siap, ndan. "
                            "Aku nggak mau asal nebak."
                        )

                await self.send(prefix(answer), m)
                self.f.next_turn_at = (
                    self.f.clock() + self.f.gap_seconds
                )

        finally:
            self.pending -= 1
            self.f.member_pending -= 1
            pending = self.c.ai_pending.get(self.f.group, 1) - 1

            if pending:
                self.c.ai_pending[self.f.group] = pending
            else:
                self.c.ai_pending.pop(self.f.group, None)

        raise ApplicationHandlerStop

    async def tick(self):
        if (
            not self.allowed()
            or self.last_human is None
            or self.pending
            or self.f.member_pending
        ):
            return

        if (
            self.c.ai_pending.get(self.f.group)
            or self.c.queued_replies.get(self.f.group)
        ):
            return

        async with self.f.lock:
            now = self.f.clock()

            if (
                now - self.last_human < self.ACTIVE_WINDOW
                and self.claim_question(time.time())
            ):
                await self.send(
                    "Mak, ada bahan baru hari ini? Wong penasaran "
                    "boleh, antre tetap tertib, ndan."
                )
                self.f.prepared.clear()
                self.f.revision += 1
                self.f.next_turn_at = now + self.f.gap_seconds
                return

            due = self.interjection
            if not due or now < due[1]:
                return

            self.interjection = None
            if not self.f.active or due[0] != self.f.started_at:
                return

            revision = self.f.revision

            try:
                answer = await self.answer(
                    list(self.f.history),
                    " Sambung obrolan Adit, Denis dan Mak secara singkat; "
                    "jangan membuka topik baru.",
                    member=False,
                )
            except TimeoutError:
                answer = None

            if answer and await self.send(answer, revision=revision):
                self.f.prepared.clear()
                self.f.revision += 1
                self.f.next_turn_at = (
                    self.f.clock() + self.f.gap_seconds
                )

    async def run(self):
        while True:
            try:
                if self.bot is None:
                    await self.connect()
                else:
                    await self.tick()

            except (
                TelegramError,
                ValueError,
                TimeoutError,
                OSError,
                sqlite3.Error,
            ) as error:
                self.state = "tertunda: " + type(error).__name__
                log.warning(
                    "Teman parodi tertunda: %s",
                    type(error).__name__,
                )

            await asyncio.sleep(20)


def pasang_obrolan(c, f, maintenance, schedule):
    if hasattr(f, "ganjar"):
        return f.ganjar

    info = (
        "Jaga obrolan tetap nyaman. Hindari spam dan data pribadi; "
        "laporkan masalah kepada admin.",
        "Kalau ada pertanyaan, sertakan konteksnya biar mudah dibantu. "
        "Jangan bagikan OTP atau kata sandi di grup.",
        "Bercanda boleh, saling menghargai tetap jalan. Mak dan "
        "kawan-kawan bisa dipanggil untuk ikut ngobrol.",
    )

    banter = {
        "cari_denis": (
            (
                "Nis, kau dicari. Jangan sembunyi di balik status online.",
                "Aku di sini bang. Statusku online, alasanku masih loading.",
            ),
        ),
        "bisa_adit": (
            (
                "Bisa bantu hitung, bahas pertanyaan, sama nemenin obrolan, le.",
                "Kalau Adit mulai serius, aku bagian bikin suasananya nggak tegang.",
            ),
        ),
        "bisa_denis": (
            (
                "Bisa ngobrol sama bantu pertanyaan, bang. "
                "Kalau belum tahu ya bilang belum tahu.",
                "Tumben rapi jawabannya, Nis. Ini sudah disunting berapa kali?",
            ),
        ),
    }

    pools = {
        id(rapikan_mcb.DAILY_DIALOGS): DIALOGS,
        id(jadwal_mcb.WORK_DIALOGS): DIALOGS,
    }

    for old, new in (
        (rapikan_mcb.CALLS, GREETINGS),
        (rapikan_mcb.CHAT, CHAT),
        (rapikan_mcb.UNKNOWN, UNKNOWN),
    ):
        pools.update(
            (id(source), target)
            for source, target in zip(old, new)
        )

    pools.update(
        (id(rapikan_mcb.MEDAN_DIALOGS[key]), value)
        for key, value in banter.items()
    )

    original_choose = f.choose
    original_begin = f.begin
    original_notice = schedule.notice

    def choose(slot, options):
        return original_choose(
            slot, pools.get(id(options), options)
        )

    def begin():
        original_begin()
        f.topic = original_choose("obrolan:topic", TOPICS)
        f.recent_topics[-1] = f.topic

    async def notice(text, *, daytime=False):
        if text in jadwal_mcb.INFO:
            text = info[jadwal_mcb.INFO.index(text) % len(info)]
        return await original_notice(text, daytime=daytime)

    f.choose = choose
    f.begin = begin
    schedule.notice = notice

    if callable(getattr(c.ai, "payload", None)):
        original_payload = c.ai.payload

        def payload(history, instructions):
            return original_payload(
                history, instructions + POLICY
            )

        c.ai.payload = payload

    original_local = f.local_answer
    original_send = f.send_text
    original_start = f.start
    original_stop = f.stop
    original_observe = f.observe
    original_status = f.status

    ganjar = f.ganjar = GanjarParodi(
        c, f, maintenance, schedule
    )

    def local(index, text):
        if not f.substantive(text):
            return f.pick_local(index, GREETINGS[index])

        if (
            re.search(r"\bbisa (?:apa|apaan)\b", text, re.I)
            and not SERVICE.search(text)
        ):
            return (
                "Bisa bantu hitung, jawab pertanyaan, dan ngobrol, le. "
                "Mau bahas apa?",
                "Bisa nemenin ngobrol dan bantu pertanyaan, bang. "
                "Yang belum pasti nggak kutebak.",
                "Bisa bantu pertanyaan dan informasi grup, le. "
                "Ceritakan yang diperlukan.",
            )[index]

        return original_local(index, text)

    async def send(index, text, *, message=None, revision=None):
        source = (
            (message.text or message.caption or "")
            if message is not None
            else ""
        )

        if not SERVICE.search(source) and SERVICE.search(text):
            text = " ".join(
                part
                for part in re.split(r"(?<=[.!?])\s+", text)
                if not SERVICE.search(part)
            )
            if not text:
                return False

        ok = await original_send(
            index,
            text,
            message=message,
            revision=revision,
        )

        if (
            ok
            and message is None
            and index == 2
            and ganjar.allowed()
            and f.active
            and ganjar.last_session != f.started_at
        ):
            ganjar.last_session = f.started_at
            ganjar.interjection = (
                f.started_at,
                f.clock() + f.gap_seconds * 0.5,
            )

        return ok

    async def start(application):
        await original_start(application)
        await ganjar.start()

    async def stop():
        await ganjar.stop()
        await original_stop()

    async def observe(update, context):
        return await ganjar.observe(
            update, context, original_observe
        )

    f.local_answer = local
    f.send_text = send
    f.start = start
    f.stop = stop
    f.observe = observe
    f.status = lambda: (
        original_status() + "\nPak Ganjar: " + ganjar.state
    )

    return ganjar
