"""Konfigurasi Bot API lokal yang berbagi container dan filesystem dengan MCB."""

import os


def local_enabled():
    return os.environ.get("MCM_LOCAL_BOT_API", "off").strip().lower() in ("on", "true", "1")


def local_port():
    try:
        port = int(os.environ.get("MCM_LOCAL_API_PORT", "8081"))
        public = int(os.environ.get("PORT", "8080"))
    except ValueError:
        raise ValueError("Port Bot API lokal tidak valid.") from None
    if not 1024 <= port <= 65535 or port == public:
        raise ValueError("Port Bot API lokal harus 1024–65535 dan berbeda dari PORT aplikasi.")
    return port


def configure_builder(builder):
    if not local_enabled():
        return builder
    endpoint = f"http://127.0.0.1:{local_port()}"
    return (builder.base_url(endpoint + "/bot").base_file_url(endpoint + "/file/bot")
            .local_mode(True).connect_timeout(15).read_timeout(90)
            .write_timeout(1800).media_write_timeout(1800))


def download_limit():
    local = local_enabled()
    default, maximum = (200, 1900) if local else (45, 45)
    try:
        size = int(os.environ.get("MCM_X_MAX_MB", str(default)))
    except ValueError:
        raise ValueError("MCM_X_MAX_MB harus berupa angka bulat.") from None
    if not 1 <= size <= maximum:
        raise ValueError(f"MCM_X_MAX_MB harus 1–{maximum} untuk mode Bot API yang aktif.")
    return size * 1024 * 1024
