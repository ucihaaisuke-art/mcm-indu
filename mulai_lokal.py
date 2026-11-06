"""Menjalankan Bot API lokal dan bot MCB bersama pada satu service Railway."""

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

if __package__:
    from .api_lokal import download_limit, local_port
else:
    from api_lokal import download_limit, local_port

logger = logging.getLogger(__name__)


class StartupError(Exception):
    pass


def checked_settings():
    token = os.environ.get("MCM_BOT_TOKEN", "").strip()
    api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()

    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise StartupError("Isi MCM_BOT_TOKEN pada Railway Variables.")

    if (
        not api_id.isdecimal()
        or int(api_id) <= 0
        or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash)
    ):
        raise StartupError(
            "Isi TELEGRAM_API_ID dan TELEGRAM_API_HASH "
            "dari my.telegram.org pada Railway Variables."
        )

    if not re.fullmatch(
        r"[1-9][0-9]*",
        os.environ.get("MCM_OWNER_ID", "").strip(),
    ):
        raise StartupError(
            "Pertahankan MCM_OWNER_ID akun owner di Railway Variables."
        )

    if not re.fullmatch(
        r"-[1-9][0-9]*",
        os.environ.get("MCM_TARGET_GROUP_ID", "").strip(),
    ):
        raise StartupError(
            "Pertahankan MCM_TARGET_GROUP_ID grup MCB utama di Railway Variables."
        )

    os.environ["TELEGRAM_API_ID"] = api_id
    os.environ["TELEGRAM_API_HASH"] = api_hash
    os.environ["MCM_LOCAL_BOT_API"] = "on"
    os.environ.setdefault("MCM_AI_PROVIDER", "gemini")

    try:
        if __package__:
            from .bot_gabungan import Settings
        else:
            from bot_gabungan import Settings

        Settings.from_env()
    except ImportError:
        raise StartupError(
            "File bot gabungan belum lengkap. "
            "Pasang semua file sebelum deploy mode lokal."
        ) from None
    except SystemExit as error:
        raise StartupError(str(error)) from None

    try:
        port = local_port()
        download_limit()
    except ValueError as error:
        raise StartupError(str(error)) from None

    binary = shutil.which("telegram-bot-api")
    if binary is None:
        raise StartupError(
            "Binary telegram-bot-api belum tersedia. "
            "Deploy memakai Dockerfile yang disediakan."
        )

    directory = Path(
        os.environ.get("MCM_LOCAL_API_DATA", "/data/telegram-bot-api")
    )
    directory.mkdir(parents=True, exist_ok=True)

    temporary = Path("/tmp/mcm-local-api")
    temporary.mkdir(parents=True, exist_ok=True)

    return token, port, directory, temporary, binary


def redact(text):
    for name in (
        "MCM_BOT_TOKEN",
        "MCM_ORBIT_BOT_TOKEN",
        "MCM_BAPER_BOT_TOKEN",
        "TELEGRAM_API_HASH",
    ):
        value = os.environ.get(name, "")
        if value:
            text = text.replace(value, "[rahasia]")

    return re.sub(
        r"\b[0-9]{5,}:[A-Za-z0-9_-]{20,}",
        "[token]",
        text,
    )


def forward_logs(stream):
    for line in stream:
        logger.warning(
            "Bot API lokal: %s",
            redact(line.rstrip())[:1500],
        )


def wait_port(process, port, *, seconds=60, stopped=None):
    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        if stopped is not None and stopped.is_set():
            raise StartupError("Startup dibatalkan.")

        if process.poll() is not None:
            raise StartupError(
                "Bot API lokal berhenti saat startup. "
                "Periksa log server dan pengaturan API Telegram."
            )

        try:
            with socket.create_connection(
                ("127.0.0.1", port),
                timeout=1,
            ):
                return
        except OSError:
            time.sleep(0.25)

    raise StartupError(
        "Bot API lokal belum membuka port dalam waktu yang tersedia."
    )


def migrate_and_check(
    client,
    token,
    port,
    directory,
    *,
    stopped=None,
):
    marker = directory / "mcm-migration.json"
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    migrated = False

    try:
        migrated = (
            json.loads(marker.read_text()).get("token_hash")
            == fingerprint
        )
    except (OSError, ValueError, AttributeError):
        pass

    if not migrated:
        if stopped is not None and stopped.is_set():
            raise StartupError("Startup dibatalkan.")

        logger.info(
            "Migrasi MCB ke Bot API lokal: logOut dari server cloud."
        )

        try:
            response = client.post(
                f"https://api.telegram.org/bot{token}/logOut",
                timeout=20,
            )
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise StartupError(
                "Belum bisa menghubungi Telegram untuk migrasi cloud. "
                "Coba deploy ulang nanti."
            ) from None

        if not isinstance(data, dict) or (
            not data.get("ok")
            and response.status_code != 401
        ):
            raise StartupError(
                "Telegram belum menerima migrasi logOut. "
                "Periksa token dan status server lama."
            )

    endpoint = f"http://127.0.0.1:{port}/bot{token}/getMe"
    deadline = time.monotonic() + 90

    while time.monotonic() < deadline:
        if stopped is not None and stopped.is_set():
            raise StartupError("Startup dibatalkan.")

        try:
            response = client.post(endpoint, timeout=15)
            data = response.json()

            if (
                isinstance(data, dict)
                and data.get("ok")
                and data.get("result", {}).get("is_bot")
            ):
                temp_marker = marker.with_suffix(".tmp")
                temp_marker.write_text(
                    json.dumps({"token_hash": fingerprint})
                )
                temp_marker.replace(marker)

                logger.info(
                    "Bot API lokal tersambung; bot MCB akan dimulai."
                )
                return

        except (httpx.HTTPError, ValueError, AttributeError):
            pass

        time.sleep(2)

    raise StartupError(
        "Bot API lokal belum berhasil login. "
        "Periksa TELEGRAM_API_ID, TELEGRAM_API_HASH, "
        "token, dan koneksi server."
    )


def terminate(process, timeout=20):
    if process is None or process.poll() is not None:
        return

    process.terminate()

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    api = bot = None
    stopped = threading.Event()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())

    try:
        token, port, directory, temporary, binary = checked_settings()

        api = subprocess.Popen(
            [
                binary,
                "--local",
                "--http-ip-address=127.0.0.1",
                f"--http-port={port}",
                f"--dir={directory}",
                f"--temp-dir={temporary}",
                "--verbosity=0",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        threading.Thread(
            target=forward_logs,
            args=(api.stderr,),
            daemon=True,
        ).start()

        wait_port(api, port, stopped=stopped)

        with httpx.Client(
            follow_redirects=False,
            trust_env=False,
        ) as client:
            migrate_and_check(
                client,
                token,
                port,
                directory,
                stopped=stopped,
            )

        bot = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("bot_gabungan.py")),
            ]
        )

        while not stopped.wait(0.25):
            if api.poll() is not None:
                raise StartupError(
                    "Bot API lokal berhenti. "
                    "Service akan dimulai ulang."
                )

            if bot.poll() is not None:
                return bot.returncode

        return 0

    except (StartupError, OSError) as error:
        if stopped.is_set():
            return 0

        if isinstance(error, StartupError):
            logger.error("%s", redact(str(error)))
        else:
            logger.error(
                "Server lokal belum bisa dijalankan. "
                "Periksa volume dan Dockerfile."
            )

        return 1

    finally:
        terminate(bot)
        terminate(api)


if __name__ == "__main__":
    raise SystemExit(main())
