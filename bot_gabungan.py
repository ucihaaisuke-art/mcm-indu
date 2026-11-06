"""MCM: moderasi, chat pribadi, VIP, teman Emak, dan pemeliharaan."""

import asyncio
import logging
import os
import threading
from http.server import ThreadingHTTPServer

from telegram.error import Conflict, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

if __package__:
    from .bot_mcm import CommunityBot, Settings, health_handler
    from .chat_pribadi import pasang_chat_pribadi
    from .vip_mcm import pasang_vip_mcm
    from .rapikan_mcb import RapikanMcb, TemanTerawat
else:
    from bot_mcm import CommunityBot, Settings, health_handler
    from chat_pribadi import pasang_chat_pribadi
    from vip_mcm import pasang_vip_mcm
    from rapikan_mcb import RapikanMcb, TemanTerawat

logger = logging.getLogger(__name__)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.CRITICAL)

    token = os.environ.get("MCM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Isi MCM_BOT_TOKEN pada service MCM."
        )

    os.environ.setdefault("MCM_AI_PROVIDER", "gemini")

    bot_class = type(
        "BotMcmDanVip", (CommunityBot,), {}
    )
    pasang_chat_pribadi(bot_class)
    pasang_vip_mcm(bot_class)

    controller = bot_class(Settings.from_env())
    maintenance = RapikanMcb(controller)
    friends = TemanTerawat(controller, maintenance)
    server = None

    async def post_init(application):
        nonlocal server

        controller.vip.bot = application.bot
        await friends.start(application)
        await maintenance.start(application, friends)

        logger.info(
            "Akun Telegram yang dipakai: @%s (ID %s).",
            application.bot.username,
            application.bot.id,
        )

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
            "Konfigurasi MCM gabungan dimuat; "
            "polling Telegram akan dimulai."
        )

    async def post_stop(application):
        await maintenance.stop()
        await friends.stop()
        await controller.stop_guidance()

    async def post_shutdown(application):
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
            logger.error(
                "Polling Telegram konflik. Periksa apakah "
                "token bot yang sama sedang dipakai "
                "deployment/service lain atau webhook masih aktif."
            )
            return

        logger.error(
            "Proses bot MCM gabungan tertunda: jenis=%s",
            type(context.error).__name__,
        )

    async def teman_command(update, context):
        await friends.command(update, context)

    async def teman_observe(update, context):
        await friends.observe(update, context)

    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(16)
        .post_init(post_init)
        .post_stop(post_stop)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS,
            maintenance.observe,
        ),
        group=-3,
    )
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS
            | filters.StatusUpdate.LEFT_CHAT_MEMBER,
            controller.service_notice,
        ),
        group=-2,
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
            ["start", "star"], controller.start
        )
    )
    app.add_handler(
        CommandHandler("vip", controller.vip_menu)
    )
    app.add_handler(
        CommandHandler(
            ["akses", "cek", "pending"],
            controller.vip_command,
        )
    )
    app.add_handler(
        CommandHandler("setqris", controller.vip_qris)
    )
    app.add_handler(
        CommandHandler("batal", controller.vip_cancel)
    )
    app.add_handler(
        CommandHandler("nimbrung", controller.control)
    )
    app.add_handler(
        CommandHandler("teman", teman_command)
    )
    app.add_handler(
        CommandHandler("id", controller.show_id)
    )
    app.add_handler(
        CommandHandler(
            "hapuswelcome", controller.remove_welcome
        )
    )
    app.add_handler(
        CommandHandler("tesai", controller.test_ai)
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
        ChatJoinRequestHandler(controller.vip_join)
    )
    app.add_handler(
        CallbackQueryHandler(
            controller.vip_callback,
            pattern=(
                r"^(?:mcm:(?:backup|video|vip)$"
                r"|vip:(?:buy|proof|check):)"
            ),
        )
    )

    media = (
        filters.PHOTO
        | filters.VIDEO
        | filters.ANIMATION
        | filters.AUDIO
        | filters.VOICE
        | filters.VIDEO_NOTE
        | filters.Document.ALL
        | filters.Sticker.ALL
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.UpdateType.MESSAGE
            & media,
            controller.private_media,
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
            filters.ChatType.GROUPS,
            teman_observe,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS,
            controller.chatter,
        ),
        group=1,
    )

    app.add_error_handler(error_handler)

    try:
        app.run_polling(
            allowed_updates=[
                "message",
                "edited_message",
                "chat_member",
                "callback_query",
                "chat_join_request",
            ]
        )
    except TelegramError as error:
        logger.error(
            "Bot MCM gabungan belum tersambung: jenis=%s",
            type(error).__name__,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
