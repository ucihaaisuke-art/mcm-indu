"""Bersihkan salinan MP4 cache Bot API saat server berhenti, lalu mulai MCB."""

import argparse
import errno
import logging
import os
import shutil
import socket
import stat
from pathlib import Path

CACHE = Path("/data/telegram-bot-api")
logger = logging.getLogger(__name__)


class CacheError(Exception):
    pass


def api_is_running():
    try:
        port = int(os.environ.get("MCM_LOCAL_API_PORT", "8081"))
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError as error:
        if error.errno == errno.ECONNREFUSED:
            return False
        raise CacheError(
            "Status server lokal belum bisa dipastikan; cache belum dibersihkan."
        ) from None
    except ValueError:
        raise CacheError("MCM_LOCAL_API_PORT tidak valid.") from None


def scan_cache(root):
    root = Path(root)
    if root.is_symlink():
        raise CacheError("Folder cache berupa tautan; pemeriksaan dihentikan.")

    found, other_bytes = [], 0
    if not root.exists():
        return found, other_bytes

    base = root.resolve()
    for directory, folders, filenames in os.walk(base, followlinks=False):
        folders[:] = [
            name for name in folders
            if not (Path(directory) / name).is_symlink()
        ]

        for name in filenames:
            path = Path(directory) / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue

            with path.open("rb") as stream:
                header = stream.read(16)

            box_size = int.from_bytes(header[:4], "big")
            if (
                len(header) == 16
                and header[4:8] == b"ftyp"
                and 16 <= box_size <= info.st_size
            ):
                found.append((path, info))
            else:
                other_bytes += info.st_size

    return found, other_bytes


def clean_cache(root=CACHE):
    if api_is_running():
        raise CacheError(
            "Server lokal masih aktif. "
            "Pembersihan hanya berjalan sebelum server dimulai."
        )

    root = Path(root)
    candidates, other_bytes = scan_cache(root)
    base = root.resolve()
    removed = 0

    for path, original in candidates:
        try:
            current = path.lstat()
            if (
                not stat.S_ISREG(current.st_mode)
                or not path.resolve().is_relative_to(base)
                or (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                ) != (
                    original.st_dev,
                    original.st_ino,
                    original.st_size,
                    original.st_mtime_ns,
                )
            ):
                continue

            path.unlink()
            removed += current.st_size

        except FileNotFoundError:
            pass

    logger.info("CACHE_MP4_DIBERSIHKAN_MB=%.2f", removed / 1048576)
    logger.info("CACHE_LAIN_DIPERTAHANKAN_MB=%.2f", other_bytes / 1048576)
    return removed


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--bersihkan", action="store_true")
    mode.add_argument("--mulai", action="store_true")
    args = parser.parse_args()

    try:
        configured = Path(os.environ.get("MCM_LOCAL_API_DATA", str(CACHE)))
        if configured != CACHE:
            raise CacheError(
                "Folder Bot API berbeda; pembersih ini hanya untuk "
                "/data/telegram-bot-api."
            )

        if args.bersihkan or args.mulai:
            clean_cache()
        else:
            candidates, other = scan_cache(CACHE)
            logger.info(
                "CACHE_MP4_DITEMUKAN_MB=%.2f",
                sum(info.st_size for _, info in candidates) / 1048576,
            )
            logger.info("CACHE_LAIN_MB=%.2f", other / 1048576)

        free = shutil.disk_usage("/data").free
        logger.info("RUANG_TERSEDIA_MB=%.2f", free / 1048576)

        if args.mulai:
            if free < 32 * 1048576:
                raise CacheError(
                    "Ruang belum cukup setelah pemeriksaan cache. "
                    "Kirim log hasil pembersihannya."
                )

            if __package__:
                from .mulai_lokal import main as start_bot
            else:
                from mulai_lokal import main as start_bot

            return start_bot()

        return 0

    except (CacheError, OSError) as error:
        if isinstance(error, CacheError):
            message = str(error)
        else:
            message = f"Pemeriksaan cache terhenti: errno={error.errno}."

        logger.error("%s", message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
