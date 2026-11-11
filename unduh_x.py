"""Video Twitter/X: balasan pribadi atau kiriman owner ke empat grup VIP."""

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import shutil
from dataclasses import dataclass
from contextlib import ExitStack
from pathlib import Path

from telegram import InputFile, InputMediaVideo, ReplyParameters
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

if __package__:
    from .bot_mcm import RELAY_COMMAND
    from .vip_mcm import VIP_GROUPS
    from .api_lokal import download_limit, local_enabled
else:
    from bot_mcm import RELAY_COMMAND
    from vip_mcm import VIP_GROUPS
    from api_lokal import download_limit, local_enabled

logger = logging.getLogger(__name__)
MAX_BYTES = 45 * 1024 * 1024
URL = re.compile(
    r"(?<![\w./@-])(?:https?://)?(?:(?:www|mobile)\.)?(?:twitter\.com|x\.com)/"
    r"(?P<path>(?:[a-zA-Z0-9_]{1,15}/status/|i/(?:web/)?status/)"
    r"(?P<id>[0-9]{1,25})(?:/video/[1-4])?)(?![\w/])(?:\?[^\s<>]*)?", re.I)
GROUP_COMMAND = re.compile(
    r"download\s+video\s+ini\s+kirim\s+ke\s+(?:(?P<count>4|empat|semua)\s+)?grup"
    r"(?:\s+vip)?(?P<names>(?:\s*:.*|\s+.*)?)", re.I)
CAPTION = re.compile(r"^\s*(?:dengan\s+)?caption\s*:[ \t]?(.*)$", re.I | re.M | re.S)
GROUP_KEYS = ("chil_vip", "indo_premium", "gay_lesby_acak", "full_paket")
CODE = r"(?:INDO|GAY|AI(?:\s+VIDEO)?|I\s*VIP|G\s*VIP|A\s*VIP|A1|I2|A3|MCB)"
CODE_LINE = re.compile(rf"{CODE}(?:\s*(?:,|\+|\bdan\b)\s*{CODE})*", re.I)
CODE_HEAD = re.compile(rf"{CODE}\s*(?:,|\+|\bdan\b)", re.I)
TOPIC_CODES = {
    "topic_a1": ("MCM_X_TOPIC_A1_ID", "A1"),
    "topic_i2": ("MCM_X_TOPIC_I2_ID", "I2"),
    "topic_a3": ("MCM_X_TOPIC_A3_ID", "A3"),
}
TOPIC_DEFAULTS = {
    "topic_a1": (-1004377838078, 14),
    "topic_i2": (-1004377838078, 10),
    "topic_a3": (-1004377838078, 12),
}
GROUP_LABELS = {
    "main_mcb": "MCB UTAMA",
    "chil_vip": "VIDEO AI HD",
    "indo_premium": "INDO PREMIUM",
    "gay_lesby_acak": "GAY/LESBY ACAK",
    "full_paket": "TOKO GEN Z (TAKE ALL)",
    "topic_a1": "TOKO GEN Z / ALL G/L (A1)",
    "topic_i2": "TOKO GEN Z / INDO PREMIUM (I2)",
    "topic_a3": "TOKO GEN Z / AI HD (A3)",
}
GROUP_NAMES = {
    "main_mcb": ("mcb", "mcb utama", "mcb induk", "grup utama", "grup utama mcb", "utama mcb"),
    "chil_vip": ("ai", "ai hd", "video ai", "video ai hd", "a vip", "avip"),
    "indo_premium": ("indo", "indo premium", "i vip", "ivip"),
    "gay_lesby_acak": (
        "gl", "g l", "gay lesby", "gay lesby acak", "gay lesbi", "gay lesbi acak",
        "gay lesbiola", "gay lesbiola acak", "g4y lesbiola", "g4y lesbiola acak",
        "gay", "lesby", "lesbi", "g4y lesby", "g4y lesby acak"),
    "full_paket": ("toko gen z", "toko genz", "tokogenz", "take all", "take all grup", "takeall"),
    "topic_a1": ("a1", "g vip", "gvip"),
    "topic_i2": ("i2",),
    "topic_a3": ("a3",),
}
GENRE_TARGETS = {
    "indo": ("indo_premium", "topic_i2"),
    "gay": ("gay_lesby_acak", "topic_a1"),
    "ai": ("chil_vip", "topic_a3"),
    "ai video": ("chil_vip", "topic_a3"),
}


@dataclass(frozen=True)
class Request:
    url: str
    publish: bool
    caption: str | None
    targets: tuple[str, ...] = ()
    urls: tuple[str, ...] = ()

    @property
    def links(self):
        return self.urls or (self.url,)


def selected_groups(command):
    names = command.group("names").strip().removeprefix(":").strip()
    if command.group("count") and names:
        raise ValueError("Pilih nama grup atau perintah semua grup, le. Jangan digabung dalam satu tujuan.")
    if not names or names.casefold() == "semua":
        return GROUP_KEYS
    return selected_names(names)


def selected_names(names):
    aliases = {name: key for key, values in GROUP_NAMES.items() for name in values}
    targets = []
    for value in re.split(r",|\+|\bdan\b", names, flags=re.I):
        name = re.sub(r"[\s/_-]+", " ", value.casefold()).strip()
        key = aliases.get(name)
        selected = GENRE_TARGETS.get(name, (key,) if key is not None else ())
        if not selected:
            raise ValueError(
                "Tujuan belum dikenali, le. Pakai kode INDO, GAY, AI, atau MCB. "
                "Tulis kode pada baris sebelum caption.")
        for target in selected:
            if target not in targets:
                targets.append(target)
    return tuple(targets)


def resolve_routes(targets, *, main_group_id=None):
    routes = []
    for key in targets:
        if key == "main_mcb":
            if main_group_id is None:
                value = os.environ.get("MCM_TARGET_GROUP_ID", "").strip()
                if not re.fullmatch(r"-[1-9][0-9]*", value):
                    raise ValueError("ID grup utama MCB belum disetel, le.")
                main_group_id = int(value)
            if not isinstance(main_group_id, int) or main_group_id >= 0:
                raise ValueError("ID grup utama MCB belum valid, le.")
            routes.append((GROUP_LABELS[key], main_group_id, None))
            continue
        thread_id = None
        group_key = key
        if key in TOPIC_CODES:
            variable, code = TOPIC_CODES[key]
            value = os.environ.get(variable)
            if value is None:
                expected_chat, default_topic = TOPIC_DEFAULTS[key]
                if VIP_GROUPS.get("full_paket") != expected_chat:
                    raise ValueError(f"Grup TOKO GEN Z berubah, le. Perbarui ID topik {code} di {variable}.")
                value = str(default_topic)
            value = value.strip()
            if not re.fullmatch(r"[1-9][0-9]*", value) or len(value) > 10 or int(value) > 2147483647:
                raise ValueError(f"Topik {code} belum disetel, le. Isi {variable} dengan ID topik yang benar.")
            thread_id = int(value)
            group_key = "full_paket"
        routes.append((GROUP_LABELS[key], VIP_GROUPS[group_key], thread_id))
    return routes


def parse_request(text):
    caption = CAPTION.search(text)
    header = text[:caption.start()] if caption else text
    links = list(URL.finditer(header))
    if not links:
        return None
    urls, seen = [], set()
    for match in links:
        path = match.group("path")
        variant = path.rsplit("/video/", 1)[1] if "/video/" in path else ""
        identity = (match.group("id"), variant or "1")
        if identity not in seen:
            seen.add(identity)
            urls.append("https://x.com/" + path)
    commands = []
    for line in header.splitlines():
        instruction = URL.sub("", line).strip()
        match = GROUP_COMMAND.fullmatch(instruction)
        if match:
            commands.append(selected_groups(match))
        elif CODE_LINE.fullmatch(instruction) or CODE_HEAD.match(instruction):
            commands.append(selected_names(instruction))
    if len(commands) > 1:
        raise ValueError("Pakai satu baris kode tujuan, le. Pisahkan beberapa kode dengan koma.")
    targets = commands[0] if commands else ()
    publish = bool(targets)
    if publish and re.search(r"\b(?:jangan|gausah|gakusah|tidak usah)\b.*\b(?:download|unduh|kirim)\b", header, re.I):
        raise ValueError("Instruksi kirimnya bertentangan, le. Kirim ulang satu perintah yang jelas.")
    value = caption.group(1).rstrip() if caption else None
    if value is not None and len(value.encode("utf-16-le")) // 2 > 1024:
        raise ValueError("Caption terlalu panjang, le. Maksimal 1024 karakter Telegram.")
    return Request(urls[0], publish, value, targets, tuple(urls))


def album_batches(count):
    """Keep input order; split 11 items into 9+2 instead of 10+1."""
    start = 0
    while start < count:
        remaining = count - start
        size = 9 if remaining == 11 else min(10, remaining)
        yield tuple(range(start, start + size))
        start += size


async def telegram_send(method, *, streams=(), **kwargs):
    for attempt in range(4):
        try:
            for stream in streams:
                stream.seek(0)
            return await method(**kwargs)
        except RetryAfter as error:
            if attempt == 3:
                raise
            delay = error.retry_after
            if hasattr(delay, "total_seconds"):
                delay = delay.total_seconds()
            await asyncio.sleep(delay + 1)


class DownloadError(Exception):
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


def x_session_file(folder):
    """Sesi akun milik owner; hanya disimpan sementara selama pengunduhan."""
    names = ("MCM_X_AUTH_TOKEN", "MCM_X_CT0")
    values = [os.environ.get(name, "").strip() for name in names]
    if not any(values):
        return None
    if not all(re.fullmatch(r"[A-Za-z0-9._~%+/:=-]{16,4096}", value) for value in values):
        raise DownloadError(
            "Sesi X belum lengkap atau formatnya salah, le. "
            "Isi MCM_X_AUTH_TOKEN dan MCM_X_CT0 pada konfigurasi bot di server.")
    path = Path(folder) / ".x-session"
    rows = ["# Netscape HTTP Cookie File"]
    for domain in (".x.com", ".twitter.com"):
        for name, value in zip(("auth_token", "ct0"), values):
            rows.append(f"{domain}\tTRUE\t/\tTRUE\t0\t{name}\t{value}")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("\n".join(rows) + "\n")
    return path


def download_failure(stderr, returncode, authenticated, limit_mb, *, stdout=b""):
    """Baca kedua keluaran untuk diagnosis; jangan tampilkan keluaran mentah."""
    detail = (stdout + b"\n" + stderr).decode("utf-8", errors="replace").lower()
    if "no module named yt_dlp" in detail:
        code = "DEPENDENCY"
        message = "Paket yt-dlp belum terpasang. Tambahkan di requirements.txt lalu deploy, le."
    elif "no space left on device" in detail or "disk quota exceeded" in detail or "errno 28" in detail:
        code = "DISK_FULL"
        message = "Ruang penyimpanan unduhan sudah habis, le. Periksa pemakaian disk server dulu."
    elif "larger than max-filesize" in detail or "max-filesize limit" in detail:
        code = "SIZE_LIMIT"
        match = re.search(r"larger than max-filesize\s*\((\d{1,18})\s+bytes", detail)
        size = f" sekitar {int(match.group(1)) / 1048576:.1f} MB" if match else ""
        message = f"Ukuran videonya{size} melewati batas unduhan {limit_mb} MB, le. Durasi bukan patokannya."
    elif any(value in detail for value in (
            "requires authentication", "login required", "sign in to", "not authorized",
            "http error 401", "protected tweet", "log in to")):
        code = "AUTH_REQUIRED"
        message = ("X belum menerima sesi akun ini atau akunnya tidak diberi akses, le. "
                   "Perbarui sesi dari browser dan pastikan akun itu bisa membuka postingannya.") if authenticated else (
                   "Postingan ini meminta login X, le. Sesi akun X belum dipasang di bot.")
    elif (re.search(r"\b(?:http(?:\s+error)?|status(?:\s+code)?)\s*[:=]?\s*429\b", detail)
          or "rate limit" in detail or "too many requests" in detail):
        code = "RATE_LIMIT"
        message = "X sedang membatasi permintaan unduh, le. Tunggu dulu lalu coba lagi."
    elif "requested format is not available" in detail:
        code = "FORMAT"
        message = (f"Belum ada versi MP4 yang cocok dengan batas {limit_mb} MB untuk bot ini, le. "
                   "Versi kecilnya mungkin tidak tersedia atau formatnya belum didukung.")
    elif "http error 403" in detail or "forbidden" in detail:
        code = "ACCESS_DENIED"
        message = "X menolak akses unduhan dari server ini, le. Sesi atau izin akses perlu diperiksa."
    elif "does not exist" in detail or "no video" in detail or "not contain any video" in detail:
        code = "NO_VIDEO"
        message = "Postingannya tidak ditemukan atau tidak memuat video yang bisa diambil, le."
    elif returncode == 101:
        code = "DOWNLOAD_STOPPED"
        message = ("Pengunduh berhenti sebelum menghasilkan file video lengkap, le. "
                   "Kode 101 tidak berarti durasi videonya dilarang; hasil unduhan perlu diperiksa.")
    else:
        code = "EXTRACT_FAILED"
        message = "Pengunduh belum berhasil mengambil video dari X, le. Periksa kode error di log bot."
    logger.warning("Unduhan X gagal: kode=%s; exit=%s; sesi=%s; batas_mb=%s",
                   code, returncode, authenticated, limit_mb)
    return DownloadError(message, code=code)


def format_selector(limit, height=None):
    """Pilih versi utuh yang muat; sisakan margin untuk estimasi bitrate."""
    ceiling = f"[height<={height}]" if height is not None else ""
    filters = (f"[ext=mp4][protocol=https][filesize<=?{limit}]"
               f"[filesize_approx<=?{int(limit * 0.90)}]{ceiling}")
    return f"best{filters}/bestvideo{filters}"


def clear_partial_download(folder):
    """Hanya hapus keluaran percobaan ini, bukan sesi atau cache server Telegram."""
    for path in Path(folder).glob("video.*"):
        if not path.is_symlink() and path.is_file():
            path.unlink()


async def download_attempt(url, folder, limit, session, height, deadline):
    target = Path(folder) / "video.mp4"
    limit_mb = limit // (1024 * 1024)
    args = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-plugin-dirs",
            "--no-cache-dir", "--no-warnings", "--no-progress", "--socket-timeout", "15",
            "--retries", "1", "--fragment-retries", "1", "--playlist-items", "1",
            "--max-downloads", "1", "--max-filesize", str(limit),
            "--format", format_selector(limit, height),
            "--output", str(Path(folder) / "video.%(ext)s")]
    if session is not None:
        args.extend(["--cookies", str(session)])
    args.extend(["--", url])
    environment = dict(os.environ)
    for name in ("MCM_X_AUTH_TOKEN", "MCM_X_CT0"):
        environment.pop(name, None)
    process = reader = None
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=environment)
        reader = asyncio.create_task(process.communicate())
        while not reader.done():
            await asyncio.wait((reader,), timeout=0.5)
            size = 0
            for path in Path(folder).iterdir():
                try:
                    if path != session and path.is_file():
                        size += path.stat().st_size
                except FileNotFoundError:
                    pass
            if size > limit:
                raise DownloadError(f"Videonya lebih dari batas {limit_mb} MB, le.", code="SIZE_LIMIT")
            if shutil.disk_usage(folder).free < 256 * 1024 * 1024:
                raise DownloadError("Ruang unduhan server hampir habis, le. Kosongkan penyimpanan dulu.", code="DISK_FULL")
            if asyncio.get_running_loop().time() >= deadline:
                raise DownloadError("Unduhannya kehabisan waktu, le. Coba lagi nanti.", code="TIMEOUT")
        stdout, stderr = await reader
        if process.returncode not in (0, 101) or not target.is_file() or not target.stat().st_size:
            raise download_failure(stderr, process.returncode, session is not None, limit_mb, stdout=stdout)
        if target.stat().st_size > limit:
            raise DownloadError(f"Videonya lebih dari batas {limit_mb} MB, le.", code="SIZE_LIMIT")
        return target
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        if reader is not None:
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


async def download_video(url, folder):
    """Pilih versi yang muat tanpa memotong durasi atau mengompres file asli."""
    try:
        limit = download_limit()
    except ValueError as error:
        raise DownloadError(str(error)) from None
    session = x_session_file(folder)
    deadline = asyncio.get_running_loop().time() + (1800 if local_enabled() else 120)
    try:
        for height in (None, 720, 480, 360, 240):
            if asyncio.get_running_loop().time() >= deadline:
                raise DownloadError("Unduhannya kehabisan waktu, le. Coba lagi nanti.", code="TIMEOUT")
            try:
                return await download_attempt(url, folder, limit, session, height, deadline)
            except DownloadError as error:
                if error.code != "SIZE_LIMIT":
                    raise
                clear_partial_download(folder)
                if height == 240:
                    raise DownloadError(
                        f"{error} Belum ditemukan versi MP4 cukup kecil yang bisa diambil bot ini.",
                        code="SIZE_LIMIT") from None
                logger.info("Unduhan X mencoba kualitas lebih rendah; batas_mb=%s", limit // 1048576)
    finally:
        if session is not None:
            session.unlink(missing_ok=True)


class UnduhX:
    def __init__(self, controller, *, downloader=download_video):
        self.c = controller
        self.store = controller.vip_store
        self.downloader = downloader
        self.busy = False
        self.active_key = None
        self.tasks = set()
        with self.store.lock, self.store.connection:
            self.store.connection.executescript("""
                CREATE TABLE IF NOT EXISTS mcm_x_jobs (
                    bot_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    mode TEXT NOT NULL, state TEXT NOT NULL,
                    PRIMARY KEY(bot_id,owner_id,message_id));
                CREATE TABLE IF NOT EXISTS mcm_x_targets (
                    bot_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL, state TEXT NOT NULL, sent_message_id INTEGER,
                    PRIMARY KEY(bot_id,owner_id,message_id,target_id));
                CREATE TABLE IF NOT EXISTS mcm_x_routes (
                    bot_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL, thread_id INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL, sent_message_id INTEGER,
                    PRIMARY KEY(bot_id,owner_id,message_id,target_id,thread_id));
                INSERT OR IGNORE INTO mcm_x_routes
                    SELECT bot_id,owner_id,message_id,target_id,0,state,sent_message_id
                    FROM mcm_x_targets;
                CREATE TABLE IF NOT EXISTS mcm_x_batches (
                    bot_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL, thread_id INTEGER NOT NULL, batch_index INTEGER NOT NULL,
                    state TEXT NOT NULL, sent_message_ids TEXT,
                    PRIMARY KEY(bot_id,owner_id,message_id,target_id,thread_id,batch_index));
            """)

    def state(self, key, state):
        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "UPDATE mcm_x_jobs SET state=? WHERE bot_id=? AND owner_id=? AND message_id=?", (state, *key))

    async def owner_text(self, update, context):
        m = update.effective_message
        if (m is None or update.edited_message is not None or m.chat.type != "private"
                or m.from_user is None or m.from_user.is_bot
                or m.from_user.id != self.c.settings.owner_id or m.chat.id != m.from_user.id):
            return
        text = m.text or ""
        if (RELAY_COMMAND.match(text) or self.c.qris_setup(context.bot.id)
                or re.search(r"\b(?:jangan|gausah|gakusah|tidak usah)\b.*\b(?:download|unduh)\b", text, re.I)):
            return await self.c.private_text(update, context)
        try:
            request = parse_request(text)
        except ValueError as error:
            await m.reply_text(str(error), parse_mode=None)
            return
        if request is None:
            return await self.c.private_text(update, context)
        key = (context.bot.id, m.from_user.id, m.message_id)
        with self.store.lock:
            previous = self.store.connection.execute(
                "SELECT state FROM mcm_x_jobs WHERE bot_id=? AND owner_id=? AND message_id=?", key).fetchone()
        if previous:
            await m.reply_text("Pesan unduh ini sudah dicatat, le. Kalau hasil kirimnya belum pasti, cek chat tujuan dulu; bot tidak mengirim ulang otomatis.")
            return
        if self.busy:
            await m.reply_text("Masih ada kiriman video yang sedang diproses, le. Tunggu selesai lalu kirim pesan link berikutnya ya.")
            return
        try:
            groups = resolve_routes(request.targets, main_group_id=self.c.settings.target_group_id) if request.publish else [("Chat pribadi", m.chat.id, None)]
        except ValueError as error:
            await m.reply_text(str(error), parse_mode=None)
            return
        except KeyError:
            await m.reply_text("Konfigurasi grup tujuan belum lengkap, le.")
            return
        if request.publish and (not groups or len({(group, topic) for _, group, topic in groups}) != len(groups)
                                or any(not isinstance(group, int) or group >= 0 for _, group, _ in groups)
                                or any(group == self.c.settings.target_group_id and
                                       (name != GROUP_LABELS["main_mcb"] or "main_mcb" not in request.targets)
                                       for name, group, _ in groups)):
            await m.reply_text("Periksa ID grup tujuan dulu, le. Setiap tujuan harus berbeda; grup MCB utama hanya boleh lewat kode MCB.")
            return
        with self.store.lock, self.store.connection:
            inserted = self.store.connection.execute(
                "INSERT OR IGNORE INTO mcm_x_jobs VALUES(?,?,?,?,?)",
                (*key, "vip" if request.publish else "private", "downloading")).rowcount
        if not inserted:
            return
        self.busy = True
        self.active_key = key
        task = asyncio.create_task(self.run(m, context.bot, request, groups, key))
        self.tasks.add(task)
        task.add_done_callback(self.finished)

    def finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Unduhan X tertunda: jenis=%s", type(task.exception()).__name__)

    async def run(self, message, bot, request, groups, key):
        try:
            try:
                text = ("Bentar le, videonya aku ambil dulu. Aku pilih kualitas yang muat batas ukuran."
                        if len(request.links) == 1 else
                        f"Bentar le, aku unduh {len(request.links)} video sesuai urutan link. Semua unduhan disiapkan dulu sebelum album dikirim.")
                if request.publish:
                    text += "\nTujuan: " + ", ".join(name for name, _, _ in groups)
                await message.reply_text(text, parse_mode=None)
            except TelegramError:
                pass
            with tempfile.TemporaryDirectory(prefix="mcm-x-") as folder:
                paths = []
                for index, url in enumerate(request.links, 1):
                    item_folder = Path(folder) / str(index)
                    item_folder.mkdir()
                    if shutil.disk_usage(folder).free < 256 * 1024 * 1024:
                        raise DownloadError("Ruang server hampir habis, le. Video belum dikirim; kosongkan penyimpanan dulu.", code="DISK_FULL")
                    try:
                        paths.append(await self.downloader(url, str(item_folder)))
                    except DownloadError as error:
                        if len(request.links) == 1:
                            raise
                        raise DownloadError(f"Link {index}/{len(request.links)} gagal: {error} Belum ada video yang dikirim.", code=error.code) from None
                results, file_ids = [], [None] * len(paths)
                self.state(key, "sending")
                for name, group_id, thread_id in groups:
                    with self.store.lock, self.store.connection:
                        claimed = self.store.connection.execute(
                            "INSERT OR IGNORE INTO mcm_x_routes VALUES(?,?,?,?,?,?,NULL)",
                            (*key, group_id, thread_id or 0, "sending")).rowcount
                    if not claimed:
                        continue
                    batch_number = 0
                    batch_claimed = False
                    try:
                        for batch_number, indices in enumerate(album_batches(len(paths)), 1):
                            with self.store.lock, self.store.connection:
                                batch_claimed = bool(self.store.connection.execute(
                                    "INSERT OR IGNORE INTO mcm_x_batches VALUES(?,?,?,?,?,?,?,NULL)",
                                    (*key, group_id, thread_id or 0, batch_number, "sending")).rowcount)
                            if not batch_claimed:
                                raise NetworkError("Album pernah dicatat; hasil kirim perlu diperiksa.")
                            sent = await self.send_batch(bot, message, request, group_id, thread_id,
                                                         paths, file_ids, indices)
                            if len(sent) != len(indices) or any(not isinstance(getattr(item, "message_id", None), int) for item in sent):
                                raise NetworkError("Telegram belum memberikan hasil kirim lengkap.")
                            for index, item in zip(indices, sent):
                                if getattr(item, "video", None):
                                    file_ids[index] = item.video.file_id
                            with self.store.lock, self.store.connection:
                                self.store.connection.execute(
                                    "UPDATE mcm_x_batches SET state='sent',sent_message_ids=? WHERE bot_id=? "
                                    "AND owner_id=? AND message_id=? AND target_id=? AND thread_id=? AND batch_index=?",
                                    (json.dumps([item.message_id for item in sent]), *key, group_id, thread_id or 0, batch_number))
                                self.store.connection.execute(
                                    "UPDATE mcm_x_routes SET sent_message_id=COALESCE(sent_message_id,?) "
                                    "WHERE bot_id=? AND owner_id=? AND message_id=? AND target_id=? AND thread_id=?",
                                    (sent[0].message_id, *key, group_id, thread_id or 0))
                        with self.store.lock, self.store.connection:
                            self.store.connection.execute("UPDATE mcm_x_routes SET state='sent' "
                                "WHERE bot_id=? AND owner_id=? AND message_id=? AND target_id=? AND thread_id=?",
                                (*key, group_id, thread_id or 0))
                        results.append(f"✅ {name}" + (f": {len(paths)} video, {batch_number} album" if len(paths) > 1 else ""))
                    except TelegramError as error:
                        state = "uncertain" if isinstance(error, NetworkError) and not isinstance(error, BadRequest) else "failed"
                        with self.store.lock, self.store.connection:
                            if batch_claimed:
                                self.store.connection.execute(
                                    "UPDATE mcm_x_batches SET state=? WHERE bot_id=? AND owner_id=? AND message_id=? "
                                    "AND target_id=? AND thread_id=? AND batch_index=?",
                                    (state, *key, group_id, thread_id or 0, batch_number))
                            self.store.connection.execute("UPDATE mcm_x_routes SET state=? "
                                "WHERE bot_id=? AND owner_id=? AND message_id=? AND target_id=? AND thread_id=?",
                                (state, *key, group_id, thread_id or 0))
                        place = "grup tujuan" if request.publish else "chat ini"
                        part = f"album {batch_number}: " if len(paths) > 1 else ""
                        results.append(f"⚠️ {name}: {part}" + (f"hasil kirim belum pasti; cek {place} dulu" if state == "uncertain" else "Telegram menolak kiriman; periksa izin bot"))
                        logger.warning("Kiriman Twitter/X tertunda: jenis=%s", type(error).__name__)
                self.state(key, "finished")
                if request.publish or any(result.startswith("⚠️") for result in results):
                    title = ("Hasil kirim ke grup:\n" if "main_mcb" in request.targets else
                             "Hasil kirim ke grup VIP:\n") if request.publish else "Hasil kirim video:\n"
                    await message.reply_text(title + "\n".join(results), parse_mode=None)
        except asyncio.CancelledError:
            self.state(key, "interrupted")
            raise
        except (DownloadError, OSError) as error:
            self.state(key, "failed")
            await message.reply_text(str(error) if isinstance(error, DownloadError)
                                     else "Unduhan belum berhasil diproses di server, le. Coba lagi nanti.", parse_mode=None)
        except TelegramError as error:
            logger.warning("Status unduhan belum terkirim: jenis=%s", type(error).__name__)
        finally:
            self.busy = False
            self.active_key = None

    async def send_batch(self, bot, message, request, group_id, thread_id, paths, file_ids, indices):
        is_local = bool(getattr(bot, "local_mode", False))
        timeout = 1800 if is_local and any(file_ids[index] is None for index in indices) else 120
        kwargs = dict(chat_id=group_id, write_timeout=timeout, read_timeout=timeout,
                      connect_timeout=20, pool_timeout=10)
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        if not request.publish:
            kwargs["reply_parameters"] = ReplyParameters(
                message_id=message.message_id, allow_sending_without_reply=True)
        with ExitStack() as files:
            streams = []
            def media(index):
                if file_ids[index] is not None:
                    return file_ids[index]
                if is_local:
                    return Path(paths[index]).resolve().as_uri()
                stream = files.enter_context(Path(paths[index]).open("rb"))
                streams.append(stream)
                return InputFile(stream, filename=f"video-{index + 1}.mp4", read_file_handle=False)
            if len(indices) == 1:
                index = indices[0]
                video = file_ids[index]
                if video is None:
                    video = Path(paths[index]).resolve() if is_local else media(index)
                sent = await telegram_send(bot.send_video, streams=streams, video=video, caption=request.caption,
                                           parse_mode=None, supports_streaming=True, **kwargs)
                return (sent,)
            album = [InputMediaVideo(media(index), caption=request.caption if offset == 0 else None,
                                     parse_mode=None, supports_streaming=True)
                     for offset, index in enumerate(indices)]
            return tuple(await telegram_send(bot.send_media_group, streams=streams, media=album, **kwargs))

    async def stop(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        if self.active_key is not None:
            self.state(self.active_key, "interrupted")
            self.active_key = None
        self.busy = False
