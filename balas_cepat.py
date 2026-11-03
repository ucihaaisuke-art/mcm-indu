import asyncio
import logging
import os

from telegram.error import TelegramError

logger = logging.getLogger("mcm_induk")


class _BalasCepat:
    def start_typing(self, chat_id, context):
        if not callable(
            getattr(context.bot, "send_chat_action", None)
        ):
            return None

        state = self.typing_state.get(chat_id)

        if state is None or state["task"].done():
            state = {
                "users": 0,
                "done": asyncio.Event(),
            }
            state["task"] = asyncio.create_task(
                self.show_typing(chat_id, context, state)
            )
            self.typing_state[chat_id] = state

        state["users"] += 1
        return state

    async def stop_typing(self, chat_id, state):
        if state is None:
            return

        state["users"] -= 1

        if state["users"] <= 0:
            if self.typing_state.get(chat_id) is state:
                self.typing_state.pop(chat_id, None)

            state["done"].set()
            state["task"].cancel()

            await asyncio.gather(
                state["task"],
                return_exceptions=True,
            )

    async def show_typing(self, chat_id, context, state):
        while (
            not state["done"].is_set()
            and getattr(
                context.application,
                "running",
                True,
            )
        ):
            try:
                async with asyncio.timeout(2):
                    await context.bot.send_chat_action(
                        chat_id=chat_id,
                        action="typing",
                    )
            except (TelegramError, TimeoutError):
                return

            try:
                await asyncio.wait_for(
                    state["done"].wait(),
                    timeout=4,
                )
            except TimeoutError:
                pass

    async def deliver_every_message(
        self,
        message,
        context,
        history,
        *,
        queued_at=None,
    ):
        chat_id = message.chat.id

        queued_at = (
            self.clock()
            if queued_at is None else queued_at
        )

        deadline = queued_at + getattr(
            self.settings,
            "reply_timeout",
            self.fast_reply_timeout,
        )

        started = self.clock()
        ai_seconds = 0
        outcome = "dibatalkan"
        typing = None

        def enabled():
            return (
                self.chatter_overrides.get(
                    chat_id,
                    self.settings.chatter_enabled,
                )
                and getattr(
                    context.application,
                    "running",
                    True,
                )
                and (
                    chat_id,
                    message.message_id,
                ) not in self.blocked_messages
            )

        try:
            if not enabled():
                return

            typing = self.start_typing(chat_id, context)

            try:
                remaining = max(
                    0.001,
                    deadline - self.clock(),
                )

                async with asyncio.timeout(remaining):
                    member_key = (
                        chat_id,
                        message.from_user.id,
                    )

                    member_lock = (
                        self.member_reply_locks.setdefault(
                            member_key,
                            asyncio.Lock(),
                        )
                    )

                    async with member_lock:
                        group_slots = (
                            self.conversation_locks.setdefault(
                                chat_id,
                                asyncio.Semaphore(2),
                            )
                        )

                        async with group_slots:
                            if not enabled():
                                return

                            started = self.clock()

                            response = await self.ai.answer(
                                history,
                                wait_for_budget=True,
                                enabled=enabled,
                            )

                            ai_seconds = (
                                self.clock() - started
                            )

                outcome = (
                    "jawaban"
                    if response is not None
                    else "AI_belum_tersedia"
                )

            except TimeoutError:
                ai_seconds = self.clock() - started
                response = None
                outcome = "batas_waktu"

            if not enabled():
                return

            if response is None:
                await self.chat_notice(
                    message,
                    "Emak lagi terkendala sambungan atau "
                    "antrean AI, le. Coba lagi sebentar ya.",
                )
                return

            # Pengiriman Telegram berada di luar batas waktu AI
            # supaya tidak menyebabkan kiriman ganda.
            send_started = self.clock()

            sent = await self.send_group_text(
                message,
                response,
                disable_web_page_preview=True,
            )

            if (
                sent is not None
                and chat_id in self.history
            ):
                self.last_reply[chat_id] = self.clock()
                self.history[chat_id].append({
                    "speaker": "bot",
                    "text": response,
                })

            logger.info(
                "Balasan %s: antre=%.1fs AI=%.1fs "
                "kirim=%.1fs hasil=%s",
                self.ai_name,
                max(0, started - queued_at),
                ai_seconds,
                self.clock() - send_started,
                outcome,
            )

        except TelegramError as error:
            logger.warning(
                "Balasan grup tertunda: jenis=%s",
                type(error).__name__,
            )

        finally:
            await self.stop_typing(chat_id, typing)

            if outcome != "jawaban":
                logger.info(
                    "Balasan %s: total=%.1fs hasil=%s",
                    self.ai_name,
                    self.clock() - queued_at,
                    outcome,
                )

            queued = (
                self.queued_replies.get(chat_id, 1) - 1
            )

            if queued:
                self.queued_replies[chat_id] = queued
            else:
                self.queued_replies.pop(chat_id, None)


def pasang_balas_cepat(bot_class):
    if bot_class.__dict__.get(
        "_balas_cepat_terpasang",
        False,
    ):
        return

    original_init = bot_class.__init__

    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

        try:
            timeout = float(os.environ.get(
                "MCM_AI_REPLY_TIMEOUT",
                "75",
            ))
        except ValueError:
            raise SystemExit(
                "MCM_AI_REPLY_TIMEOUT harus "
                "berupa angka 5–180."
            ) from None

        if not 5 <= timeout <= 180:
            raise SystemExit(
                "MCM_AI_REPLY_TIMEOUT harus "
                "5–180 detik."
            )

        self.fast_reply_timeout = timeout
        self.member_reply_locks = {}
        self.typing_state = {}

        logger.info(
            "Balas cepat v13 aktif: dua member dapat "
            "diproses bersamaan; jawaban tetap lewat AI."
        )

    bot_class.__init__ = initialize

    for name in (
        "start_typing",
        "stop_typing",
        "show_typing",
        "deliver_every_message",
    ):
        setattr(
            bot_class,
            name,
            getattr(_BalasCepat, name),
        )

    bot_class._balas_cepat_terpasang = True
