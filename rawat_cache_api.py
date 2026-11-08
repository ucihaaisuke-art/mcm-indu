"""Bersihkan salinan MP4 saat server berhenti, lalu mulai MCB."""

import argparse
import errno
import hashlib
import logging
import os
import shutil
import socket
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path

CACHE = Path("/data/telegram-bot-api")
logger = logging.getLogger(__name__)
FREE_TARGET = 256 * 1048576


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


def checked_unlink(path, original, root):
    try:
        current = path.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or not path.resolve().is_relative_to(root.resolve())
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
            return 0
        path.unlink()
        return current.st_size
    except FileNotFoundError:
        return 0


def clean_cache(root=CACHE):
    if api_is_running():
        raise CacheError(
            "Server lokal masih aktif. Pembersihan hanya berjalan sebelum server dimulai."
        )
    root = Path(root)
    candidates, other_bytes = scan_cache(root)
    removed = 0
    for path, original in candidates:
        removed += checked_unlink(path, original, root)
    logger.info("CACHE_MP4_DIBERSIHKAN_MB=%.2f", removed / 1048576)
    logger.info("CACHE_LAIN_DIPERTAHANKAN_MB=%.2f", other_bytes / 1048576)
    return removed


def media_locations(data_root):
    database = Path(
        os.environ.get("MCM_MEDIA_DB", "mcm_media.sqlite3")
    ).absolute()
    directory = database.parent / "mcm_videos"
    if ".." in database.parts or ".." in data_root.absolute().parts:
        raise CacheError(
            "Lokasi katalog tidak baku; salinan lokal belum dibersihkan."
        )
    for item in (database, directory):
        if not item.is_relative_to(data_root.absolute()):
            return None
        current = item
        while current != data_root.absolute():
            if current.is_symlink():
                raise CacheError(
                    "Lokasi katalog berupa tautan; salinan lokal belum dibersihkan."
                )
            current = current.parent
    if data_root.is_symlink():
        raise CacheError(
            "Lokasi volume berupa tautan; salinan lokal belum dibersihkan."
        )
    return database, directory


def clean_catalog_cache(data_root=Path("/data"), *, target_free=FREE_TARGET):
    """Buang salinan terverifikasi paling lama sampai ruang target tercapai."""
    if api_is_running():
        raise CacheError(
            "Server lokal masih aktif. Salinan katalog belum dibersihkan."
        )
    data_root = Path(data_root).absolute()
    locations = media_locations(data_root)
    if locations is None:
        logger.info(
            "SALINAN_LOKAL_DILEWATI: lokasi katalog bukan di volume ini."
        )
        return 0
    database, directory = locations
    if not database.is_file() or not directory.is_dir():
        logger.info(
            "SALINAN_LOKAL_DILEWATI: katalog atau folder salinan belum tersedia."
        )
        return 0
    try:
        bot_id = int(os.environ.get("MCM_BOT_TOKEN", "").split(":")[0])
        owner_id = int(os.environ.get("MCM_OWNER_ID", ""))
        group_id = int(os.environ.get("MCM_TARGET_GROUP_ID", ""))
        if bot_id <= 0 or owner_id <= 0 or group_id >= 0:
            raise ValueError
    except ValueError:
        logger.info(
            "SALINAN_LOKAL_DILEWATI: identitas bot/owner/grup belum valid."
        )
        return 0

    try:
        with closing(
            sqlite3.connect(
                database.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=10,
            )
        ) as db:
            rows = db.execute(
                "SELECT DISTINCT file_id FROM video "
                "WHERE bot_id=? AND owner_id=? AND group_id=? AND kind='video'",
                (bot_id, owner_id, group_id),
            ).fetchall()
    except sqlite3.Error:
        logger.warning(
            "SALINAN_LOKAL_DILEWATI: katalog belum bisa diverifikasi; database tidak diubah."
        )
        return 0

    permitted = set()
    for (file_id,) in rows:
        key = hashlib.sha256(
            f"{bot_id}/{owner_id}/{group_id}/{file_id}".encode()
        ).hexdigest()
        permitted.update((key + ".mp4", key + ".part"))

    candidates = []
    for path in directory.iterdir():
        if path.name not in permitted:
            continue
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
            candidates.append((path, info))

    removed = 0
    for path, info in sorted(
        candidates, key=lambda item: item[1].st_mtime_ns
    ):
        if shutil.disk_usage(data_root).free >= target_free:
            break
        removed += checked_unlink(path, info, directory)

    logger.info("SALINAN_LOKAL_DIBERSIHKAN_MB=%.2f", removed / 1048576)
    return removed


def tree_bytes(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        return 0
    total = 0
    for directory, folders, filenames in os.walk(root, followlinks=False):
        folders[:] = [
            name for name in folders
            if not (Path(directory) / name).is_symlink()
        ]
        for name in filenames:
            try:
                info = (Path(directory) / name).lstat()
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
            except FileNotFoundError:
                pass
    return total


def show_volume_usage(data_root=Path("/data")):
    data_root = Path(data_root)
    locations = media_locations(data_root)
    local_bytes = tree_bytes(locations[1]) if locations else 0
    api_bytes = tree_bytes(data_root / "telegram-bot-api")
    total_bytes = tree_bytes(data_root)
    logger.info("VOLUME_API_MB=%.2f", api_bytes / 1048576)
    logger.info("VOLUME_SALINAN_VIDEO_MB=%.2f", local_bytes / 1048576)
    logger.info(
        "VOLUME_FILE_LAIN_MB=%.2f",
        max(0, total_bytes - api_bytes - local_bytes) / 1048576,
    )


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--bersihkan", action="store_true")
    mode.add_argument("--mulai", action="store_true")
    args = parser.parse_args()

    try:
        configured = Path(
            os.environ.get("MCM_LOCAL_API_DATA", str(CACHE))
        )
        if configured != CACHE:
            raise CacheError(
                "Folder Bot API berbeda; pembersih ini hanya untuk /data/telegram-bot-api."
            )

        if args.bersihkan or args.mulai:
            clean_cache()
            clean_catalog_cache()
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
                show_volume_usage()
                raise CacheError(
                    "Ruang belum cukup setelah pemeriksaan cache. Kirim log hasil pembersihannya."
                )
            if __package__:
                from .mulai_lokal import main as start_bot
            else:
                from mulai_lokal import main as start_bot
            return start_bot()
        return 0

    except (CacheError, OSError) as error:
        message = (
            str(error)
            if isinstance(error, CacheError)
            else f"Pemeriksaan cache terhenti: errno={error.errno}."
        )
        logger.error("%s", message)
        if args.mulai:
            logger.error(
                "STARTUP_DITUNDA_20_DETIK: mencegah restart/log berulang terlalu cepat."
            )
            time.sleep(20)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
