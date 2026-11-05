"""Adit dan Dennis: satu pengatur, tanpa polling tambahan atau pesan berulang."""

import asyncio
import json
import logging
import os
import random
import re
from collections import deque

import httpx
from telegram import Bot, ReplyParameters
from telegram.error import BadRequest, InvalidToken, NetworkError, TelegramError
from telegram.ext import ApplicationHandlerStop
from telegram.request import HTTPXRequest

if __package__:
    from .bot_mcm import LINK_PATTERN, addressed_question, child_content_request, declines_bot_reply, question_like
else:
    from bot_mcm import LINK_PATTERN, addressed_question, child_content_request, declines_bot_reply, question_like

logger = logging.getLogger(__name__)
BT_TEXT = "BT apa sih bang? 😂 Aku bete sama kamu 🎶"
ACTORS = (
    ("Adit", "aditsapo_bot", "MCM_ORBIT_BOT_TOKEN"),
    ("Dennis", "denisjarwo_bot", "MCM_BAPER_BOT_TOKEN"),
)
NAMES = ("Adit", "Dennis", "Emak")
ALIASES = (re.compile(r"\b(?:adit+|dit+)\b", re.I),
           re.compile(r"\b(?:den{1,2}is+|nis+)\b", re.I))
VIP_INVITE = "Kalau mau lihat pilihan grup VIP, chat pribadi Emak terus ketik /vip, le 😄"
LOCAL_REPLIES = (
    (
        "Ada apa, le? Ceritain dulu biar gue ngerti.",
        "Iya, gue di sini. Bagian mana yang mau dibantu?",
        "Siap, Dit-nya hadir 😄 Lu butuh bantuan apa?",
        "Denger kok, le. Langsung aja, lagi kenapa?",
        "Pelan-pelan, gue simak. Mau nanya apa?",
        "Ada gue, le. Coba jelasin masalahnya dulu.",
        "Oke, mulai dari yang paling bikin lu bingung aja.",
        "Gue nyaut nih. Mau bahas yang serius apa yang receh?",
    ),
    (
        "Hadir, le! Gue dipanggil apa disuruh bayar parkir? 😂",
        "Iyaaa, nis-nya nyaut. Ada apa nih?",
        "Gue di sini, le. Jangan nyariin sampai lapor RT 😂",
        "Siap! Tapi kalau suruh lari, gue bagian nyemangatin aja.",
        "Nyaut dulu sebelum dikira sinyal gue tinggal satu bar 😂",
        "Ada apa, le? Ceritain, gue lagi ga pura-pura sibuk.",
        "Hadir bang. Panggilan masuk, saldo tetap ga masuk-masuk 😂",
        "Woi juga! Ada masalah apa cuma lagi tes speaker? 😂",
    ),
    (
        "Emak denger, le. Lagi bahas apa nih?",
        "Ada apa le? Ceritain dulu, Emak simak.",
        "Nyaut dulu nih. Ada yang mau ditanyain? 😄",
    ),
)
LOCAL_UNKNOWN = (
    (
        "Yang itu belum ada jawaban pastinya di stok gue, le. Coba tanyain Emak pakai kata mak ya.",
        "Gue belum tahu pasti soal itu. Daripada asal jawab, coba tanyain Emak juga, le.",
        "Belum bisa gue pastiin yang itu, le. Kasih konteks lebih lengkap atau tanya Emak ya.",
    ),
    (
        "Yang itu gue belum tahu, le. Daripada ngarang sampai masuk buku dongeng, coba tanya Emak 😂",
        "Waduh, stok jawaban gue belum sampai situ. Panggil mak juga ya, le.",
        "Gue ga mau sok tahu yang ini, le. Coba tanyain Emak; bagian bikin ribut biar gue aja 😂",
    ),
    ("Emak belum punya jawaban lokal buat itu, le. Tanya lagi pakai mak supaya masuk jalur AI ya.",),
)
LOCAL_FACTS = (
    (r"\b(langit.*biru|biru.*langit)\b", "Langit terlihat biru karena molekul udara lebih kuat menghamburkan cahaya biru daripada merah dari sinar Matahari, le."),
    (r"\b(kenapa|kok|gimana|bagaimana)\b.*\bhujan\b", "Hujan terjadi saat uap air mengembun jadi butiran di awan, lalu butirannya cukup besar untuk jatuh, le."),
    (r"\b(kenapa|kok|mengapa)\b.*\b(mars|planet merah)\b", "Mars tampak kemerahan karena banyak mineral besi yang teroksidasi di permukaannya, mirip karat, le."),
    (r"\b(gravitasi|kenapa.*jatuh)\b", "Gravitasi itu gaya tarik antarbenda bermassa, le. Tarikan Bumi bikin kita tetap menapak dan benda jatuh ke tanah."),
    (r"\b(kenapa|kok|mengapa)\b.*\bbulan\b.*\b(terang|bersinar|bercahaya)\b", "Bulan ga bikin cahaya sendiri, le. Yang kelihatan terang itu sinar Matahari yang dipantulkan permukaannya."),
    (r"\b(kenapa|kok|mengapa)\b.*\b(kucing|meong)\b", "Kucing bisa mengeong buat berkomunikasi dengan manusia, le. Tapi maksudnya tergantung situasi: minta makan, perhatian, atau ada yang bikin ga nyaman."),
    (r"\b(kenapa|kok|mengapa)\b.*\b(mimpi|bermimpi)\b", "Mimpi berkaitan dengan aktivitas otak saat tidur, le. Peneliti masih mempelajari fungsinya; salah satu dugaan terkait pengolahan ingatan dan emosi."),
    (r"\b(apa itu|jelasin|jelaskan)\b.*\b(ai|kecerdasan buatan)\b", "AI itu sistem komputer yang dilatih untuk tugas seperti mengenali pola atau menghasilkan teks, le. Hasilnya tetap bisa salah, jadi fakta penting perlu dicek."),
    (r"\b(qris|vip|paket\s+(?:vip|grup)|bayar\s+(?:vip|grup)|pembayaran\s+vip|harga\s+(?:vip|grup))\b", "Buat lihat pilihan dan harga VIP yang berlaku, chat pribadi Emak terus ketik /vip, le. Bukti pembayaran tetap dicek owner dulu."),
    (r"\b(link cadangan|grup cadangan)\b", "Link cadangan bisa diminta lewat chat pribadi Emak, le. Ketik /start lalu pilih Link cadangan grup."),
    (r"\b(minta|kirim|ambil)\b.*\bvideo\b", "Kalau mau minta video yang terdaftar, chat pribadi Emak lalu pilih Minta video, le. Siapkan link pesan videonya dari grup."),
    (r"\b(berita terbaru|harga.*hari ini|cuaca.*hari ini|skor.*(?:sekarang|tadi))\b", "Gue ga punya info langsung buat yang terbaru itu, le. Cek sumber terkini dulu biar ga ketuker kabar lama."),
    (r"\b(patah hati|putus cinta|ditinggal pacar)\b", "Wajar kalau lagi berat, le. Kasih diri lu waktu, ngobrol sama orang yang dipercaya, dan jangan maksa semuanya langsung beres."),
)
LOCAL_DIALOGS = (
    ("Den, bayangin alien ikut rapat RT. Hal pertama yang ditanyain kira-kira apa?", "Pasti nanya WiFi sama passwordnya. Jauh-jauh lintas galaksi, kuota tetap habis 😂", "Belum juga duduk, udah Emak kasih formulir iuran kebersihan, le."),
    ("Kalau kucing jadi satpam, gerbangnya aman ga ya?", "Aman dari manusia. Kalau yang lewat kardus kosong, langsung dia ikutin 😂", "Yang maling ikan malah petugasnya sendiri nanti, le."),
    ("Gue kepikiran buka warung di Bulan. Saingannya belum banyak.", "Dit, ongkir sambelnya bisa lebih mahal dari warungnya 😂", "Survei pembeli dulu, le. Jangan bangun warung yang ngantri cuma batu."),
    ("Robot kalau disuruh belanja ke pasar bakal jago nawar ga ya?", "Baru bilang harga terakhir, langsung disuruh update sistem 😂", "Robotnya boleh pinter, tapi pedagang udah hafal seribu alasan orang nawar, le."),
    ("Kenapa ya antrean sebelah selalu kelihatan lebih cepat?", "Karena pas gue pindah, gue bawa aura lambatnya ke sebelah 😂", "Makanya pilih antrean, bukan pilih drama baru, le."),
    ("Kalau dinosaurus masih ada sekarang, tempat parkirnya gimana?", "Yang T-rex pasti protes. Tiket parkir ditaruh di kaca, dia ga bisa ambil 😂", "Bayangin emak nyuruh minggir gara-gara dia nutupin warung."),
    ("Kadang ide bagus malah muncul pas mandi, ya.", "Makanya gue bawa buku. Tapi sekali nyatet, tulisannya ikut keramas 😂", "Selesai mandi dulu baru dicatat, le. Bukan semua ide harus basah juga."),
    ("Kalau kulkas bisa ngomong, rahasia rumah bakal bocor semua.", "Dia bakal bilang: yang diet siapa, yang buka pintu jam dua pagi siapa 😂", "Kulkas Emak cukup bunyi kalau pintunya lupa ditutup. Sisanya biar Emak yang ngomel."),
    ("Nabung itu paling susah pas ada tulisan diskon gede.", "Gue kuat kok, Dit. Kuat masukin semua ke keranjang 😂", "Diskon seratus ribu juga tetap keluar uang kalau barangnya ga lu butuhin, le."),
    ("Dulu main petak umpet bisa sampai lupa waktu.", "Gue pernah ngumpet lama banget. Ternyata yang lain udah pulang makan 😂", "Makanya sebelum ngumpet pastikan temennya masih niat nyari, le."),
    ("Kenapa lagu tertentu bisa bikin keinget masa lalu, ya?", "Kalau gue bunyi motor tukang paket juga bikin inget. Inget cicilan belanja 😂", "Memori lu isinya resi semua ternyata, le."),
    ("Pelayanan publik yang jelas itu enak. Orang ga harus nebak mesti ke loket mana.", "Gue pengen loket khusus orang yang salah loket. Biar salahnya tetep dilayani 😂", "Yang penting petunjuknya gampang dibaca dan orangnya dibantu, le."),
)
TOPICS = (
    "planet, bintang dan perjalanan ke Mars", "laut dalam dan makhluk unik",
    "kenapa manusia bermimpi", "sejarah penemuan yang mengubah keseharian",
    "kebiasaan manusia yang lucu", "salah paham dalam percintaan nonseksual",
    "persahabatan dan teman yang suka ngaret", "nostalgia sekolah dan masa kecil",
    "drama tetangga fiktif", "perbedaan kebiasaan antar generasi",
    "makanan daerah dan debat selera", "masak, dapur dan resep yang gagal",
    "olahraga dan logika fans", "musik dan kenangan lagu, tanpa mengutip lirik",
    "film dan tebakan alur cerita", "game dan kebiasaan pemain",
    "AI, robot dan masa depan pekerjaan", "gawai dan teknologi sehari-hari",
    "belanja online dan paket nyasar", "pekerjaan, tanggal gajian dan hemat uang",
    "hewan peliharaan dan kelakuannya", "cuaca, musim dan kebiasaan orang",
    "bahasa, salah dengar dan perbedaan ungkapan", "budaya dan tradisi dari berbagai negara",
    "jalan-jalan dan rencana liburan hemat", "kendaraan dan angkutan umum",
    "fakta sains sederhana", "arkeologi dan kehidupan zaman dahulu",
    "kebun, tanaman dan lingkungan", "pertanyaan andaikan yang lucu dan masuk akal",
    "politik sehari-hari dan layanan publik, tanpa kampanye atau ajakan memilih",
    "andaikan alien ikut rapat RT dan harus bayar iuran",
    "andaikan kucing jadi satpam kompleks",
    "andaikan kulkas bisa mengadu soal penghuni rumah",
    "kenapa ide bagus sering muncul saat mandi",
    "kejadian salah kirim chat yang sepenuhnya fiktif",
    "benda sehari-hari yang desainnya bikin heran",
    "andaikan dinosaurus hidup di kota sekarang",
    "mitos sehari-hari dan penjelasan sainsnya",
    "andaikan robot harus menawar harga di pasar",
    "rencana absurd membuka warung di bulan",
    "kenapa antrean sebelah selalu terasa lebih cepat",
