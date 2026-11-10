"""Satu bot MCM: moderasi grup, chat/media pribadi, dan VIP QRIS manual."""

import logging
import os
import threading
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from http.server import ThreadingHTTPServer

from telegram.error import Conflict, TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, ChatJoinRequestHandler, ChatMemberHandler,
    CommandHandler, MessageHandler, filters,
)

if __package__:
    from .bot_mcm import CommunityBot, Settings, health_handler
    from .chat_pribadi import pasang_chat_pribadi
    from .vip_mcm import pasang_vip_mcm
    from .rapikan_mcb import RapikanMcb
    from .unduh_x import UnduhX
    from .kirim_video_vip import KirimVideoVIP
    from .api_lokal import configure_builder, local_enabled
    from .jadwal_mcb import JadwalMcb
    from .jawaban_konteks import TemanKonteks
    from .cek_bukti_bayar import CekBuktiBayar
else:
    from bot_mcm import CommunityBot, Settings, health_handler
    from chat_pribadi import pasang_chat_pribadi
    from vip_mcm import pasang_vip_mcm
    from rapikan_mcb import RapikanMcb
    from unduh_x import UnduhX
    from kirim_video_vip import KirimVideoVIP
    from api_lokal import configure_builder, local_enabled
    from jadwal_mcb import JadwalMcb
    from jawaban_konteks import TemanKonteks
    from cek_bukti_bayar import CekBuktiBayar

logger = logging.getLogger(__name__)


class RapikanSehari(RapikanMcb):
    """Simpan pesan tiga bot 24 jam; aturan pesan anggota tetap terpisah."""

    RETENTION = 24 * 60 * 60

    def __init__(self, controller, **kwargs):
        self.outgoing_source = ContextVar("mcm_retention_source", default=None)
        super().__init__(controller, **kwargs)
        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "CREATE TABLE IF NOT EXISTS mcm_bot_retention ("
                "chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, "
                "due_at INTEGER NOT NULL, PRIMARY KEY(chat_id,message_id))"
            )

    @contextmanager
    def outgoing(self, message):
        source = ((message.chat.id, message.message_id)
                  if message is not None else (None, None))
        token = self.outgoing_source.set(source)
        try:
            yield
        finally:
            self.outgoing_source.reset(token)

    def install_handlers(self):
        super().install_handlers()
        original_send = self.c.send_group_text
        original_menu = self.c.vip_menu

        async def send(message, text, **kwargs):
            with self.outgoing(message):
                result = await original_send(message, text, **kwargs)
                if result is not None:
                    bot = kwargs.get("bot") or self.bot or self.c.vip.bot
                    self.expire(bot, message.chat.id, result.message_id, self.RETENTION)
                return result

        async def menu(update, context):
            with self.outgoing(update.effective_message):
                return await original_menu(update, context)

        self.c.send_group_text = send
        self.c.vip_menu = menu

    def attach_friends(self, friends):
        if getattr(friends, "_retention_owner", None) is self:
            return
        original_send = friends.send_text

        async def send(index, text, *, message=None, revision=None):
            with self.outgoing(message):
                return await original_send(index, text, message=message, revision=revision)

        friends.send_text = send
        friends._retention_owner = self

    def expire(self, bot, chat_id, message_id, seconds):
        if bot is None or chat_id != self.c.settings.target_group_id:
            return
        source = self.outgoing_source.get()
        is_bot_message = source is not None and source != (chat_id, message_id)
        with self.store.lock, self.store.connection:
            if is_bot_message:
                self.store.connection.execute(
                    "INSERT OR IGNORE INTO mcm_bot_retention VALUES(?,?,?)",
                    (chat_id, message_id, int(self.clock() + self.RETENTION)),
                )
            row = self.store.connection.execute(
                "SELECT due_at FROM mcm_bot_retention WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            ).fetchone()
            if row is not None:
                self.store.connection.execute(
                    "INSERT INTO mcm_cleanup(chat_id,message_id,sender_bot,due_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(chat_id,message_id) DO UPDATE "
                    "SET due_at=CASE WHEN finished=0 THEN excluded.due_at ELSE due_at END",
                    (chat_id, message_id, bot.id, row["due_at"]),
                )
        if row is None:
            return super().expire(bot, chat_id, message_id, seconds)
        self.wake.set()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.CRITICAL)
    token = os.environ.get("MCM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Isi MCM_BOT_TOKEN pada service MCM.")
    os.environ.setdefault("MCM_AI_PROVIDER", "gemini")
    bot_class = type("BotMcmDanVip", (CommunityBot,), {})
    pasang_chat_pribadi(bot_class)
    pasang_vip_mcm(bot_class)
    controller = bot_class(Settings.from_env())

    async def salinan_telegram(bot, file_id, kind):
        return file_id

    controller.salinan_lokal = salinan_telegram
    logger.info("Mode hemat ruang: video memakai file_id Telegram; salinan lokal baru dimatikan.")
    maintenance = RapikanSehari(controller)
    schedule = JadwalMcb(controller)
    friends = TemanKonteks(controller, maintenance, schedule)
    maintenance.attach_friends(friends)
    downloads = UnduhX(controller)
    uploads = KirimVideoVIP(controller)
    receipt_checks = CekBuktiBayar(controller)
    server = None

    async def post_init(application):
        nonlocal server
        controller.vip.bot = application.bot
        await schedule.start(application, friends)
        await friends.start(application)
        await maintenance.start(application, friends)
        logger.info("Akun Telegram yang dipakai: @%s (ID %s).", application.bot.username, application.bot.id)
        server = ThreadingHTTPServer(
            ("0.0.0.0", int(os.environ.get("PORT", "8080"))), health_handler()
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        logger.info("Konfigurasi MCM gabungan dimuat; polling Telegram akan dimulai.")

    async def post_stop(application):
        await schedule.stop()
        await uploads.stop()
        await downloads.stop()
        await maintenance.stop()
        await friends.stop()
        await controller.stop_guidance()

    async def post_shutdown(application):
        await schedule.stop()
        await uploads.stop()
        await downloads.stop()
        await maintenance.stop()
        await friends.stop()
        await controller.stop_guidance()
        if server is not None:
            await asyncio.to_thread(server.shutdown)
            await asyncio.to_thread(server.server_close)
        if controller.ai is not None:
            await controller.ai.close()
        controller.vip_store.close()

    async def error_handler(update, context):
        if isinstance(context.error, Conflict):
            logger.error("Polling Telegram konflik. Periksa apakah token bot yang sama sedang dipakai deployment/service lain atau webhook masih aktif.")
            return
        logger.error("Proses bot MCM gabungan tertunda: jenis=%s", type(context.error).__name__)

    async def teman_command(update, context):
        await friends.command(update, context)

    async def teman_observe(update, context):
        await friends.observe(update, context)

    async def owner_text(update, context):
        if await uploads.reply_text(update, context):
            return
        await downloads.owner_text(update, context)

    async def upload_command(update, context):
        await uploads.command(update, context)

    async def media_received(update, context):
        await uploads.private_media(update, context)

    async def jadwal_command(update, context):
        await schedule.command(update, context)

    async def group_chat(update, context):
        if not schedule.night():
            await controller.chatter(update, context)

    app = (configure_builder(Application.builder().token(token)).concurrent_updates(16)
           .post_init(post_init).post_stop(post_stop).post_shutdown(post_shutdown).build())
    if local_enabled():
        logger.info("MCB memakai Bot API lokal; unggahan memakai filesystem bersama.")
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, maintenance.observe), group=-3)
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER,
        controller.service_notice,
    ), group=-2)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, controller.moderate), group=-1)
    app.add_handler(CommandHandler(["start", "star"], controller.start))
    app.add_handler(CommandHandler("vip", controller.vip_menu))
    app.add_handler(CommandHandler(["akses", "cek", "pending"], controller.vip_command))
    app.add_handler(CommandHandler("setqris", controller.vip_qris))
    app.add_handler(CommandHandler("batal", controller.vip_cancel))
    app.add_handler(CommandHandler("nimbrung", controller.control))
    app.add_handler(CommandHandler("teman", teman_command))
    app.add_handler(CommandHandler("jadwal", jadwal_command))
    app.add_handler(CommandHandler("id", controller.show_id))
    app.add_handler(CommandHandler("hapuswelcome", controller.remove_welcome))
    app.add_handler(CommandHandler("tesai", controller.test_ai))
    app.add_handler(CommandHandler("kirim", upload_command))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, controller.welcome))
    app.add_handler(ChatMemberHandler(controller.membership, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(controller.vip_join))
    app.add_handler(CallbackQueryHandler(controller.vip_callback,
        pattern=r"^(?:mcm:(?:backup|video|vip)$|vip:(?:buy|proof|check):)"))
    media = (filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.AUDIO
             | filters.VOICE | filters.VIDEO_NOTE | filters.Document.ALL | filters.Sticker.ALL)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE & media,
                                   media_received))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND
                                   & filters.User(user_id=controller.settings.owner_id), owner_text))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
                                   controller.private_text))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, teman_observe))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, group_chat), group=1)
    if __package__:
        from .respon_pembayaran import pasang_respon_pembayaran
    else:
        from respon_pembayaran import pasang_respon_pembayaran
    pasang_respon_pembayaran(app, controller)
    app.add_error_handler(error_handler)
    try:
        app.run_polling(allowed_updates=["message", "edited_message", "chat_member", "callback_query", "chat_join_request"])
    except TelegramError as error:
        logger.error("Bot MCM gabungan belum tersambung: jenis=%s", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
