"""Pengingat VIP, penghapusan chat sementara, dan gaya dialog teman Emak."""

import asyncio
import hashlib
import json
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
    from .teman_ngobrol import ALIASES, LOCAL_REPLIES, LOCAL_UNKNOWN, NAMES, TemanNgobrol
    from .vip_mcm import PLANS
else:
    from teman_ngobrol import ALIASES, LOCAL_REPLIES, LOCAL_UNKNOWN, NAMES, TemanNgobrol
    from vip_mcm import PLANS

logger = logging.getLogger(__name__)
VIP_PATTERN = re.compile(r"(?<!\w)/(?:vip|akses|cek|pending|setqris|batal)(?:@\w+)?\b|\b(?:vip|qris|paket\s+(?:grup|premium|lengkap)|bukti\s+(?:bayar|transfer))\b", re.I)
NO_NEED = re.compile(r"^\s*(?:(?:dit+|adit+|nis+|den{1,2}is+)\s*[,!]?\s*)?(?:ga|gak|nggak|enggak|tidak|tak)\s*(?:ada|perlu|usah)(?:\s+(?:apa|apa-apa|apaapa|lagi|kok|aja|doang|makasih|mksh|bang|bangku|abangku|le|dit+|adit+|nis+|den{1,2}is+))*[\s,.!?]*$", re.I)

DAILY_DIALOGS = (
    ("Nama file paling mencurigakan itu final_beneran_terakhir.", "Besok lahir adiknya: final_fix_jangan_diedit_lagi.", "Ujungnya yang dikirim malah dokumen kosong. Dramanya lengkap."),
    ("Kalau nonton film misteri, petunjuk kecil suka kelewat.", "Aku fokus nebak pelaku, ternyata masih iklan pembuka.", "Pantes semua merek kau tuduh, Nis."),
    ("Roti yang dipanggang sebentar sama kelamaan nasibnya jauh kali.", "Beda satu lagu, dari sarapan jadi barang bukti.", "Hitamnya jangan kau promosikan sebagai rasa arang premium."),
    ("Menurutku kalimat sesuai selera itu bagian resep paling berani.", "Selera siapa dulu? Selera aku kadang pengen masakan langsung jadi.", "Kalau begitu resep kau pendek: pesan, tunggu, makan."),
    ("Foto makanan bagus itu banyak main di cahaya.", "Kalau aku main di kecepatan, sebelum keburu dimakan yang lain.", "Di grup ini dokumentasi kalah sama distribusi."),
    ("Buku baru kadang dibeli karena pengen mulai rajin baca.", "Terus rajinnya pindah ke menyusun buku di rak.", "Setidaknya raknya sudah lulus orientasi."),
    ("Tombol lewati intro itu kecil, tapi berguna kali.", "Coba ada buat cerita orang yang pembukaannya tiga kecamatan.", "Bah, jangan kau pencet waktu Emak baru bilang jadi begini."),
    ("Tanaman layu belum tentu kurang air, bisa juga kebanyakan.", "Berarti perhatian juga perlu takaran ya.", "Iya, pot bunga bukan tempat latihan bikin danau."),
    ("Satu kata salah ketik bisa mengubah suasana rapat.", "Maksudnya siap pak, terkirim siapa pak. Langsung jadi tes pengenalan atasan.", "Yang baca ikut mikir, ini karyawan baru atau orang nyasar."),
    ("Di peta, jarak dekat belum tentu jalannya gampang.", "Betul, kadang kelihatan sebelahan tapi dipisah pagar pabrik.", "Jangan sampai rute tercepat malah butuh izin satpam."),
    ("Pas bikin teh, air panasnya bikin orang mendadak sabar.", "Aku udah siap minum, tehnya masih mode lava.", "Tiup dulu, bukan ditatap sambil berharap dia ngerti."),
    ("Kalau semua jawaban pilihan ganda sama-sama masuk akal, curiga juga.", "Aku malah curiga kalau tiga nomor berturut jawabannya C.", "Hurufnya ga punya kewajiban tampil bergantian, Nis."),
    ("Kadang judul video lebih heboh dari kejadiannya.", "Judulnya seluruh warga panik, isinya satu orang nyari tutup toples.", "Toplesnya memang penting, tapi jangan sekampung jadi figuran."),
    ("Susah juga menjelaskan arah kalau patokannya toko yang udah tutup.", "Belok setelah bekas warung yang dulu warna hijau.", "Yang bisa sampai cuma warga lama sama penjelajah waktu."),
    ("Di game, tas karakter bisa muat barang aneh-aneh.", "Bawa batu dua puluh kilo aman, mau tambah bunga malah penuh.", "Tasnya pilih-pilih tanggung jawab rupanya."),
    ("Kalau presentasi, contoh sederhana sering lebih nempel.", "Aku pernah hafal contoh, pas ditanya pengertiannya malah kosong.", "Contohnya sudah sampai, penjelasannya masih cari parkir."),
    ("Pakaian habis dijemur itu kadang masih hangat.", "Sensasinya kayak dapat pelukan dari setrikaan yang udah pensiun.", "Perumpamaan kau jauh juga muternya, Nis."),
    ("Kalau ikut lomba masak, penampilan bukan satu-satunya nilai.", "Syukur, masakanku penampilannya suka bikin juri banyak bertanya.", "Selama pertanyaannya bukan ini harus dimakan atau dilaporkan."),
    ("Banyak orang memilih kursi kereta dekat jendela.", "Aku pilih yang ga bikin siku perang dingin sama tetangga.", "Perjanjian batas wilayah sandaran tangan memang belum selesai."),
    ("Nulis daftar kerja itu membantu lihat prioritas.", "Punyaku nomor satu bikin daftar, langsung bisa dicentang.", "Pembukaannya meyakinkan. Sekarang coba nomor duanya ikut hidup."),
    ("Kadang nada chat bisa salah ditangkap.", "Tulis oke titik aja bisa terasa kayak dipanggil kepala sekolah.", "Emak kalau tulis oke memang oke. Ga sedang menyusun dakwaan."),
    ("Bungkus makanan rapat itu bagus buat perjalanan.", "Saking rapatnya, yang kelaparan harus lulus tes kesabaran dulu.", "Lauk aman sampai rumah, pembukanya yang mulai emosional."),
    ("Saat menghafal, mengulang dengan bahasa sendiri bisa membantu.", "Aku ganti bahasanya, kadang maknanya ikut pindah alamat.", "Parafrase boleh, bikin cerita cabang jangan."),
    ("Ada lagu yang baru enak setelah didengar beberapa kali.", "Ada juga yang baru mulai, aku udah hafal bagian skip-nya.", "Selera boleh beda. Speaker tetangga jangan jadi korban."),
    ("Tutup botol yang terlalu kencang bikin semua orang ikut mencoba.", "Yang terakhir buka langsung dianggap punya kekuatan tersembunyi.", "Padahal yang empat orang tadi udah pemanasan buat dia."),
    ("Mesin cuci selesai bunyinya semangat kali.", "Kayak menang lomba, padahal yang jemur tetap kita.", "Dia merayakan serah terima pekerjaan."),
    ("Pas potong rambut, contoh foto membantu jelasin modelnya.", "Foto modelnya bagus, bentuk kepalaku mengajukan keberatan.", "Tukang cukur ngatur rambut, Nis. Bukan cetak ulang orang."),
    ("Teka-teki enak kalau jawabannya bikin oh iya juga.", "Kalau jawabannya maksa, aku malah pengen banding ke panitia.", "Panitianya cuma Adit, jangan berharap ada pengadilan teka-teki."),
    ("Beda orang, beda cara menata meja kerja.", "Mejaku pakai sistem lapisan sejarah. Makin bawah makin lama.", "Kalau cari pulpen perlu kuas arkeologi, sudah kelewatan itu."),
    ("Es di minuman lama-lama bikin rasanya berubah.", "Makanya aku minum cepat demi menjaga integritas rasa.", "Alasan ilmiahnya rapi kali buat orang yang memang haus."),
    ("Kalau penjelasan panjang, berhenti sebentar bisa membantu orang nyimak.", "Aku berhenti sebentar, orangnya sekalian pamit.", "Jeda kau mungkin sudah masuk jam istirahat."),
    ("Kamera depan dan belakang kadang bikin orang merasa beda.", "Yang satu cocok buat foto, yang satu cocok buat evaluasi diri.", "Sudutnya diubah dulu, jangan langsung ganti identitas."),
    ("Baca ulasan barang perlu lihat keluhan yang berulang.", "Ada ulasan bintang satu: kurirnya datang waktu aku mandi.", "Barangnya ikut kena urusan jadwal pribadi."),
    ("Kalau cerita pakai diagram, hubungan antarbagian lebih kelihatan.", "Diagram keluarga di sinetron perlu papan sepanjang gang.", "Baru dua episode, penghapusnya udah minta cuti."),
    ("Suara hujan bisa bikin suasana tenang.", "Sampai ada bunyi tetesan yang lokasinya ternyata di ruang tamu.", "Itu bukan musik latar lagi, itu panggilan ember."),
    ("Kipas angin bisa bikin kertas di meja berpetualang.", "Catatanku pindah sendiri, mungkin pengen suasana kerja baru.", "Kertasnya produktif bergerak. Yang nulis masih duduk."),
    ("Ada orang cepat hafal wajah tapi susah ingat nama.", "Akhirnya sapaan jadi panjang: bang yang waktu itu sama si itu.", "Alamat percakapannya lengkap, penerimanya ga jelas."),
    ("Kalau lomba tarik tambang, kompaknya penting.", "Kalau tarik kesimpulan, aku kadang terlalu cepat.", "Yang ditarik kalimat Adit malah ke cabang lomba lain."),
    ("Belajar bahasa sering ketemu kata yang mirip tapi beda arti.", "Aku percaya diri dulu, kamusnya protes belakangan.", "Bagus berani mencoba, asal mau koreksi pas keliru."),
    ("Pintu otomatis bisa terbuka karena sensor mendeteksi orang.", "Aku suka melambat biar berasa sedang disambut kerajaan.", "Silakan Yang Mulia, keranjang belanjanya tetap angkat sendiri."),
    ("Saat pindah rumah, barang kecil paling gampang terselip.", "Aneh ya, barang yang mau dibuang malah selalu ketemu.", "Dia merasa sudah lama mengabdi, minta diperpanjang kontrak."),
    ("Kalau bikin nama grup, yang pendek gampang dicari.", "Kalau kepanjangan, baca namanya aja udah lewat jam kumpul.", "Belum lagi ganti nama tiap ada kejadian. Arsipnya ikut olahraga."),
    ("Orang bisa beda pendapat tanpa harus saling nyolot.", "Kecuali rebutan potongan terakhir, tatapannya suka ikut rapat.", "Potong dua aja. Jangan sampai risol perlu mediator."),
    ("Tebak gambar itu seru karena orang bisa lihat hal yang beda.", "Kau gambar payung, aku lihat jamur lagi rapat.", "Setidaknya imajinasi kau ga kekurangan peserta."),
    ("Makanan pedas itu toleransinya beda-beda.", "Level satu buat orang lain, buat aku udah adegan klimaks.", "Ga usah lomba kuat. Makan enak bukan ujian kenaikan pangkat."),
    ("Kertas petunjuk perakitan paling berguna dibaca sebelum mulai.", "Aku biasanya baca pas ada sisa baut yang merasa ditinggalkan.", "Itu baut bukan bonus loyalitas, Nis."),
    ("Debat akhir film bisa lebih panjang daripada filmnya.", "Yang paling yakin jelasin malah tadi tidur di bagian penting.", "Analisisnya hasil mimpi tambahan rupanya."),
    ("Kalau mau rekam suara, tempat yang tenang membantu.", "Begitu pencet rekam, tetangga serempak menemukan hobi bertukang.", "Satu gang mendadak jadi tim efek suara."),
    ("Tanda baca kecil, pengaruhnya bisa besar.", "Makan dulu nenek sama makan dulu, nenek beda suasana kali.", "Koma memang kadang jadi petugas penyelamat."),
    ("Orang yang paham biasanya bisa mengakui bagian yang belum dia tahu.", "Kalau aku belum tahu pertanyaannya aja udah angguk duluan.", "Kepalamu jangan dijadikan tanda setuju otomatis."),
    ("Sepatu baru kadang perlu waktu biar nyaman.", "Sepatunya baru, cara jalanku jadi versi percobaan.", "Kalau sakit jangan dipaksa. Bukan semua hal perlu diperjuangkan pakai lecet."),
    ("Di kuis, bunyi tombol cepat bikin suasana tegang.", "Aku pengen mencet, jawaban belum ada tapi semangatnya duluan.", "Semangat kau sudah final, isinya masih babak penyisihan."),
    ("Kalau belanja bahan masakan, ukuran ikat itu suka beda-beda.", "Satu ikat di resep, di pasar malah kayak bouquet wisuda.", "Makan bayam tiga hari sambil merasa baru lulus."),
    ("Hewan di dokumenter kelihatannya tenang karena kita cuma lihat cuplikannya.", "Berarti kalau hidupku diedit rapi, bisa terlihat teratur juga.", "Bisa. Tapi editor minta tambahan waktu dan penjelasan."),
    ("Kursi yang bunyinya keras bikin ruangan langsung menoleh.", "Padahal yang pindah cuma dua senti, pengumumannya sekelurahan.", "Kursinya punya jiwa pembawa acara."),
    ("Menjelaskan aturan permainan perlu contoh satu putaran.", "Aku paling ngerti setelah kalah pertama. Itu biaya kursusnya.", "Asal putaran kedua ga tanya ini main apa lagi."),
    ("Papan tulis penuh itu belum tentu berarti penjelasannya lengkap.", "Kadang separuhnya cuma gambar panah nyari tujuan.", "Panahnya saling menunjuk, kesimpulannya belum datang."),
    ("Kalau baca komik, ekspresi kecil bisa bikin adegan tambah lucu.", "Aku ketawa duluan, pas baca balonnya ternyata adegan serius.", "Ekspresi kau sendiri sekarang yang butuh keterangan."),
    ("Wadah bekas es krim sering punya kehidupan kedua.", "Harapan manis, kenyataan sambal. Plot twist paling dekat dari rumah.", "Yang bikin tegang itu buka tutupnya sambil udah pegang sendok."),
    ("Kadang tugas terasa berat karena belum jelas mulai dari mana.", "Kalau mulai dari kasih nama folder, masih dihitung bergerak kan?", "Dihitung. Habis itu isinya ikut dikerjain, jangan foldernya doang yang punya masa depan."),
)

# Urutan setiap pool diacak dan disimpan; stok dipakai habis sebelum diulang.
CALLS = (
    ("Nyimak aku, abangku.", "Adit masuk percakapan.", "Iya, kedengaran sampai sini.", "Kau panggil, aku nyaut.", "Ada aku. Lanjutkan aja.", "Aku baca, ga kelewat kok.", "Hadir di barisan chat.", "Siap dengar versimu.", "Sini, aku ikut nyimak.", "Panggilan diterima, bang.", "Adit masih di sini.", "Bah, pas kali aku lihat chat."),
    ("Nis hadir, tanpa acara pembukaan.", "Aku nyaut. Absen jangan dicoret.", "Ada, bang. Belum jadi legenda grup.", "Kedengaran, ga perlu pakai toa.", "Iya, Denis yang kau cari.", "Masuk aku, pintunya chat ini kan?", "Hadir. Kursi virtual masih kosong.", "Nyimak, kuping versi digital siap.", "Nama dipanggil, orangnya bot yang nyaut.", "Bah, nongol juga giliranku.", "Aku baca. Bukan centang hiasan.", "Sampai panggilannya, bang."),
    ("Emak nyimak dari sini.", "Iya, masuk ceritanya.", "Emak ikut baca.", "Ada, kawan. Lanjut aja.", "Kedengaran sama Emak.", "Pelan aja, Emak simak.", "Emak hadir tanpa pidato.", "Sini, kita baca bareng.", "Aku perhatiin, bang.", "Chatnya sampai, tenang.", "Emak belum ke mana-mana.", "Bah, ada cerita rupanya."),
)
CLOSINGS = (
    ("Oke, aku kasih ruang dulu.", "Sip, panggil lagi kalau perlu.", "Baik, kita santai dulu.", "Paham. Aku nyimak aja dulu.", "Beres, ga aku panjangin.", "Aman, ga mesti ada urusan kok."),
    ("Oke, aku parkir candaan dulu.", "Sip. Mikrofon imajiner aku taruh.", "Paham, ga jadi buka rapat.", "Baiklah, aku tenang sebentar.", "Aman. Absen doang juga boleh.", "Oke, bang. Kita leyeh dulu."),
    ("Oke, Emak kasih jeda.", "Sip, ga perlu dipanjangin.", "Baik, lanjut santai aja.", "Paham, Emak nyimak dulu.", "Beres, panggil kalau perlu.", "Aman, ga ada yang dikejar."),
)
CHAT = (
    ("Aku ikutin dulu alur ceritanya.", "Bagian itu yang bikin obrolannya menarik.", "Coba teruskan, biar konteksnya utuh.", "Aku baca maksudmu dulu, ga buru-buru simpulkan.", "Bisa dilihat dari sisi lain juga itu.", "Kalau ada detailnya, baru lebih enak kita bahas."),
    ("Aku ikut nyimak, komentar recehnya antre dulu.", "Oke, alurnya masih aku kejar.", "Lanjut, jangan berhenti pas mulai menarik.", "Bah, kalimat pembukanya sudah bikin nengok.", "Aku tahan tebak-tebakannya dulu.", "Ini perlu dibaca dua kali biar aku ga salah belok."),
    ("Emak baca dulu sampai utuh.", "Lanjut aja, ga perlu pakai bahasa rapat.", "Aku ikutin ceritanya, bang.", "Bagian itu boleh dijelasin lagi.", "Oke, konteksnya mulai kelihatan.", "Emak simak tanpa potong dulu."),
)
UNKNOWN = (
    ("Aku belum bisa pastikan yang itu. Coba panggil mak dengan pertanyaannya supaya Emak bisa cek lewat jalur AI.", "Yang itu di luar jawaban lokal yang aku punya. Tulis pertanyaannya ke mak, jangan kita isi pakai tebakan.", "Aku belum pegang jawabannya, bang. Panggil mak buat bantu bahas lebih jauh."),
    ("Yang ini aku belum tahu. Kalau diteruskan nebak, nanti jadi cerita karangan; coba panggil mak.", "Stok jawabanku belum sampai situ, bang. Pertanyaannya lempar ke mak dulu.", "Aku angkat tangan buat yang ini. Panggil mak biar kita ga kompak salah."),
    ("Jawaban lokal Emak belum mencakup itu. Panggil mak dengan pertanyaan lengkap untuk jalur AI.", "Yang ini perlu jawaban di luar stok lokal. Coba arahkan pertanyaannya pakai mak.", "Emak belum bisa memastikan dari stok lokal. Tanyakan lagi dengan panggilan mak supaya masuk jalur AI."),
)
SOCIAL = (
    (r"(?:apa|gimana) kabar|sehat (?:ga|gak)", (
        ("Siap ngobrol. Kau sendiri lagi gimana?", "Aman buat lanjut bahas cerita kau."),
        ("Masih sigap nyaut, lawaknya belum tentu sigap.", "Aman, bang. Bahan bercandanya sedang pemanasan."),
        ("Emak siap nyimak. Kau baik-baik aja?", "Ada di sini, bang. Cerita aja kalau lagi penuh pikiran."))),
    (r"lagi (?:apa|ngapain)", (
        ("Lagi ikutin alur chat di sini.", "Nunggu bagian yang bisa aku bantu jelasin."),
        ("Lagi menahan komentar sebelum salah sambung.", "Lagi baca chat, belum buka cabang keributan."),
        ("Lagi nyimak percakapan kalian.", "Standby di chat ini, bang."))),
    (r"(?:makasih|makasi|terima kasih|thanks|thank you|tengkyu|mksh)(?: ya)?", (
        ("Sama-sama. Senang kalau penjelasannya kepakai.", "Sip, semoga membantu beresin yang tadi."),
        ("Sama-sama, ga pakai biaya tepuk tangan.", "Sip, bang. Kali ini aku nyambung juga."),
        ("Sama-sama, kawan.", "Sip, senang kalau bisa bantu."))),
    (r"(?:wkwk\w*|haha\w*|hehe\w*|ngakak|awok\w*)", (
        ("Bah, bagian itu rupanya yang kena.", "Oke, kita sepakat yang tadi agak di luar dugaan."),
        ("Nah, ada saksi candaan ini masih hidup.", "Ketawa dulu, penjelasannya nanti kalau diminta."),
        ("Rame kali kalian, Emak ikut senyum.", "Sudah, ambil napas dulu sebelum lanjut."))),
    (r"(?:garing|ga lucu|gak lucu|nggak lucu|receh kali|itu lagi|sama terus)", (
        ("Oke, aku cari sudut lain. Yang tadi ga usah dipaksa.", "Catat, kita pindah bahasan aja."),
        ("Iya, yang itu aku pensiunkan dulu.", "Gagal mendarat rupanya. Aku ganti jalur candaan."),
        ("Paham, jangan diulang sampai jadi tugas hafalan.", "Oke, kita cari obrolan yang lain."))),
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
                    bot_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, installed_at INTEGER NOT NULL,
                    PRIMARY KEY(bot_id, owner_id));
                CREATE TABLE IF NOT EXISTS mcm_reminders (
                    bot_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, order_id TEXT NOT NULL,
                    number INTEGER NOT NULL, due_at INTEGER NOT NULL, attempted_at INTEGER,
                    delivered_at INTEGER, error_type TEXT,
                    PRIMARY KEY(bot_id, owner_id, order_id, number));
                CREATE TABLE IF NOT EXISTS mcm_cleanup (
                    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, sender_bot INTEGER NOT NULL,
                    due_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, finished INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(chat_id, message_id));
                CREATE TABLE IF NOT EXISTS mcm_dialog_progress (
                    group_id INTEGER NOT NULL, day TEXT NOT NULL, position INTEGER NOT NULL,
                    PRIMARY KEY(group_id, day));
                CREATE TABLE IF NOT EXISTS mcm_phrase_rotation (
                    group_id INTEGER NOT NULL, slot TEXT NOT NULL,
                    remaining TEXT NOT NULL, last_choice INTEGER,
                    PRIMARY KEY(group_id, slot));
            """)
        self.install_handlers()

    def install_handlers(self):
        original_menu = self.c.vip_menu
        original_send = self.c.send_group_text

        async def private_menu(update, context):
            if update.effective_chat.type != "private":
                return
            rows = [[InlineKeyboardButton(
                f"{plan.name} — Rp{plan.amount:,}".replace(",", "."),
                callback_data="vip:buy:" + key,
            )] for key, plan in PLANS.items()]
            await update.effective_message.reply_text(
                "Mau paket yang mana, pak/ibu bos? 😄\n\n" + "\n".join(
                    f"{plan.name}: Rp{plan.amount:,}".replace(",", ".") for plan in PLANS.values()
                ) + "\n\nAkses permanen, bayar sekali. TAKE ALL masuk ke satu grup gabungan.\n"
                "Bayar QRIS, kirim bukti dan nama pengirim, lalu tunggu owner cek uang masuk.\n"
                "Pakai /akses untuk mengambil akses yang sudah lunas. /batal untuk kembali ngobrol.",
                reply_markup=InlineKeyboardMarkup(rows), parse_mode=None)

        async def menu(update, context):
            message = update.effective_message
            if update.effective_chat.id == self.c.settings.target_group_id:
                sent = await message.reply_text("Pak/ibu bos, buka chat pribadi Emak lalu ketik /vip ya 😄", parse_mode=None)
                self.expire(context.bot, message.chat.id, message.message_id, 60)
                self.expire(context.bot, message.chat.id, sent.message_id, 60)
                return
            return await original_menu(update, context)

        async def send(message, text, **kwargs):
            result = await original_send(message, text, **kwargs)
            quote = getattr(message, "reply_to_message", None)
            if message.chat.id == self.c.settings.target_group_id and vip_text(
                    text, message.text or message.caption, (quote.text or quote.caption) if quote else ""):
                self.expire(self.bot or self.c.vip.bot, message.chat.id, message.message_id, 60)
                if result is not None:
                    self.expire(self.bot or self.c.vip.bot, message.chat.id, result.message_id, 60)
            return result

        self.c.vip.start = private_menu
        self.c.vip_menu = menu
        self.c.send_group_text = send

    def expire(self, bot, chat_id, message_id, seconds):
        if bot is None or chat_id != self.c.settings.target_group_id:
            return
        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "INSERT INTO mcm_cleanup(chat_id,message_id,sender_bot,due_at) VALUES(?,?,?,?) "
                "ON CONFLICT(chat_id,message_id) DO UPDATE SET due_at=MIN(due_at,excluded.due_at)",
                (chat_id, message_id, bot.id, int(self.clock() + seconds)))
        self.wake.set()

    async def observe(self, update, context):
        m = update.effective_message
        if m is None or m.chat.id != self.c.settings.target_group_id or not m.text:
            return
        quote = m.reply_to_message
        if vip_text(m.text, (quote.text or quote.caption) if quote else ""):
            self.expire(context.bot, m.chat.id, m.message_id, 60)

    async def start(self, application, friends):
        self.bot, self.friends = application.bot, friends
        with self.store.lock, self.store.connection:
            self.store.connection.execute("INSERT OR IGNORE INTO mcm_rapi_meta VALUES(?,?,?)",
                (self.bot.id, self.c.settings.owner_id, int(self.clock())))
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def enroll_reminders(self):
        owner, bot_id = self.c.settings.owner_id, self.bot.id
        with self.store.lock, self.store.connection:
            installed = self.store.connection.execute(
                "SELECT installed_at FROM mcm_rapi_meta WHERE bot_id=? AND owner_id=?", (bot_id, owner)).fetchone()[0]
            for number, delay in ((1, 300), (2, 3600)):
                self.store.connection.execute(
                    "INSERT OR IGNORE INTO mcm_reminders(bot_id,owner_id,order_id,number,due_at) "
                    "SELECT ?,?,o.order_id,?,m.approved_at+? FROM orders o JOIN manual_orders m USING(order_id) "
                    "WHERE o.state='paid' AND m.owner_id=? AND m.approved_by=? AND m.approved_at>=? "
                    "AND EXISTS(SELECT 1 FROM manual_reviews v WHERE v.order_id=o.order_id AND v.owner_id=? AND v.bot_id=?)",
                    (bot_id, owner, number, delay, owner, owner, installed, owner, bot_id))

    async def process(self):
        self.enroll_reminders()
        now = int(self.clock())
        with self.store.lock:
            reminders = self.store.connection.execute(
                "SELECT r.*,o.user_id,m.plan_id FROM mcm_reminders r JOIN orders o USING(order_id) "
                "JOIN manual_orders m USING(order_id) WHERE r.bot_id=? AND r.owner_id=? "
                "AND r.attempted_at IS NULL AND r.due_at<=? AND o.state='paid' ORDER BY r.due_at LIMIT 20",
                (self.bot.id, self.c.settings.owner_id, now)).fetchall()
        for row in reminders:
            key = (row["bot_id"], row["owner_id"], row["order_id"], row["number"])
            with self.store.lock, self.store.connection:
                claimed = self.store.connection.execute(
                    "UPDATE mcm_reminders SET attempted_at=? WHERE bot_id=? AND owner_id=? AND order_id=? "
                    "AND number=? AND attempted_at IS NULL AND due_at<=?", (now, *key, now)).rowcount
                if claimed and row["number"] == 1:
                    self.store.connection.execute("UPDATE mcm_reminders SET due_at=MAX(due_at,?) "
                        "WHERE bot_id=? AND owner_id=? AND order_id=? AND number=2 AND attempted_at IS NULL",
                        (now + 3300, row["bot_id"], row["owner_id"], row["order_id"]))
            if not claimed:
                continue
            plan = PLANS.get(row["plan_id"])
            name = plan.name if plan else "VIP"
            action = ("Gas buka websitenya, pak bos." if plan and plan.kind == "website"
                      else "Gas join grupnya, pak bos.")
            try:
                await self.bot.send_message(chat_id=row["user_id"], parse_mode=None,
                    text=f"Hallo pak bos 👋\nPaket VIP yang diorder sudah di-ACC.\nPaket: {name}\n\n"
                    + action + " Ketik /akses di chat ini untuk mengambil link aksesnya.")
                with self.store.lock, self.store.connection:
                    self.store.connection.execute("UPDATE mcm_reminders SET delivered_at=? WHERE bot_id=? "
                        "AND owner_id=? AND order_id=? AND number=?", (int(self.clock()), *key))
            except TelegramError as error:
                with self.store.lock, self.store.connection:
                    self.store.connection.execute("UPDATE mcm_reminders SET error_type=? WHERE bot_id=? "
                        "AND owner_id=? AND order_id=? AND number=?", (type(error).__name__, *key))
                logger.warning("Pengingat VIP tertunda: nomor=%s; jenis=%s", row["number"], type(error).__name__)
        await self.cleanup(now)

    async def cleanup(self, now):
        with self.store.lock:
            rows = self.store.connection.execute(
                "SELECT * FROM mcm_cleanup WHERE chat_id=? AND finished=0 AND due_at<=? ORDER BY due_at LIMIT 50",
                (self.c.settings.target_group_id, now)).fetchall()
        clients = {self.bot.id: self.bot}
        clients.update({bot.id: bot for bot in getattr(self.friends, "bots", ())})
        for row in rows:
            bot = clients.get(row["sender_bot"], self.bot)
            finished, delay = True, 30
            try:
                await bot.delete_message(chat_id=row["chat_id"], message_id=row["message_id"])
            except BadRequest as error:
                if "message to delete not found" not in str(error).lower():
                    logger.warning("Hapus chat belum berhasil: jenis=BadRequest. Periksa izin hapus bot.")
            except RetryAfter as error:
                wait = error.retry_after
                delay = max(30, int(wait.total_seconds() if hasattr(wait, "total_seconds") else wait) + 1)
                finished = False
            except TelegramError as error:
                finished = row["attempts"] >= 4
                logger.warning("Hapus chat ditunda: jenis=%s", type(error).__name__)
            with self.store.lock, self.store.connection:
                self.store.connection.execute("UPDATE mcm_cleanup SET finished=?,attempts=attempts+1,due_at=? "
                    "WHERE chat_id=? AND message_id=?", (int(finished), now + delay, row["chat_id"], row["message_id"]))

    async def run(self):
        while True:
            self.wake.clear()
            try:
                await self.process()
            except (sqlite3.Error, TelegramError) as error:
                logger.warning("Pemeliharaan MCB ditunda: jenis=%s", type(error).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=10)
            except TimeoutError:
                pass


class TemanTerawat(TemanNgobrol):
    def __init__(self, controller, maintenance):
        super().__init__(controller)
        self.maintenance = maintenance
        self.contacts = {}
        logger.info("Kosakata baru aktif: %s paket dialog; rotasi tersimpan.", len(DAILY_DIALOGS))

    def choose(self, slot, options):
        options = tuple(options)
        if not options:
            raise ValueError("Stok kalimat kosong.")
        signature = hashlib.sha256(json.dumps(options, ensure_ascii=False).encode()).hexdigest()[:16]
        slot = f"{slot}:{signature}"
        store = self.maintenance.store
        with store.lock, store.connection:
            row = store.connection.execute(
                "SELECT remaining,last_choice FROM mcm_phrase_rotation WHERE group_id=? AND slot=?",
                (self.group, slot)).fetchone()
            remaining, previous = [], row[1] if row else None
            if row:
                try:
                    remaining = json.loads(row[0])
                    if (not isinstance(remaining, list) or any(type(i) is not int or not 0 <= i < len(options)
                            for i in remaining) or len(set(remaining)) != len(remaining)):
                        remaining = []
                except (ValueError, TypeError):
                    remaining = []
            if not remaining:
                remaining = list(range(len(options)))
                random.shuffle(remaining)
                if len(remaining) > 1 and remaining[0] == previous:
                    remaining[0], remaining[-1] = remaining[-1], remaining[0]
            selected = remaining.pop(0)
            store.connection.execute(
                "INSERT INTO mcm_phrase_rotation VALUES(?,?,?,?) ON CONFLICT(group_id,slot) "
                "DO UPDATE SET remaining=excluded.remaining,last_choice=excluded.last_choice",
                (self.group, slot, json.dumps(remaining), selected))
        return options[selected]

    def pick_local(self, index, options):
        answer = self.choose(f"reply:{index}", options)
        self.local_recent[index].append(answer)
        return answer

    def local_answer(self, index, text):
        if NO_NEED.fullmatch(text):
            return self.pick_local(index, CLOSINGS[index])
        rest = ALIASES[index].sub("", text).strip(" ,!.?") if index < 2 else text
        if not rest or re.fullmatch(r"(?:woi|woy|oi|oy|eh|hei|hey|bang|bro)[\s!,.?]*", rest, re.I):
            return self.pick_local(index, CALLS[index])
        plain = re.sub(r"\s+", " ", rest.casefold()).strip(" ,!.?")
        for pattern, pools in SOCIAL:
            if re.fullmatch(pattern, plain, re.I):
                return self.pick_local(index, pools[index])
        answer = super().local_answer(index, text)
        if answer in LOCAL_UNKNOWN[index]:
            return self.pick_local(index, UNKNOWN[index])
        if answer in LOCAL_REPLIES[index]:
            return self.pick_local(index, CHAT[index])
        return answer

    async def generate(self, history, instructions, *, json_mode=False):
        fresh = (
            " Variasikan gaya: Adit boleh mengamati atau ikut bercanda tanpa selalu bertanya; "
            "Denis tidak harus menutup setiap pesan dengan emoji; Emak boleh ikut menikmati candaan "
            "tanpa memberi nasihat. Jangan mendaur ulang punchline, pembuka, atau perumpamaan dalam "
            "riwayat. Hindari menjadikan saldo, dompet, dan tagihan sebagai lelucon bawaan. "
            "Sambungkan makna tiap giliran, jangan cuma mengganti nama benda dalam lelucon yang sama."
        )
        return await super().generate(history, instructions + fresh, json_mode=json_mode)

    async def observe(self, update, context):
        m = update.effective_message
        if (m is not None and m.chat.id == self.group and update.edited_message is None
                and m.from_user is not None and not m.from_user.is_bot and m.sender_chat is None
                and NO_NEED.fullmatch(m.text or "") and self.allowed()):
            key = (m.chat.id, m.message_id)
            if key in self.seen:
                raise ApplicationHandlerStop
            contact = self.contacts.get(m.from_user.id)
            quoted = getattr(m.reply_to_message, "from_user", None)
            index = next((i for i, bot in enumerate(self.bots) if quoted and bot.id == quoted.id), None)
            if index is None:
                index = next((i for i, pattern in enumerate(ALIASES) if pattern.search(m.text)), None)
            if index is None and contact and self.clock() - contact[1] <= 120:
                index = contact[0]
            if index is not None:
                self.seen.add(key)
                self.last_activity = self.clock()
                self.finish()
                await self.send_text(index, self.local_answer(index, m.text), message=m)
                self.contacts.pop(m.from_user.id, None)
                raise ApplicationHandlerStop
        return await super().observe(update, context)

    def prepare_local(self):
        if self.turn % 3 == 0 or not hasattr(self, "daily_pack"):
            day = datetime.fromtimestamp(self.maintenance.clock(), timezone(timedelta(hours=7))).date().isoformat()
            store = self.maintenance.store
            with store.lock, store.connection:
                row = store.connection.execute("SELECT position FROM mcm_dialog_progress WHERE group_id=? AND day=?",
                    (self.group, day)).fetchone()
                position = row[0] if row else 0
                store.connection.execute("INSERT INTO mcm_dialog_progress VALUES(?,?,?) ON CONFLICT(group_id,day) "
                    "DO UPDATE SET position=excluded.position", (self.group, day, position + 1))
            self.daily_pack = self.choose("dialog", DAILY_DIALOGS)
        self.prepared.extend(((self.turn + offset) % 3, self.daily_pack[(self.turn + offset) % 3])
            for offset in range(min(3 - self.turn % 3, self.max_turns - self.turn)))

    async def send_text(self, index, text, *, message=None, revision=None):
        c = self.controller
        text = re.sub(r"\b(gue|lu)\b", lambda m: (
            {"gue": "aku", "lu": "kau"}[m.group().lower()].capitalize()
            if m.group()[0].isupper() else {"gue": "aku", "lu": "kau"}[m.group().lower()]), text, flags=re.I)
        text = re.sub(r"\ble\b", "abangku" if index == 0 else "bang", text)
        if not vip_text(text):
            text = " ".join(re.split(r"(?<=[.!?])\s+", text)[:2])
        async with c.outgoing_locks.setdefault(self.group, asyncio.Lock()):
            await c.wait_outgoing(self.group)
            if (not self.allowed() or (revision is not None and revision != self.revision)
                    or (message is not None and (self.group, message.message_id) in c.blocked_messages)):
                return False
            bot = self.bots[index] if index < 2 else self.application.bot
            try:
                sent = await bot.send_message(chat_id=self.group, text=text[:350], parse_mode=None,
                    reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True)
                    if message is not None else None)
                self.send_errors.pop(index, None)
                seconds = 60 if vip_text(text, message.text if message is not None else "") else 300
                self.maintenance.expire(bot, self.group, sent.message_id, seconds)
                if message is not None:
                    if len(self.contacts) >= 5000:
                        self.contacts.clear()
                    self.contacts[message.from_user.id] = (index, self.clock())
                    if seconds == 60:
                        self.maintenance.expire(self.application.bot, self.group, message.message_id, 60)
                return True
            except TelegramError as error:
                self.send_errors[index] = type(error).__name__
                logger.warning("Kiriman %s gagal: jenis=%s", NAMES[index], type(error).__name__)
                return False
            finally:
                c.record_wait(self.group, 3.5)
