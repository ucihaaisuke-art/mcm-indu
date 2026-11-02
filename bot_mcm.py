import asyncio
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

logger = logging.getLogger("mcm_induk")

WELCOME = "Hai bro , welcome ya"

BACKUP_TEXT = (
    "Ini link kita. Ketika grup kena banned, kamu bisa dapat akses grup baru "
    "dari sini. Jangan lupa save botnya ya."
)

LINK_PATTERN = re.compile(
    r"(?:https?://|tg://|www\.|(?:t|telegram)\.me/)\S+|"
    r"(?<![\w@.])(?:[a-z0-9-]+\.)+"
    r"(?:com|net|org|info|biz|id|site|xyz|online|app|co|io|me|gg|tv|link|vip)"
    r"(?=[/\s?#.,!):;]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Settings:
    backup_url: str = "https://anggur.site/makcombackinduk"
    chatter_enabled: bool = True
    cooldown: int = 120
    chance: float = 0.08

    @classmethod
    def from_env(cls):
        url = os.environ.get("MCM_BACKUP_URL", cls.backup_url).strip()

        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and parsed.port in (None, 443)
                and not any(char.isspace() for char in url)
            )
            cooldown = int(
                os.environ.get("MCM_NIMBRUNG_JEDA", "120")
            )
            chance = float(
                os.environ.get("MCM_NIMBRUNG_PELUANG", "0.08")
            )

        except ValueError:
            raise SystemExit(
                "Pengaturan URL/jeda/peluang MCM tidak valid."
            ) from None

        if (
            not valid
            or not 30 <= cooldown <= 3600
            or not 0 <= chance <= 1
        ):
            raise SystemExit(
                "Periksa URL HTTPS, jeda 30–3600, dan peluang 0–1."
            )

        enabled = os.environ.get(
            "MCM_NIMBRUNG", "on"
        ).lower() in ("on", "true", "1")

        return cls(url, enabled, cooldown, chance)


def contains_link(message):
    entities = (
        tuple(message.entities or ())
        + tuple(message.caption_entities or ())
    )

    for entity in entities:
        if entity.type in (
            MessageEntity.URL,
            MessageEntity.TEXT_LINK,
        ):
            return True

    return bool(
        LINK_PATTERN.search(
            message.text or message.caption or ""
        )
    )


def forwarded_media(message):
    origin = message.forward_origin

    if origin is None or message.is_automatic_forward:
        return False

    source = (
        getattr(origin, "sender_chat", None)
        or getattr(origin, "chat", None)
    )

    if source is not None and source.id == message.chat.id:
        return False

    document = message.document

    file_media = document and (
        (document.mime_type or "").startswith(
            ("image/", "video/")
        )
        or (document.file_name or "").lower().endswith(
            (
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".gif",
                ".mp4",
                ".mov",
                ".mkv",
                ".webm",
            )
        )
    )

    return bool(
        message.photo
        or message.video
        or message.animation
        or message.video_note
        or file_media
    )


def simple_reply(text):
    lower = text.lower()

    if re.search(
        r"\b(meninggal|berduka|kecelakaan|bunuh diri|sakit parah)\b",
        lower,
    ):
        return None

    if re.search(r"\b(horor|horror)\b", lower):
        return (
            "Tim horor nih? Lebih suka suasana bikin merinding "
            "atau jumpscare, bro? 👀"
        )

    if re.search(
        r"\b(film|movie|nonton|episode|serial|series|spoiler)\b",
        lower,
    ):
        return random.choice((
            "Lagi bahas tontonan apa, bro? Kasih judulnya "
            "biar yang lain bisa ikut nimbrung 🎬",
            "Boleh kasih pendapat soal filmnya, "
            "tapi tahan spoiler dulu ya, bro 😄",
        ))

    if re.search(
        r"\b(wkwk\w*|haha\w*|hehe\w*)\b",
        lower,
    ):
        return random.choice((
            "Wkwk, ada aja bro 😂",
            "Obrolan kalian bikin rame suasana 😄",
        ))

    if re.search(
        r"\b(pagi|siang|sore|malam|halo|hallo|assalamualaikum)\b",
        lower,
    ):
        return (
            "Halo bro! Semoga harinya asik. "
            "Lagi pada bahas apa nih? 👋"
        )

    if re.search(
        r"\b(makasih|terimakasih|terima kasih|thanks)\b",
        lower,
    ):
        return (
            "Sama-sama bro, saling bantu "
            "biar grupnya makin enak 😄"
        )

    return None


class CommunityBot:
    def __init__(self, settings, *, clock=time.monotonic):
        self.settings = settings
        self.clock = clock
        self.admin_cache = {}
        self.welcomed = {}
        self.last_reply = {}
        self.chatter_overrides = {}

    def private_button(self, bot):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "Simpan bot / Link cadangan",
                url=f"https://t.me/{bot.username}?start=backup",
            )
        ]])

    async def admin_status(self, message, context):
        if message.sender_chat is not None:
            return message.sender_chat.id == message.chat.id

        user = message.from_user
        if user is None:
            return False

        if user.id == context.bot.id:
            return True

        key = (message.chat.id, user.id)
        cached = self.admin_cache.get(key)

        if cached and cached[0] > self.clock():
            return cached[1]

        try:
            member = await context.bot.get_chat_member(
                message.chat.id,
                user.id,
            )

        except TelegramError as error:
            logger.warning(
                "Pemeriksaan admin tertunda: jenis=%s",
                type(error).__name__,
            )
            return None

        result = member.status in (
            "creator",
            "administrator",
        )

        if len(self.admin_cache) > 5000:
            self.admin_cache.clear()

        self.admin_cache[key] = (
            self.clock() + 30,
            result,
        )

        return result

    async def moderate(self, update, context):
        message = update.effective_message

        if (
            message is None
            or message.chat.type not in ("group", "supergroup")
        ):
            return

        # Posting otomatis dari channel resmi yang terhubung diizinkan.
        if message.is_automatic_forward:
            return

        if (
            not contains_link(message)
            and not forwarded_media(message)
        ):
            return

        status = await self.admin_status(
            message,
            context,
        )

        if status is not False:
            return

        try:
            await message.delete()

        except TelegramError as error:
            logger.warning(
                "Hapus pesan gagal: jenis=%s. "
                "Periksa izin hapus pesan.",
                type(error).__name__,
            )

        # Hanya menghapus pesan. Tidak melakukan ban, kick, atau restrict.
        raise ApplicationHandlerStop

    async def start(self, update, context):
        message = update.effective_message

        if update.effective_chat.type == "private":
            await message.reply_text(
                BACKUP_TEXT
                + "\n\n"
                + self.settings.backup_url,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "Buka link cadangan",
                        url=self.settings.backup_url,
                    )
                ]]),
                disable_web_page_preview=True,
            )

        else:
            await message.reply_text(
                "Buka chat pribadi bot lalu tekan Start "
                "untuk mengambil link cadangan.",
                reply_markup=self.private_button(context.bot),
            )

    async def private_text(self, update, context):
        text = (
            update.effective_message.text or ""
        ).strip().lower()

        if text in ("start", "star"):
            await self.start(update, context)

        else:
            await update.effective_message.reply_text(
                "Ketik /start untuk mengambil link cadangan."
            )

    async def greet(self, chat_id, users, context):
        fresh = []
        now = self.clock()

        if len(self.welcomed) > 5000:
            self.welcomed = {
                key: stamp
                for key, stamp in self.welcomed.items()
                if now - stamp < 20
            }

        for user in users:
            key = (chat_id, user.id)

            if (
                not user.is_bot
                and now - self.welcomed.get(key, -1000) >= 20
            ):
                self.welcomed[key] = now
                fresh.append(key)

        if not fresh:
            return

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=WELCOME,
                reply_markup=self.private_button(context.bot),
            )

        except TelegramError:
            for key in fresh:
                self.welcomed.pop(key, None)
            raise

    async def welcome(self, update, context):
        message = update.effective_message

        await self.greet(
            message.chat.id,
            message.new_chat_members,
            context,
        )

    async def membership(self, update, context):
        change = update.chat_member

        def joined(member):
            return (
                member.status in (
                    "member",
                    "administrator",
                    "creator",
                )
                or (
                    member.status == "restricted"
                    and member.is_member
                )
            )

        if (
            not joined(change.old_chat_member)
            and joined(change.new_chat_member)
        ):
            await self.greet(
                change.chat.id,
                [change.new_chat_member.user],
                context,
            )

    async def chatter(self, update, context):
        message = update.effective_message

        if (
            message is None
            or update.edited_message is not None
            or message.chat.type not in ("group", "supergroup")
            or message.sender_chat is not None
            or message.from_user is None
            or message.from_user.is_bot
        ):
            return

        enabled = self.chatter_overrides.get(
            message.chat.id,
            self.settings.chatter_enabled,
        )

        if not enabled:
            return

        text = message.text or message.caption or ""

        if (
            not text
            or text.startswith("/")
            or contains_link(message)
            or forwarded_media(message)
        ):
            return

        response = simple_reply(text)

        sensitive = re.search(
            r"\b(meninggal|berduka|kecelakaan|bunuh diri|sakit parah)\b",
            text.lower(),
        )

        if sensitive:
            return

        reply = message.reply_to_message
        replied_user = reply.from_user if reply else None

        mentioned = (
            f"@{context.bot.username.lower()}"
            in text.lower()
        )

        directed = mentioned or (
            replied_user is not None
            and replied_user.id == context.bot.id
        )

        if response is None:
            if not directed:
                return

            response = (
                "Siap bro, aku bot MCM INDUK. "
                "Lagi bahas apa nih? 😄"
            )

        now = self.clock()
        delay = (
            15 if directed
            else self.settings.cooldown
        )

        if (
            now - self.last_reply.get(
                message.chat.id, -10000
            ) < delay
        ):
            return

        if (
            not directed
            and random.random() >= self.settings.chance
        ):
            return

        self.last_reply[message.chat.id] = now

        try:
            await message.reply_text(response)

        except TelegramError as error:
            logger.warning(
                "Balasan nimbrung tertunda: jenis=%s",
                type(error).__name__,
            )

    async def control(self, update, context):
        message = update.effective_message

        if message.chat.type not in ("group", "supergroup"):
            await message.reply_text(
                "Gunakan perintah ini di grup sebagai admin."
            )
            return

        if await self.admin_status(message, context) is not True:
            await message.reply_text(
                "Pengaturan ini khusus admin grup, bro."
            )
            return

        if (
            context.args
            and context.args[0].lower() in ("on", "off")
        ):
            self.chatter_overrides[message.chat.id] = (
                context.args[0].lower() == "on"
            )

        enabled = self.chatter_overrides.get(
            message.chat.id,
            self.settings.chatter_enabled,
        )

        await message.reply_text(
            f"Nimbrung: {'aktif' if enabled else 'nonaktif'}.\n"
            "Atur dengan /nimbrung on atau /nimbrung off.\n"
            "Moderasi link dan forward tetap aktif."
        )


def health_handler():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps({
                "status": (
                    "running"
                    if self.path == "/health"
                    else "not_found"
                )
            }).encode()

            self.send_response(
                200 if self.path == "/health" else 404
            )
            self.send_header(
                "Content-Type", "application/json"
            )
            self.send_header(
                "Content-Length", str(len(body))
            )
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.CRITICAL)

    token = os.environ.get(
        "MCM_BOT_TOKEN", ""
    ).strip()

    if not token:
        raise SystemExit(
            "MCM_BOT_TOKEN belum diisi di Variables Railway."
        )

    controller = CommunityBot(
        Settings.from_env()
    )
    server = None

    async def post_init(application):
        nonlocal server

        server = ThreadingHTTPServer(
            (
                "0.0.0.0",
                int(os.environ.get("PORT", "8080")),
            ),
            health_handler(),
        )

        threading.Thread(
            target=server.serve_forever,
            daemon=True,
        ).start()

        logger.info(
            "MCM INDUK aktif. "
            "Mode nimbrung sederhana, tanpa API AI."
        )

    async def post_shutdown(application):
        if server is not None:
            await asyncio.to_thread(
                server.shutdown
            )
            await asyncio.to_thread(
                server.server_close
            )

    async def error_handler(update, context):
        logger.error(
            "Proses MCM tertunda: jenis=%s",
            type(context.error).__name__,
        )

    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(16)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS,
            controller.moderate,
        ),
        group=-1,
    )

    app.add_handler(
        CommandHandler(
            ["start", "star"],
            controller.start,
        )
    )

    app.add_handler(
        CommandHandler(
            "nimbrung",
            controller.control,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            controller.welcome,
        )
    )

    app.add_handler(
        ChatMemberHandler(
            controller.membership,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.TEXT
            & ~filters.COMMAND,
            controller.private_text,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & (filters.TEXT | filters.CAPTION),
            controller.chatter,
        ),
        group=1,
    )

    app.add_error_handler(
        error_handler
    )

    try:
        app.run_polling(
            allowed_updates=[
                "message",
                "edited_message",
                "chat_member",
            ]
        )

    except TelegramError as error:
        logger.error(
            "Bot gagal tersambung: jenis=%s",
            type(error).__name__,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
