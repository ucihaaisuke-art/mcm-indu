"""Jam tenang WIB, informasi layanan, dan dialog persiapan kerja Senin."""

import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TelegramError

if __package__:
    from .rapikan_mcb import TemanTerawat
    from .vip_mcm import VIP_GROUPS
else:
    from rapikan_mcb import TemanTerawat
    from vip_mcm import VIP_GROUPS

logger = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7))
INTERVAL = 3 * 60 * 60

SENDING = (
    "can_send_messages",
    "can_send_audios",
    "can_send_documents",
    "can_send_photos",
    "can_send_videos",
    "can_send_video_notes",
    "can_send_voice_notes",
    "can_send_polls",
    "can_send_other_messages",
    "can_add_web_page_previews",
)

INFO = (
    "Butuh bantuan layanan grup? Chat pribadi Emak untuk melihat informasi paket, "
    "ketentuan akses, dan cara menghubungi admin. Periksa rincian paket sebelum membayar.",

    "Untuk pertanyaan pesanan, pembayaran, atau bantuan admin, buka chat pribadi Emak. "
    "Gunakan akun pembeli yang sama agar pesanan dan akses bisa dikenali.",

    "Baca ketentuan paket dan durasi akses sebelum memilih layanan. Jika memerlukan "
    "bantuan pesanan atau materi belajar AI non-eksplisit, tanyakan ketersediaannya kepada admin.",
)

WORK_DIALOGS = (
    (
        "Den, persiapan Senin itu enaknya diberesin sebelum malam kali.",
        "Aku udah siap, Dit. Yang belum siap cuma menerima kenyataan besok kerja.",
        "Tas sama pakaian cek dulu, Nis. Kenyataan ga bisa disetrika, kemeja bisa.",
    ),
    (
        "Kalau tugas awal pekan numpuk, urutkan yang paling penting dulu.",
        "Aku urutkan dulu siapa yang paling cepat nanya udah selesai belum.",
        "Boleh kau catat, tapi jangan suara paling keras selalu jadi prioritas.",
    ),
    (
        "Bekal sederhana bisa bikin pagi ga terlalu repot.",
        "Bekalku sering lebih siap daripada orang yang mau berangkat.",
        "Bagus itu. Tinggal orangnya disiapkan tanpa harus masuk kotak makan.",
    ),
    (
        "Kalau alarm bunyi, jangan dibahas panjang kali, Den.",
        "Aku cuma negosiasi lima menit, Dit. Masalahnya rapatnya suka diperpanjang.",
        "Alarm kau bukan serikat perundingan. Taruh agak jauh biar kau bangun.",
    ),
    (
        "Rute berangkat besok coba dicek dari sekarang.",
        "Aku sudah hafal jalan, yang belum hafal kapan jalanannya berubah jadi parkiran.",
        "Sisihkan waktu cadangan. Macet ga baca janji kau sama atasan.",
    ),
    (
        "Senin ga harus langsung menyelesaikan semua urusan sekaligus.",
        "Syukur, aku baru sanggup menyelesaikan satu urusan: membuka laptop.",
        "Pembukaannya sudah. Sekarang dokumennya ikut dibuka, Nis.",
    ),
    (
        "Rapat pagi bagus kalau tujuan dan akhirnya jelas.",
        "Yang serem itu rapat soal kenapa rapat sebelumnya belum selesai.",
        "Bah, rapatnya berkembang biak. Satu keputusan dulu aja dibawa pulang.",
    ),
    (
        "Kalau ada kerjaan yang kurang jelas, tanya sebelum nebak.",
        "Aku pernah bilang siap dengan penuh keyakinan, baru paham bingungnya setelah itu.",
        "Siap mengerjakan sama siap pura-pura paham itu beda, Nis.",
    ),
    (
        "Pagi lebih ringan kalau barang penting ga dicari sambil buru-buru.",
        "Kartu aksesku suka latihan petak umpet pas aku sudah pegang pintu.",
        "Simpan di tempat yang sama. Jangan tiap malam dia pindah kos.",
    ),
    (
        "Tugas besar bisa dipecah jadi langkah yang lebih kecil.",
        "Kalau aku pecah jadi pikirin dulu, ambil minum, baru lihat lagi.",
        "Yang pecah langkah kerjanya, bukan fokus kau ke tiga penjuru.",
    ),
    (
        "Kerja rapi itu termasuk kasih nama dokumen yang jelas.",
        "Nama fileku sudah jelas: yang_ini_baru_fix_serius.",
        "Besok Adit buka, serasa masuk babak terakhir yang ga pernah tamat.",
    ),
    (
        "Kalau perlu bantuan rekan kerja, jelaskan bagian yang mentok.",
        "Jadi jangan cuma kirim bang tolong terus hilang ya, Dit?",
        "Iya, Nis. Orang mau bantu juga butuh alamat masalahnya.",
    ),
    (
        "Sepatu dan pakaian buat besok bisa disiapkan malam ini.",
        "Udah, Dit. Tinggal semangatnya yang belum ketemu pasangan.",
        "Semangat ga harus lengkap dulu baru berangkat, bang. Mulai pelan aja.",
    ),
    (
        "Kalau hari kerja beda shift, waktu istirahatnya juga beda.",
        "Nah, jangan semua orang diajak rapat pagi. Ada yang baru pulang kerja.",
        "Betul, Nis. Tahu jadwal kawan lebih berguna daripada asal bilang malas.",
    ),
    (
        "Senin itu cuma nama hari, rencana kecil tetap bisa bantu.",
        "Iya, tapi kenapa namanya terasa tebal kali pas Minggu malam?",
        "Karena kau baca sambil mikirin semua minggu sekaligus. Besok dulu satu hari.",
    ),
    (
        "Bawa air minum sendiri bikin ga bolak-balik cari gelas.",
        "Botolku penuh, Dit. Daftar tugasku juga ga mau kalah.",
        "Yang satu diminum, yang satu dikerjain. Jangan tertukar strategi.",
    ),
    (
        "Pas kirim hasil kerja, cek lampirannya ikut atau belum.",
        "Kalimat terlampir paling berani waktu file-nya belum dipilih.",
        "Bah, pesannya sudah berangkat, berkasnya masih pakai sandal di rumah.",
    ),
    (
        "Catatan singkat habis rapat bisa mencegah salah tangkap.",
        "Aku catat siapa ngerjain apa, biar semuanya ga mendadak jadi aku.",
        "Nah, itu catatan yang berguna. Tanggung jawab harus jelas orangnya.",
    ),
    (
        "Kalau pikiran sudah penuh, jeda sebentar bisa membantu fokus.",
        "Aku boleh jeda dulu sebelum pusingnya resmi dimulai ga?",
        "Boleh istirahat, tapi jangan jadwal jedanya lebih rapi daripada jadwal kerjanya.",
    ),
    (
        "Akhir hari coba catat satu langkah buat besok.",
        "Aku catat jangan panik dulu. Biasanya itu yang keburu dikerjain.",
        "Bagus, tambah satu tindakan kecil yang nyata. Biar catatannya punya kelanjutan.",
    ),
    (
        "Kalau kerja bareng, kabari lebih awal waktu ada hambatan.",
        "Daripada semua bilang aman, ujungnya baru tahu sama-sama nyasar.",
        "Iya, bang. Kabar jujur lebih membantu daripada tenang yang ternyata kosong.",
    ),
    (
        "Besok mulai dari hal yang paling bisa kau kerjakan dulu.",
        "Aku paling bisa menyapa satpam dengan meyakinkan, Dit.",
        "Itu bagus. Setelah ramah di gerbang, tetap lanjut ke pekerjaan kau.",
    ),
    (
        "Persiapan kerja ga mesti mewah, yang penting barang pokoknya siap.",
        "Pulpen, catatan, sama muka yang belum mau ikut Senin.",
        "Mukanya boleh pelan menyusul. Jadwal berangkat jangan ikut ketinggalan.",
    ),
    (
        "Buat yang kerja Senin, malam ini usahakan cukup istirahat.",
        "Kalau masih kepikiran tugas, aku tulis dulu ya biar ga muter terus.",
        "Nah, Nis. Catat yang bisa dilakukan besok, malam ini kasih badan jeda.",
    ),
)


class ScheduleError(Exception):
    pass


class JadwalMcb:
    def __init__(self, controller, *, clock=time.time):
        self.c = controller
        self.store = controller.vip_store
        self.clock = clock
        self.group = controller.settings.target_group_id

        if (
            type(self.group) is not int
            or self.group >= 0
            or self.group in VIP_GROUPS.values()
        ):
            raise ValueError("Jadwal hanya untuk grup MCB utama.")

        self.bot = None
        self.friends = None
        self.task = None
        self.enabled = True
        self.retry_at = 0
        self.lock = asyncio.Lock()

        with self.store.lock, self.store.connection:
            self.store.connection.execute("""
                CREATE TABLE IF NOT EXISTS mcm_group_schedule (
                    bot_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    saved_permissions TEXT,
                    phase TEXT NOT NULL DEFAULT 'open',
                    notice_day TEXT,
                    next_notice REAL NOT NULL,
                    notice_index INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(bot_id,owner_id,group_id))
            """)

    def local_time(self):
        return datetime.fromtimestamp(self.clock(), WIB)

    def night(self):
        return self.enabled and self.local_time().hour < 8

    def row(self):
        with self.store.lock:
            return self.store.connection.execute(
                "SELECT * FROM mcm_group_schedule "
                "WHERE bot_id=? AND owner_id=? AND group_id=?",
                (
                    self.bot.id,
                    self.c.settings.owner_id,
                    self.group,
                ),
            ).fetchone()

    def update(self, **values):
        allowed = {
            "enabled",
            "saved_permissions",
            "phase",
            "notice_day",
            "next_notice",
            "notice_index",
        }
        if not values or not set(values) <= allowed:
            raise ValueError("Kolom jadwal tidak valid.")

        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "UPDATE mcm_group_schedule SET "
                + ",".join(key + "=?" for key in values)
                + " WHERE bot_id=? AND owner_id=? AND group_id=?",
                (
                    *values.values(),
                    self.bot.id,
                    self.c.settings.owner_id,
                    self.group,
                ),
            )

    def button(self):
        username = self.bot.username or ""
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            raise ScheduleError("Username bot belum terbaca.")

        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "CHAT EMAK",
                    url=f"https://t.me/{username}?start=menu",
                )
            ]
        ])

    async def notice(self, text, *, daytime=False):
        async with self.c.outgoing_locks.setdefault(
            self.group, asyncio.Lock()
        ):
            await self.c.wait_outgoing(self.group)

            if daytime and self.night():
                return

            try:
                await self.bot.send_message(
                    chat_id=self.group,
                    text=text,
                    reply_markup=self.button(),
                    parse_mode=None,
                    disable_notification=True,
                    read_timeout=30,
                    write_timeout=30,
                    connect_timeout=15,
                    pool_timeout=10,
                )
            except TelegramError as error:
                logger.warning(
                    "Pemberitahuan jadwal belum terkonfirmasi: jenis=%s",
                    type(error).__name__,
                )
            finally:
                self.c.record_wait(self.group, 3.5)

    async def permissions(self):
        member = await self.bot.get_chat_member(
            self.group,
            self.bot.id,
            read_timeout=20,
            write_timeout=20,
            connect_timeout=10,
            pool_timeout=10,
        )

        if (
            member.status != "creator"
            and not (
                member.status == "administrator"
                and getattr(member, "can_restrict_members", False)
            )
        ):
            raise ScheduleError(
                "Bot perlu admin dengan izin batasi anggota untuk mode malam."
            )

        chat = await self.bot.get_chat(
            self.group,
            read_timeout=20,
            write_timeout=20,
            connect_timeout=10,
            pool_timeout=10,
        )

        if getattr(chat, "permissions", None) is None:
            raise ScheduleError(
                "Izin grup belum terbaca; pengaturan tidak diubah."
            )

        raw = chat.permissions.to_dict()
        return {
            key: bool(raw.get(key, False))
            for key in ChatPermissions.all_permissions().to_dict()
        }

    async def restore(self):
        row = self.row()
        if row["saved_permissions"] is None:
            return False

        saved = json.loads(row["saved_permissions"])
        current = await self.permissions()

        for key in SENDING:
            current[key] = bool(saved.get(key, False))

        result = await self.bot.set_chat_permissions(
            self.group,
            ChatPermissions(**current),
            use_independent_chat_permissions=True,
            read_timeout=20,
            write_timeout=20,
            connect_timeout=10,
            pool_timeout=10,
        )

        if result is not True:
            raise ScheduleError(
                "Pemulihan izin grup belum dikonfirmasi Telegram."
            )

        self.update(
            saved_permissions=None,
            phase="open",
            next_notice=self.clock() + INTERVAL,
        )

        if self.friends is not None:
            self.friends.last_activity = self.clock()
            self.friends.finish()

        logger.info("Mode pagi MCB: izin kirim awal dipulihkan.")
        return True

    async def tick(self):
        if self.bot is None or self.clock() < self.retry_at:
            return

        async with self.lock:
            row = self.row()
            self.enabled = bool(row["enabled"])

            if not self.enabled:
                return

            if self.night():
                if row["saved_permissions"] is None:
                    saved = await self.permissions()
                    self.update(
                        saved_permissions=json.dumps(saved),
                        phase="closing",
                    )
                    row = self.row()

                today = self.local_time().date().isoformat()

                if row["notice_day"] != today:
                    self.update(notice_day=today)
                    await self.notice(
                        "Waktu istirahat grup, le 🌙\n\n"
                        "Chat dan media member biasa ditutup "
                        "pukul 00.00–08.00 WIB. "
                        "Untuk bantuan layanan, ketentuan akses, "
                        "atau pertanyaan pesanan, buka chat pribadi Emak "
                        "lewat tombol di bawah."
                    )

                if not self.night():
                    return

                if row["phase"] != "closed":
                    saved = await self.permissions()

                    if not self.night():
                        return

                    for key in SENDING:
                        saved[key] = False

                    result = await self.bot.set_chat_permissions(
                        self.group,
                        ChatPermissions(**saved),
                        use_independent_chat_permissions=True,
                        read_timeout=20,
                        write_timeout=20,
                        connect_timeout=10,
                        pool_timeout=10,
                    )

                    if result is not True:
                        raise ScheduleError(
                            "Penutupan grup belum dikonfirmasi Telegram."
                        )

                    self.update(phase="closed")

                    if self.friends is not None:
                        self.friends.finish()

                    logger.info(
                        "Mode malam MCB: member biasa dibatasi sampai 08.00 WIB."
                    )
                return

            if await self.restore():
                await self.notice(
                    "Selamat pagi, le ☀️\n\n"
                    "Izin kirim grup sudah dikembalikan seperti sebelum "
                    "mode malam. Jaga obrolan dan ikuti aturan grup. "
                    "Untuk bantuan layanan atau pesanan, "
                    "gunakan tombol CHAT EMAK.",
                    daytime=True,
                )
                return

            row = self.row()
            if self.clock() >= row["next_notice"]:
                index = row["notice_index"] % len(INFO)
                self.update(
                    next_notice=self.clock() + INTERVAL,
                    notice_index=index + 1,
                )
                await self.notice(INFO[index], daytime=True)

    async def safe_tick(self):
        try:
            await self.tick()
        except (
            ScheduleError,
            TelegramError,
            sqlite3.Error,
            ValueError,
            TypeError,
        ) as error:
            delay = 60

            if isinstance(error, RetryAfter):
                wait = error.retry_after
                delay = max(
                    delay,
                    int(
                        wait.total_seconds()
                        if hasattr(wait, "total_seconds")
                        else wait
                    ) + 1,
                )

            self.retry_at = self.clock() + delay
            detail = (
                str(error)
                if isinstance(error, ScheduleError)
                else type(error).__name__
            )
            logger.warning("Jadwal MCB ditunda: %s", detail)

    async def start(self, application, friends):
        self.bot = application.bot
        self.friends = friends

        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "INSERT OR IGNORE INTO mcm_group_schedule"
                "(bot_id,owner_id,group_id,next_notice) VALUES(?,?,?,?)",
                (
                    self.bot.id,
                    self.c.settings.owner_id,
                    self.group,
                    self.clock() + INTERVAL,
                ),
            )

        self.enabled = bool(self.row()["enabled"])
        await self.safe_tick()

        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

        logger.info(
            "Jadwal MCB aktif: malam 00–08 WIB; informasi 3 jam saat buka."
        )

    async def run(self):
        while True:
            await asyncio.sleep(30)
            await self.safe_tick()

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def command(self, update, context):
        message = update.effective_message

        if (
            message is None
            or update.edited_message is not None
            or message.from_user is None
            or message.from_user.is_bot
            or message.from_user.id != self.c.settings.owner_id
            or message.chat.id not in (
                self.group,
                self.c.settings.owner_id,
            )
        ):
            return

        action = (
            context.args[0].casefold()
            if context.args
            else "status"
        )

        try:
            if action == "off":
                async with self.lock:
                    await self.restore()
                    self.update(enabled=0)
                    self.enabled = False

            elif action == "on":
                self.update(enabled=1)
                self.enabled = True
                self.retry_at = 0
                await self.safe_tick()

            elif action not in ("status",):
                await message.reply_text(
                    "Pakai /jadwal, /jadwal on, atau /jadwal off, bos."
                )
                return

            chat = await context.bot.get_chat(
                self.group,
                read_timeout=20,
                write_timeout=20,
                connect_timeout=10,
                pool_timeout=10,
            )

            timer = getattr(chat, "message_auto_delete_time", 0) or 0

            timer_text = (
                "24 jam aktif"
                if timer == 86400
                else f"{timer} detik"
                if timer
                else "belum aktif; atur Hapus Otomatis di Telegram"
            )

            phase = self.row()["phase"]

            await message.reply_text(
                f"Jadwal {'ON' if self.enabled else 'OFF'}, bos.\n"
                "Waktu: WIB (Medan).\n"
                "Malam: 00.00–08.00.\n"
                f"Status izin: {phase}.\n"
                "Informasi: tiap 3 jam saat buka.\n"
                f"Hapus otomatis Telegram: {timer_text}.\n"
                "/jadwal off memulihkan izin kirim dan menghentikan jadwal.",
                parse_mode=None,
            )

        except (
            ScheduleError,
            TelegramError,
            sqlite3.Error,
            ValueError,
        ) as error:
            detail = (
                str(error)
                if isinstance(error, ScheduleError)
                else type(error).__name__
            )
            await message.reply_text(
                f"Jadwal belum selesai diproses, bos: {detail}",
                parse_mode=None,
            )


class TemanKerja(TemanTerawat):
    def __init__(self, controller, maintenance, schedule):
        super().__init__(controller, maintenance)
        self.schedule = schedule

    def allowed(self):
        return super().allowed() and not self.schedule.night()

    def work_day(self):
        return self.schedule.local_time().weekday() in (6, 0)

    def begin(self):
        super().begin()
        if self.work_day():
            self.topic = (
                "persiapan kerja Senin dan awal pekan; "
                "hormati juga pekerja shift dan yang belum bekerja"
            )

    def prepare_local(self):
        if not self.work_day():
            return super().prepare_local()

        if self.turn % 3 == 0 or not hasattr(self, "daily_pack"):
            self.daily_pack = self.choose(
                "kerja_senin",
                WORK_DIALOGS,
            )

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

    async def generate(self, history, instructions, *, json_mode=False):
        if self.work_day():
            instructions += (
                " Tema hari ini persiapan Senin atau awal pekan kerja. "
                "Jangan menganggap semua orang kerja kantor; hormati "
                "pekerja shift, yang libur, dan yang mencari kerja."
            )
        return await super().generate(
            history,
            instructions,
            json_mode=json_mode,
        )

    async def tick(self):
        self.last_vip_invite = self.clock()
        await super().tick()
