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
