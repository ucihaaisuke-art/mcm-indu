"""Video Twitter/X: balasan pribadi atau kiriman owner ke empat grup VIP."""

import asyncio
import logging
import re
import sys
import tempfile
import shutil
from dataclasses import dataclass
from pathlib import Path

from telegram import InputFile, ReplyParameters
from telegram.error import BadRequest, NetworkError, TelegramError

if __package__:
    from .bot_mcm import RELAY_COMMAND
    from .vip_mcm import PLANS, VIP_GROUPS
    from .api_lokal import download_limit, local_enabled
else:
    from bot_mcm import RELAY_COMMAND
    from vip_mcm import PLANS, VIP_GROUPS
    from api_lokal import download_limit, local_enabled

logger = logging.getLogger(__name__)
MAX_BYTES = 45 * 1024 * 1024
URL = re.compile(
    r"(?<![\w./@-])(?:https?://)?(?:(?:www|mobile)\.)?(?:twitter\.com|x\.com)/"
    r"(?P<path>(?:[a-zA-Z0-9_]{1,15}/status/|i/(?:web/)?status/)"
    r"(?P<id>[0-9]{1,25})(?:/video/[1-4])?)(?![\w/])(?:\?[^\s<>]*)?", re.I)
GROUP_COMMAND = re.compile(
    r"download\s+video\s+ini\s+kirim\s+ke\s+(?:(?:4|empat)\s+)?grup(?:\s+vip)?\s*:?", re.I)
CAPTION = re.compile(r"^\s*(?:dengan\s+)?caption\s*:[ \t]?(.*)$", re.I | re.M | re.S)
GROUP_KEYS = ("chil_vip", "indo_premium", "gay_lesby_acak", "full_paket")


@dataclass(frozen=True)
class Request:
    url: str
    publish: bool
    caption: str | None


def parse_request(text):
    caption = CAPTION.search(text)
    header = text[:caption.start()] if caption else text
    links = list(URL.finditer(header))
    if not links:
        return None
    if len({match.group("id") for match in links}) != 1:
        raise ValueError("Kirim satu link unggahan Twitter/X per pesan, le.")
    publish = any(GROUP_COMMAND.fullmatch(URL.sub("", line).strip()) for line in header.splitlines())
    if publish and re.search(r"\b(?:jangan|gausah|gakusah|tidak usah)\b.*\b(?:download|unduh|kirim)\b", header, re.I):
        raise ValueError("Instruksi kirimnya bertentangan, le. Kirim ulang satu perintah yang jelas.")
    value = caption.group(1).rstrip() if caption else None
    if value is not None and len(value.encode("utf-16-le")) // 2 > 1024:
        raise ValueError("Caption terlalu panjang, le. Maksimal 1024 karakter Telegram.")
    return Request("https://x.com/" + links[0].group("path"), publish, value)


class DownloadError(Exception):
    pass


async def download_video(url, folder):
    """Subproses dibatasi waktu/ukuran dan dihentikan sebelum file sementara dibuang."""
    target = Path(folder) / "video.mp4"
    try:
        limit = download_limit()
    except ValueError as error:
        raise DownloadError(str(error)) from None
    limit_mb = limit // (1024 * 1024)
    args = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-plugin-dirs",
            "--no-cache-dir", "--no-warnings", "--no-progress", "--socket-timeout", "15",
            "--retries", "1", "--fragment-retries", "1", "--playlist-items", "1",
            "--max-downloads", "1", "--max-filesize", str(limit),
            "--format", f"best[ext=mp4][protocol=https][filesize<=?{limit}]/bestvideo[ext=mp4][protocol=https][filesize<=?{limit}]",
            "--output", str(Path(folder) / "video.%(ext)s"), "--", url]
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    reader = asyncio.create_task(process.communicate())
    deadline = asyncio.get_running_loop().time() + (1800 if local_enabled() else 120)
    try:
        while not reader.done():
            await asyncio.wait((reader,), timeout=0.5)
            size = 0
            for path in Path(folder).iterdir():
                try:
                    if path.is_file():
                        size += path.stat().st_size
                except FileNotFoundError:
                    pass
            if size > limit:
                raise DownloadError(f"Videonya lebih dari batas {limit_mb} MB, le. Coba unggahan yang lebih kecil.")
            if shutil.disk_usage(folder).free < 20 * 1024 * 1024:
                raise DownloadError("Ruang unduhan server hampir habis, le. Kosongkan penyimpanan dulu.")
            if asyncio.get_running_loop().time() >= deadline:
                raise DownloadError("Unduhannya kehabisan waktu, le. Coba lagi nanti.")
        _, stderr = await reader
        if b"No module named yt_dlp" in stderr:
            raise DownloadError("Paket yt-dlp belum terpasang. Tambahkan di requirements.txt lalu deploy, le.")
        if process.returncode not in (0, 101) or not target.is_file() or not target.stat().st_size:
            raise DownloadError("Video belum bisa diambil, le. Pastikan link berisi video publik; unggahan yang meminta login atau dibatasi X bisa gagal.")
        if target.stat().st_size > limit:
            raise DownloadError(f"Videonya lebih dari batas {limit_mb} MB, le.")
        return target
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


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
            await m.reply_text("Masih ada satu video yang sedang diproses, le. Tunggu selesai lalu kirim link berikutnya ya.")
            return
        try:
            groups = [(PLANS[name].name, VIP_GROUPS[name]) for name in GROUP_KEYS] if request.publish else [("Chat pribadi", m.chat.id)]
        except KeyError:
            await m.reply_text("Konfigurasi empat paket/grup VIP belum lengkap, le.")
            return
        if request.publish and (len({group for _, group in groups}) != 4
                                or self.c.settings.target_group_id in {group for _, group in groups}):
            await m.reply_text("Periksa konfigurasi empat grup VIP dulu, le. Tujuannya harus berbeda dari grup MCB utama.")
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
                await message.reply_text("Bentar le, videonya aku ambil dulu.", parse_mode=None)
            except TelegramError:
                pass
            with tempfile.TemporaryDirectory(prefix="mcm-x-") as folder:
                path = await self.downloader(request.url, folder)
                results, file_id = [], None
                self.state(key, "sending")
                for name, group_id in groups:
                    with self.store.lock, self.store.connection:
                        claimed = self.store.connection.execute(
                            "INSERT OR IGNORE INTO mcm_x_targets VALUES(?,?,?,?,?,NULL)",
                            (*key, group_id, "sending")).rowcount
                    if not claimed:
                        continue
                    try:
                        is_local = bool(getattr(bot, "local_mode", False))
                        timeout = 1800 if is_local and file_id is None else 120
                        kwargs = dict(chat_id=group_id, caption=request.caption, parse_mode=None,
                                      supports_streaming=True, write_timeout=timeout, read_timeout=timeout,
                                      connect_timeout=20, pool_timeout=10)
                        if not request.publish:
                            kwargs["reply_parameters"] = ReplyParameters(
                                message_id=message.message_id, allow_sending_without_reply=True)
                        if file_id is None:
                            if is_local:
                                sent = await bot.send_video(video=path.resolve(), **kwargs)
                            else:
                                with path.open("rb") as video:
                                    sent = await bot.send_video(video=InputFile(video, filename="video.mp4", read_file_handle=False), **kwargs)
                        else:
                            sent = await bot.send_video(video=file_id, **kwargs)
                        if getattr(sent, "video", None):
                            file_id = sent.video.file_id
                        with self.store.lock, self.store.connection:
                            self.store.connection.execute("UPDATE mcm_x_targets SET state='sent',sent_message_id=? "
                                "WHERE bot_id=? AND owner_id=? AND message_id=? AND target_id=?",
                                (sent.message_id, *key, group_id))
                        results.append(f"✅ {name}")
                    except TelegramError as error:
                        state = "uncertain" if isinstance(error, NetworkError) and not isinstance(error, BadRequest) else "failed"
                        with self.store.lock, self.store.connection:
                            self.store.connection.execute("UPDATE mcm_x_targets SET state=? "
                                "WHERE bot_id=? AND owner_id=? AND message_id=? AND target_id=?", (state, *key, group_id))
                        place = "grup tujuan" if request.publish else "chat ini"
                        results.append(f"⚠️ {name}: " + (f"hasil kirim belum pasti; cek {place} dulu" if state == "uncertain" else "Telegram menolak kiriman; periksa izin bot"))
                        logger.warning("Kiriman Twitter/X tertunda: jenis=%s", type(error).__name__)
                self.state(key, "finished")
                if request.publish or any(result.startswith("⚠️") for result in results):
                    title = "Hasil kirim ke grup VIP:\n" if request.publish else "Hasil kirim video:\n"
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
