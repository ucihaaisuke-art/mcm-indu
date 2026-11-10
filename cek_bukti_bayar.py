"""Pemeriksaan awal waktu bukti; persetujuan uang masuk tetap milik owner."""

import asyncio
import base64
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

if __package__:
    from .vip_mcm import PaymentError, PLANS, sender_name
else:
    from vip_mcm import PaymentError, PLANS, sender_name

logger = logging.getLogger(__name__)
WIB = timezone(timedelta(hours=7))
CONTACT = "https://t.me/Madamselin"
MAX_BYTES = 8 * 1024 * 1024
WINDOW = 30 * 60
ZONES = {"WIB": 7, "WITA": 8, "WIT": 9, "UTC": 0, "unknown": 7}
MONTHS = {
    "jan": 1, "januari": 1, "january": 1, "feb": 2, "februari": 2, "february": 2,
    "mar": 3, "maret": 3, "march": 3, "apr": 4, "april": 4, "mei": 5, "may": 5,
    "jun": 6, "juni": 6, "june": 6, "jul": 7, "juli": 7, "july": 7,
    "agu": 8, "ags": 8, "agt": 8, "agustus": 8, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "okt": 10, "oktober": 10, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "des": 12, "desember": 12,
    "dec": 12, "december": 12,
}
POLICY = (
    "Kirim bukti segera setelah transfer, maksimal 30 menit setelah transaksi "
    "dan pada tanggal yang sama dalam WIB. Tanggal/jam transaksi akan dibaca "
    "otomatis sebelum bukti diteruskan ke owner. "
    "Jika bukti terlambat atau tidak terbaca, hubungi @Madamselin. "
    "Kalau sudah transfer, jangan bayar ulang."
)
REASONS = {
    "valid": "Tanggal/jam memenuhi batas pengiriman otomatis.",
    "late": "Bukti dikirim lebih dari 30 menit setelah transaksi.",
    "different_day": "Tanggal transaksi berbeda dari tanggal pengiriman bukti dalam WIB.",
    "future": "Waktu transaksi pada bukti lebih maju dari waktu pengiriman pesan.",
    "unreadable": "Tanggal/jam transaksi belum dapat dibaca dengan pasti.",
    "unavailable": "Pemeriksaan otomatis sedang tidak tersedia.",
    "checking": "Pemeriksaan otomatis belum selesai.",
    "legacy": "Bukti lama belum memiliki catatan pemeriksaan waktu otomatis.",
}


def printed_date(value):
    if not isinstance(value, str) or len(value) > 100:
        return None
    text = value.strip().casefold()
    found = []
    for pattern, order in ((r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)", "ymd"),
                           (r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?!\d)", "dmy")):
        for match in re.finditer(pattern, text):
            parts = list(map(int, match.groups()))
            y, m, d = parts if order == "ymd" else (parts[2], parts[1], parts[0])
            if y < 100:
                y += 2000
            try:
                found.append(date(y, m, d))
            except ValueError:
                return None
    for match in re.finditer(r"(?<!\d)(\d{1,2})\s*[- .]?\s*([a-z]+)\.?\s*[- ,]?\s*(\d{4}|\d{2})(?!\d)", text):
        d, month, y = match.groups()
        if month not in MONTHS:
            continue
        try:
            year = int(y) + (2000 if len(y) == 2 else 0)
            found.append(date(year, MONTHS[month], int(d)))
        except ValueError:
            return None
    return found[0] if found and len(set(found)) == 1 else None


def receipt_time(reading, submitted):
    """Putuskan dari waktu transaksi ke waktu kirim Telegram, bukan waktu OCR."""
    result = {"status": "unreadable", "paid_at": None, "precision": None}
    if not isinstance(submitted, datetime) or submitted.tzinfo is None:
        return result
    if (not isinstance(reading, dict) or reading.get("is_receipt") is not True
            or reading.get("ambiguous") is not False):
        return result
    day = printed_date(reading.get("date_text"))
    raw_time = reading.get("time_text")
    zone = reading.get("timezone")
    if (day is None or not isinstance(raw_time, str) or len(raw_time) > 60
            or not isinstance(zone, str) or zone not in ZONES):
        return result
    match = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?\s*(AM|PM)?\s*", raw_time, re.I)
    if not match:
        return result
    hour, minute, second, period = match.groups()
    hour, minute = int(hour), int(minute)
    if period:
        if not 1 <= hour <= 12:
            return result
        hour = hour % 12 + (12 if period.upper() == "PM" else 0)
    try:
        paid = datetime(day.year, day.month, day.day, hour, minute, int(second or 0),
                        tzinfo=timezone(timedelta(hours=ZONES[zone])))
    except ValueError:
        return result
    received = submitted.astimezone(WIB)
    paid = paid.astimezone(WIB)
    result.update(paid_at=int(paid.timestamp()), precision="second" if second is not None else "minute")
    if paid.date() != received.date():
        result["status"] = "different_day"
    elif paid > received:
        result["status"] = "future"
    else:
        oldest_age = (received - paid).total_seconds()
        newest_age = max(0, oldest_age - (59 if second is None else 0))
        result["status"] = ("late" if newest_age > WINDOW else
                            "valid" if oldest_age <= WINDOW else "unreadable")
    return result


class CekBuktiBayar:
    def __init__(self, controller, *, clock=time.time):
