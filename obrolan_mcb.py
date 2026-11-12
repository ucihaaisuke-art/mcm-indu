            self.interjection = None
            if not self.f.active or due[0] != self.f.started_at:
                return
            revision = self.f.revision
            try:
                answer = await self.answer(list(self.f.history),
                    " Sambung obrolan Adit, Denis dan Mak secara singkat; jangan membuka topik baru.", member=False)
            except TimeoutError:
                answer = None
            if answer and await self.send(answer, revision=revision):
                self.f.prepared.clear()
                self.f.revision += 1
                self.f.next_turn_at = self.f.clock() + self.f.gap_seconds

    async def run(self):
        while True:
            try:
                if self.bot is None:
                    await self.connect()
                else:
                    await self.tick()
            except (TelegramError, ValueError, TimeoutError, OSError, sqlite3.Error) as error:
                self.state = "tertunda: " + type(error).__name__
                log.warning("Teman parodi tertunda: %s", type(error).__name__)
            await asyncio.sleep(20)


def pasang_obrolan(c, f, maintenance, schedule):
    """Installer idempoten; tidak mengganti token, routing VIP, atau tabel pesanan."""
    if hasattr(f, "ganjar"):
        return f.ganjar
    info = (
        "Jaga obrolan tetap nyaman. Hindari spam dan data pribadi; laporkan masalah kepada admin.",
        "Kalau ada pertanyaan, sertakan konteksnya biar mudah dibantu. Jangan bagikan OTP atau kata sandi di grup.",
        "Bercanda boleh, saling menghargai tetap jalan. Mak dan kawan-kawan bisa dipanggil untuk ikut ngobrol.",
    )
    # Jalur candaan singkat versi lama juga harus bebas sisipan penjualan.
    banter = {
        "cari_denis": (("Nis, kau dicari. Jangan sembunyi di balik status online.", "Aku di sini bang. Statusku online, alasanku masih loading."),),
        "bisa_adit": (("Bisa bantu hitung, bahas pertanyaan, sama nemenin obrolan, le.", "Kalau Adit mulai serius, aku bagian bikin suasananya nggak tegang."),),
        "bisa_denis": (("Bisa ngobrol sama bantu pertanyaan, bang. Kalau belum tahu ya bilang belum tahu.", "Tumben rapi jawabannya, Nis. Ini sudah disunting berapa kali?"),),
    }
    # Peta hanya berlaku pada instance ini; jangan mengubah konstanta modul
    # yang mungkin dipakai instance lama atau proses pengujian lain.
    pools = {id(rapikan_mcb.DAILY_DIALOGS): DIALOGS, id(jadwal_mcb.WORK_DIALOGS): DIALOGS}
    for old, new in ((rapikan_mcb.CALLS, GREETINGS), (rapikan_mcb.CHAT, CHAT),
                     (rapikan_mcb.UNKNOWN, UNKNOWN)):
        pools.update((id(source), target) for source, target in zip(old, new))
    pools.update((id(rapikan_mcb.MEDAN_DIALOGS[key]), value) for key, value in banter.items())
    original_choose, original_begin = f.choose, f.begin
    original_notice = schedule.notice

    def choose(slot, options):
        return original_choose(slot, pools.get(id(options), options))

    def begin():
        original_begin()
        f.topic = original_choose("obrolan:topic", TOPICS)
        f.recent_topics[-1] = f.topic

    async def notice(text, *, daytime=False):
        if text in jadwal_mcb.INFO:
            text = info[jadwal_mcb.INFO.index(text) % len(info)]
        return await original_notice(text, daytime=daytime)

    f.choose, f.begin, schedule.notice = choose, begin, notice
    if callable(getattr(c.ai, "payload", None)):
        original_payload = c.ai.payload

        def payload(history, instructions):
            return original_payload(history, instructions + POLICY)
        c.ai.payload = payload
    original_local, original_send = f.local_answer, f.send_text
    original_start, original_stop = f.start, f.stop
    original_observe, original_status = f.observe, f.status
    ganjar = f.ganjar = GanjarParodi(c, f, maintenance, schedule)

    def local(index, text):
        if not f.substantive(text):
            return f.pick_local(index, GREETINGS[index])
        if re.search(r"\bbisa (?:apa|apaan)\b", text, re.I) and not SERVICE.search(text):
            return ("Bisa bantu hitung, jawab pertanyaan, dan ngobrol, le. Mau bahas apa?",
                    "Bisa nemenin ngobrol dan bantu pertanyaan, bang. Yang belum pasti nggak kutebak.",
                    "Bisa bantu pertanyaan dan informasi grup, le. Ceritakan yang diperlukan.")[index]
        return original_local(index, text)

    async def send(index, text, *, message=None, revision=None):
        source = (message.text or message.caption or "") if message else ""
        if not SERVICE.search(source) and SERVICE.search(text):
            text = " ".join(part for part in re.split(r"(?<=[.!?])\s+", text) if not SERVICE.search(part))
            if not text:
                return False
        ok = await original_send(index, text, message=message, revision=revision)
        if ok and message is None and index == 2 and ganjar.allowed() and f.active:
            if ganjar.last_session != f.started_at:
                ganjar.last_session = f.started_at
                ganjar.interjection = (f.started_at, f.clock() + f.gap_seconds * .5)
        return ok

    async def start(application):
        await original_start(application)
        await ganjar.start()

    async def stop():
        await ganjar.stop()
        await original_stop()

    async def observe(update, context):
        return await ganjar.observe(update, context, original_observe)

    f.local_answer, f.send_text = local, send
    f.start, f.stop, f.observe = start, stop, observe
    f.status = lambda: original_status() + "\nPak Ganjar: " + ganjar.state
    return ganjar
