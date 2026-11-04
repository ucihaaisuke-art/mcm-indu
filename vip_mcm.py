"""Menu VIP untuk bot MCM: QRIS manual dan akses sesuai pesanan."""

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import TelegramError

logger = logging.getLogger(__name__)

VIP_GROUPS = {
    "chil_vip": -1004328410781,
    "indo_premium": -1004434516169,
    "gay_lesby_acak": -5370555952,
    "full_paket": -1004377838078,
}

GREETING = (
    "Ada apa le? 😄\n"
    "Mau minta link cadangan grup atau minta video "
    "yang gue share di grup?\n\n"
    "Atau mau grup VIP INDO PREMIUM, GAY LESBY, "
    "dan VIDEO AI HD?"
)


class PaymentError(Exception):
    """Pesan kesalahan tanpa kredensial pembayaran."""


def rupiah(value):
    if isinstance(value, bool):
        raise PaymentError("Nominal pembayaran tidak valid.")

    try:
        amount = Decimal(str(value))

        if (
            not amount.is_finite()
            or amount <= 0
            or amount != amount.to_integral_value()
        ):
            raise PaymentError(
                "Nominal harus berupa rupiah utuh "
                "dan lebih dari nol."
            )

        return int(amount)

    except (InvalidOperation, ValueError, TypeError):
        raise PaymentError(
            "Nominal pembayaran tidak valid."
        ) from None


class ExistingStore:
    def __init__(self, path):
        self.connection = sqlite3.connect(
            path,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()

        with self.connection:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    group_id INTEGER,
                    product_id TEXT,
                    amount INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    charge_id TEXT UNIQUE
                );

                CREATE TABLE IF NOT EXISTS access (
                    user_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    order_id TEXT NOT NULL,
                    PRIMARY KEY (user_id, group_id)
                );

                CREATE TABLE IF NOT EXISTS invites (
                    link TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS website_access (
                    user_id INTEGER NOT NULL,
                    product_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    PRIMARY KEY (user_id, product_id)
                );
            """)

            columns = {
                row[1]
                for row in self.connection.execute(
                    "PRAGMA table_info(orders)"
                )
            }

            if "notification_attempted" not in columns:
                self.connection.execute(
                    "ALTER TABLE orders ADD COLUMN "
                    "notification_attempted INTEGER "
                    "NOT NULL DEFAULT 0"
                )

        self._upgrade_orders(path)

    def _upgrade_orders(self, path):
        columns = {
            row[1]: row
            for row in self.connection.execute(
                "PRAGMA table_info(orders)"
            )
        }

        if (
            "product_id" in columns
            and not columns["group_id"][3]
        ):
            return

        if str(path) != ":memory:":
            db_path = Path(path)

            stamp = datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%S%fZ"
            )

            backup_path = db_path.with_name(
                db_path.name
                + ".before-website-"
                + stamp
                + ".bak"
            )

            backup = sqlite3.connect(backup_path)

            try:
                self.connection.backup(backup)
            finally:
                backup.close()

        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")

            self.connection.execute("""
                CREATE TABLE orders_upgrade (
                    order_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    group_id INTEGER,
                    product_id TEXT,
                    amount INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    charge_id TEXT UNIQUE,
                    notification_attempted INTEGER
                        NOT NULL DEFAULT 0
                )
            """)

            product_column = (
                "product_id"
                if "product_id" in columns
                else "NULL"
            )

            self.connection.execute(
                "INSERT INTO orders_upgrade "
                "(order_id, user_id, group_id, product_id, "
                "amount, state, charge_id, notification_attempted) "
                "SELECT order_id, user_id, group_id, "
                + product_column
                + ", amount, state, charge_id, "
                "notification_attempted FROM orders"
            )

            self.connection.execute("DROP TABLE orders")

            self.connection.execute(
                "ALTER TABLE orders_upgrade RENAME TO orders"
            )

    def close(self):
        self.connection.close()

    def create_order(self, user_id, group_id, amount):
        if (
            not isinstance(user_id, int)
            or isinstance(user_id, bool)
            or user_id <= 0
        ):
            raise PaymentError(
                "ID pengguna Telegram tidak valid."
            )

        if (
            not isinstance(group_id, int)
            or isinstance(group_id, bool)
            or group_id >= 0
        ):
            raise PaymentError(
                "ID grup Telegram harus berupa angka negatif."
            )

        amount = rupiah(amount)
        order_id = "vip-" + uuid.uuid4().hex

        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO orders "
                "(order_id,user_id,group_id,amount) "
                "VALUES (?,?,?,?)",
                (
                    order_id,
                    user_id,
                    group_id,
                    amount,
                ),
            )

        return self.order(order_id)

    def create_website_order(
        self,
        user_id,
        product_id,
        amount,
    ):
        if (
            not isinstance(user_id, int)
            or isinstance(user_id, bool)
            or user_id <= 0
        ):
            raise PaymentError(
                "ID pengguna Telegram tidak valid."
            )

        if not isinstance(product_id, str) or not product_id:
            raise PaymentError("Produk website tidak valid.")

        amount = rupiah(amount)
        order_id = "vip-" + uuid.uuid4().hex

        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO orders "
                "(order_id,user_id,product_id,amount) "
                "VALUES (?,?,?,?)",
                (
                    order_id,
                    user_id,
                    product_id,
                    amount,
                ),
            )

        return self.order(order_id)

    def order(self, order_id):
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM orders WHERE order_id=?",
                (order_id,),
            ).fetchone()

            return dict(row) if row else None

    def has_access(self, user_id, group_id):
        with self.lock:
            return self.connection.execute(
                "SELECT 1 FROM access "
                "WHERE user_id=? AND group_id=?",
                (user_id, group_id),
            ).fetchone() is not None

    def access_orders(self, user_id):
        with self.lock:
            return [
                dict(row)
                for row in self.connection.execute(
                    "SELECT orders.* FROM access "
                    "JOIN orders USING(order_id) "
                    "WHERE access.user_id=? "
                    "UNION "
                    "SELECT orders.* FROM website_access "
                    "JOIN orders USING(order_id) "
                    "WHERE website_access.user_id=?",
                    (user_id, user_id),
                )
            ]

    def claim_notification(self, order_id):
        with self.lock, self.connection:
            return self.connection.execute(
                "UPDATE orders "
                "SET notification_attempted=1 "
                "WHERE order_id=? AND state='paid' "
                "AND notification_attempted=0",
                (order_id,),
            ).rowcount == 1

    def add_invite(self, order_id, link, expires_at):
        with self.lock, self.connection:
            order = self.order(order_id)

            if (
                order is None
                or order["state"] != "paid"
                or order["group_id"] is None
            ):
                raise PaymentError(
                    "Pesanan belum dibayar."
                )

            self.connection.execute(
                "INSERT INTO invites "
                "(link,order_id,expires_at) VALUES (?,?,?)",
                (link, order_id, expires_at),
            )

    def invite_order(self, link):
        with self.lock:
            row = self.connection.execute(
                "SELECT orders.*, invites.expires_at "
                "FROM invites JOIN orders USING(order_id) "
                "WHERE invites.link=?",
                (link,),
            ).fetchone()

            return dict(row) if row else None

    def _verified_status(
        self,
        order_id,
        state,
        charge_id,
    ):
        with self.lock, self.connection:
            order = self.order(order_id)

            if order is None:
                raise PaymentError(
                    "Pesanan tidak ditemukan."
                )

            if order["state"] == "paid":
                if (
                    state == "paid"
                    and order["charge_id"] != charge_id
                ):
                    raise PaymentError(
                        "ID transaksi tidak cocok "
                        "dengan pembayaran tersimpan."
                    )

                return self.order(order_id)

            if state == "paid":
                try:
                    self.connection.execute(
                        "UPDATE orders "
                        "SET state='paid',charge_id=? "
                        "WHERE order_id=?",
                        (charge_id, order_id),
                    )

                except sqlite3.IntegrityError:
                    raise PaymentError(
                        "ID transaksi sudah digunakan "
                        "pesanan lain."
                    ) from None

                if order["group_id"] is None:
                    if not order["product_id"]:
                        raise PaymentError(
                            "Produk pesanan tidak valid."
                        )

                    self.connection.execute(
                        "INSERT OR IGNORE INTO website_access "
                        "(user_id,product_id,order_id) "
                        "VALUES (?,?,?)",
                        (
                            order["user_id"],
                            order["product_id"],
                            order_id,
                        ),
                    )

                else:
                    self.connection.execute(
                        "INSERT OR IGNORE INTO access "
                        "(user_id,group_id,order_id) "
                        "VALUES (?,?,?)",
                        (
                            order["user_id"],
                            order["group_id"],
                            order_id,
                        ),
                    )

            else:
                self.connection.execute(
                    "UPDATE orders SET state=? "
                    "WHERE order_id=?",
                    (state, order_id),
                )

            return self.order(order_id)


class Store(ExistingStore):
    def is_manual_order(self, order_id):
        with self.lock:
            table = self.connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='manual_orders'"
            ).fetchone()

            return bool(
                table
                and self.connection.execute(
                    "SELECT 1 FROM manual_orders "
                    "WHERE order_id=?",
                    (order_id,),
                ).fetchone()
            )

    def _verified_status(
        self,
        order_id,
        state,
        charge_id,
    ):
        with self.lock:
            if self.is_manual_order(order_id):
                raise PaymentError(
                    "Pesanan manual hanya dapat dikonfirmasi "
                    "melalui reply done dari owner."
                )

            return super()._verified_status(
                order_id,
                state,
                charge_id,
            )


@dataclass(frozen=True)
class Plan:
    name: str
    amount: int
    kind: str = "group"


PLANS = {
    "chil_vip": Plan("VIDEO AI HD", 15000),
    "indo_premium": Plan("INDO PREMIUM", 30000),
    "gay_lesby_acak": Plan("GAY/LESBY", 45000),
    "full_paket": Plan("TAKE ALL GRUP", 99000),
    "ai_foto_video": Plan(
        "WEBSITE AI VIDEO/GAMBAR",
        25000,
        "website",
    ),
}


def website_settings(raw):
    url = raw.strip()

    try:
        parsed = urlsplit(url)

        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and not any(char.isspace() for char in url)
        )

    except ValueError:
        valid = False

    if not valid:
        raise PaymentError(
            "Link website AI belum siap. Hubungi admin."
        )

    return url


class _VipBase:
    def __init__(
        self,
        payments,
        store,
        groups,
        environment,
        website_url=None,
    ):
        self.payments = payments
        self.store = store
        self.groups = groups
        self.environment = environment
        self.bot = None
        self.delivery_locks = {}

        self.website_url = (
            os.environ.get("AI_WEBSITE_URL", "")
            if website_url is None
            else website_url
        )

    async def ready_group(self, group_id):
        chat = await self.bot.get_chat(group_id)

        member = await self.bot.get_chat_member(
            group_id,
            self.bot.id,
        )

        if (
            chat.type not in ("group", "supergroup")
            or chat.username
        ):
            raise PaymentError(
                "Grup VIP harus berupa grup privat."
            )

        if (
            member.status != "administrator"
            or not member.can_invite_users
        ):
            raise PaymentError(
                "Bot harus menjadi admin dengan "
                "izin mengundang anggota."
            )

    async def check_order(self, update, context):
        if update.effective_chat.type != "private":
            return

        if len(context.args) != 1:
            await update.effective_message.reply_text(
                "Gunakan /cek NOMOR_PESANAN."
            )
            return

        try:
            order = await asyncio.to_thread(
                self.payments.check,
                context.args[0],
                user_id=update.effective_user.id,
            )

            if order["state"] == "paid":
                await self.deliver(
                    order,
                    explicit=True,
                )
            else:
                await update.effective_message.reply_text(
                    "Status pembayaran: " + order["state"]
                )

        except (PaymentError, TelegramError) as error:
            logger.error(
                "Pemeriksaan VIP gagal: jenis=%s",
                type(error).__name__,
            )

            await update.effective_message.reply_text(
                "Pembayaran belum dapat diverifikasi. "
                "Periksa nomor pesanan atau hubungi admin."
            )

    async def access(self, update, context):
        if update.effective_chat.type != "private":
            return

        orders = self.store.access_orders(
            update.effective_user.id
        )

        if not orders:
            await update.effective_message.reply_text(
                "Belum ada akses VIP terverifikasi. "
                "Pilih paket melalui /start."
            )
            return

        await update.effective_message.reply_text(
            "Sedang mengambil akses yang sudah terverifikasi…"
        )

        for order in orders:
            await self.deliver(
                order,
                explicit=True,
            )

    async def deliver(
        self,
        order,
        *,
        explicit=False,
    ):
        lock = self.delivery_locks.setdefault(
            order["order_id"],
            asyncio.Lock(),
        )

        async with lock:
            order = self.store.order(order["order_id"])

            if order is None or order["state"] != "paid":
                return

            if (
                not explicit
                and not self.store.claim_notification(
                    order["order_id"]
                )
            ):
                return

            try:
                if order["product_id"] is not None:
                    plan = PLANS.get(order["product_id"])

                    if (
                        plan is None
                        or plan.kind != "website"
                    ):
                        raise PaymentError(
                            "Produk website tidak ditemukan."
                        )

                    url = website_settings(self.website_url)

                    await self.bot.send_message(
                        chat_id=order["user_id"],
                        text=(
                            f"Pembayaran terverifikasi.\n"
                            f"{plan.name}\n\n"
                            "Berikut link website AI yang kamu beli:\n"
                            + url
                            + "\n\nSimpan link ini. Gunakan /akses "
                            "untuk mengambilnya kembali."
                        ),
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                "Buka Website AI",
                                url=url,
                            )
                        ]]),
                    )
                    return

                await self.ready_group(
                    order["group_id"]
                )

                expires_at = int(time.time()) + 600

                invite = await self.bot.create_chat_invite_link(
                    chat_id=order["group_id"],
                    creates_join_request=True,
                    expire_date=datetime.fromtimestamp(
                        expires_at,
                        timezone.utc,
                    ),
                )

                self.store.add_invite(
                    order["order_id"],
                    invite.invite_link,
                    expires_at,
                )

                await self.bot.send_message(
                    chat_id=order["user_id"],
                    text=(
                        "Pembayaran terverifikasi. "
                        "Akses VIP kamu permanen.\n"
                        "Klik link lalu ajukan bergabung "
                        "menggunakan akun ini.\n"
                        "Link berlaku 10 menit; gunakan /akses "
                        "untuk meminta link baru.\n"
                        + invite.invite_link
                    ),
                )

            except (PaymentError, TelegramError) as error:
                logger.error(
                    "Pengiriman akses tertunda: jenis=%s",
                    type(error).__name__,
                )

                try:
                    await self.bot.send_message(
                        chat_id=order["user_id"],
                        text=(
                            "Pembayaran tersimpan, tetapi akses "
                            "belum dapat dikirim. Hubungi admin "
                            "dengan nomor pesanan: "
                            + order["order_id"]
                            + ". Gunakan /akses untuk mencoba lagi."
                        ),
                    )
                except TelegramError:
                    pass

    async def join(self, update, context):
        request = update.chat_join_request

        if request.invite_link is None:
            return

        order = self.store.invite_order(
            request.invite_link.invite_link
        )

        if order is None:
            return

        if (
            order["state"] == "paid"
            and order["user_id"] == request.from_user.id
            and order["group_id"] == request.chat.id
            and order["expires_at"] > time.time()
            and self.store.has_access(
                request.from_user.id,
                request.chat.id,
            )
        ):
            await self.bot.approve_chat_join_request(
                request.chat.id,
                request.from_user.id,
            )

            await self.bot.revoke_chat_invite_link(
                request.chat.id,
                request.invite_link.invite_link,
            )

        else:
            await self.bot.decline_chat_join_request(
                request.chat.id,
                request.from_user.id,
            )


class ManualPayments:
    def __init__(self, store, owner_id):
        if (
            not isinstance(owner_id, int)
            or isinstance(owner_id, bool)
            or owner_id <= 0
        ):
            raise PaymentError(
                "VIP_OWNER_ID harus berupa ID akun Telegram owner."
            )

        self.store = store
        self.owner_id = owner_id

        with store.lock, store.connection:
            store.connection.executescript("""
                CREATE TABLE IF NOT EXISTS manual_orders (
                    order_id TEXT PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    plan_id TEXT NOT NULL,
                    sender_name TEXT,
                    proof_file_id TEXT,
                    proof_kind TEXT,
                    revision INTEGER NOT NULL DEFAULT 0,
                    review_attempted INTEGER NOT NULL DEFAULT -1,
                    approved_by INTEGER,
                    approved_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS manual_qris (
                    owner_id INTEGER NOT NULL,
                    bot_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    PRIMARY KEY(owner_id, bot_id)
                );

                CREATE TABLE IF NOT EXISTS manual_prompts (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    order_id TEXT NOT NULL,
                    PRIMARY KEY(chat_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS manual_reviews (
                    owner_id INTEGER NOT NULL,
                    bot_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    order_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    PRIMARY KEY(owner_id, bot_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS manual_decisions (
                    order_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    decided_at INTEGER NOT NULL,
                    PRIMARY KEY(
                        order_id, revision, owner_id, decision
                    )
                );

                CREATE TABLE IF NOT EXISTS manual_updates (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    PRIMARY KEY(chat_id, message_id)
                );
            """)

    def qris(self, bot_id, file_id=None):
        with self.store.lock, self.store.connection:
            if file_id is not None:
                self.store.connection.execute(
                    "INSERT OR REPLACE INTO manual_qris "
                    "VALUES (?,?,?)",
                    (
                        self.owner_id,
                        bot_id,
                        file_id,
                    ),
                )

            row = self.store.connection.execute(
                "SELECT file_id FROM manual_qris "
                "WHERE owner_id=? AND bot_id=?",
                (
                    self.owner_id,
                    bot_id,
                ),
            ).fetchone()

            return row[0] if row else None

    def order(self, order_id, user_id=None):
        with self.store.lock:
            row = self.store.connection.execute(
                "SELECT orders.*, manual_orders.* "
                "FROM orders JOIN manual_orders USING(order_id) "
                "WHERE order_id=? AND owner_id=?",
                (
                    order_id,
                    self.owner_id,
                ),
            ).fetchone()

            if row is None or (
                user_id is not None
                and row["user_id"] != user_id
            ):
                raise PaymentError(
                    "Pesanan tidak ditemukan untuk akun ini."
                )

            return dict(row)

    def pending(self, user_id=None):
        with self.store.lock:
            query = (
                "SELECT orders.*, manual_orders.* "
                "FROM orders JOIN manual_orders USING(order_id) "
                "WHERE owner_id=? AND state='pending'"
            )

            if user_id is not None:
                query += " AND user_id=?"
                values = (
                    self.owner_id,
                    user_id,
                )
            else:
                values = (self.owner_id,)

            rows = self.store.connection.execute(
                query,
                values,
            ).fetchall()

            return [dict(row) for row in rows]

    def create(self, user_id, plan_id, groups):
        plan = PLANS.get(plan_id)

        if plan is None or (
            plan.kind == "group"
            and plan_id not in groups
        ):
            raise PaymentError("Paket belum tersedia.")

        if (
            not isinstance(user_id, int)
            or isinstance(user_id, bool)
            or user_id <= 0
        ):
            raise PaymentError("ID pembeli tidak valid.")

        with self.store.lock, self.store.connection:
            existing = next(
                (
                    row
                    for row in self.pending(user_id)
                    if row["plan_id"] == plan_id
                ),
                None,
            )

            if existing:
                return existing

            order_id = "vip-" + uuid.uuid4().hex

            self.store.connection.execute(
                "INSERT INTO orders "
                "(order_id,user_id,group_id,product_id,amount) "
                "VALUES (?,?,?,?,?)",
                (
                    order_id,
                    user_id,
                    (
                        groups[plan_id]
                        if plan.kind == "group"
                        else None
                    ),
                    (
                        plan_id
                        if plan.kind == "website"
                        else None
                    ),
                    plan.amount,
                ),
            )

            self.store.connection.execute(
                "INSERT INTO manual_orders "
                "(order_id,owner_id,plan_id) VALUES (?,?,?)",
                (
                    order_id,
                    self.owner_id,
                    plan_id,
                ),
            )

            return self.order(order_id, user_id)

    def check(self, order_id, *, user_id=None):
        return self.order(order_id, user_id)

    def prompt(self, chat_id, message_id, order_id):
        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "INSERT OR REPLACE INTO manual_prompts "
                "VALUES (?,?,?)",
                (
                    chat_id,
                    message_id,
                    order_id,
                ),
            )

    def resolve(self, user_id, reply_id=None):
        with self.store.lock:
            if reply_id is not None:
                row = self.store.connection.execute(
                    "SELECT order_id FROM manual_prompts "
                    "WHERE chat_id=? AND message_id=?",
                    (
                        user_id,
                        reply_id,
                    ),
                ).fetchone()

                return (
                    self.order(row[0], user_id)
                    if row
                    else None
                )

            pending = self.pending(user_id)

            return (
                pending[0]
                if len(pending) == 1
                else None
            )

    def evidence(
        self,
        order_id,
        user_id,
        message_id,
        *,
        name=None,
        proof=None,
    ):
        with self.store.lock, self.store.connection:
            order = self.order(order_id, user_id)

            if order["state"] != "pending":
                raise PaymentError(
                    "Pesanan ini sudah selesai."
                )

            inserted = self.store.connection.execute(
                "INSERT OR IGNORE INTO manual_updates "
                "VALUES (?,?)",
                (
                    user_id,
                    message_id,
                ),
            ).rowcount

            if not inserted:
                return None

            self.store.connection.execute(
                "UPDATE manual_orders SET "
                "sender_name=COALESCE(?,sender_name), "
                "proof_file_id=COALESCE(?,proof_file_id), "
                "proof_kind=COALESCE(?,proof_kind), "
                "revision=revision+1 WHERE order_id=?",
                (
                    name,
                    proof[1] if proof else None,
                    proof[0] if proof else None,
                    order_id,
                ),
            )

            return self.order(order_id, user_id)

    def claim_review(self, order_id):
        with self.store.lock, self.store.connection:
            return self.store.connection.execute(
                "UPDATE manual_orders "
                "SET review_attempted=revision "
                "WHERE order_id=? AND owner_id=? "
                "AND sender_name IS NOT NULL "
                "AND proof_file_id IS NOT NULL "
                "AND review_attempted<revision",
                (
                    order_id,
                    self.owner_id,
                ),
            ).rowcount == 1

    def add_review(
        self,
        bot_id,
        message_id,
        order,
    ):
        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "INSERT OR REPLACE INTO manual_reviews "
                "VALUES (?,?,?,?,?)",
                (
                    self.owner_id,
                    bot_id,
                    message_id,
                    order["order_id"],
                    order["revision"],
                ),
            )

    def decide(
        self,
        actor_id,
        bot_id,
        message_id,
        decision,
    ):
        if (
            actor_id != self.owner_id
            or decision not in ("done", "belum")
        ):
            raise PaymentError(
                "Konfirmasi hanya boleh dilakukan owner."
            )

        with self.store.lock, self.store.connection:
            review = self.store.connection.execute(
                "SELECT * FROM manual_reviews "
                "WHERE owner_id=? AND bot_id=? AND message_id=?",
                (
                    actor_id,
                    bot_id,
                    message_id,
                ),
            ).fetchone()

            if review is None:
                raise PaymentError(
                    "Balas pesan pemeriksaan pembayaran "
                    "yang dikirim bot."
                )

            order = self.order(review["order_id"])

            if order["state"] == "paid":
                return order, False

            if (
                order["state"] != "pending"
                or not order["sender_name"]
                or not order["proof_file_id"]
                or order["revision"] != review["revision"]
            ):
                raise PaymentError(
                    "Bukti sudah diperbarui. "
                    "Balas pesan pemeriksaan terbaru."
                )

            changed = (
                self.store.connection.execute(
                    "INSERT OR IGNORE INTO manual_decisions "
                    "VALUES (?,?,?,?,?)",
                    (
                        order["order_id"],
                        order["revision"],
                        actor_id,
                        decision,
                        int(time.time()),
                    ),
                ).rowcount
                == 1
            )

            if decision == "done" and changed:
                self.store.connection.execute(
                    "UPDATE orders "
                    "SET state='paid',charge_id=? "
                    "WHERE order_id=?",
                    (
                        "manual:" + order["order_id"],
                        order["order_id"],
                    ),
                )

                self.store.connection.execute(
                    "UPDATE manual_orders "
                    "SET approved_by=?,approved_at=? "
                    "WHERE order_id=?",
                    (
                        actor_id,
                        int(time.time()),
                        order["order_id"],
                    ),
                )

                if order["group_id"] is not None:
                    self.store.connection.execute(
                        "INSERT OR IGNORE INTO access "
                        "VALUES (?,?,?)",
                        (
                            order["user_id"],
                            order["group_id"],
                            order["order_id"],
                        ),
                    )

                else:
                    self.store.connection.execute(
                        "INSERT OR IGNORE INTO website_access "
                        "VALUES (?,?,?)",
                        (
                            order["user_id"],
                            order["product_id"],
                            order["order_id"],
                        ),
                    )

            return (
                self.order(order["order_id"]),
                changed,
            )


def sender_name(text, *, bare=False):
    match = re.search(
        r"(?:^|\n)\s*nama\s+(?:pengirim|pemilik)"
        r"\s*:\s*([^\r\n]+)",
        text or "",
        re.I,
    )

    if match:
        value = match.group(1)
    elif bare:
        value = text or ""
    else:
        value = ""

    value = " ".join(value.split()).strip()

    if (
        not 2 <= len(value) <= 100
        or value.casefold().strip(".!?")
        in ("done", "belum")
        or re.search(
            r"https?://|t\.me/",
            value,
            re.I,
        )
    ):
        return None

    return value


class ManualVipBot(_VipBase):
    async def start(self, update, context):
        if update.effective_chat.type != "private":
            return

        rows = [
            [
                InlineKeyboardButton(
                    f"{plan.name} — Rp{plan.amount:,}"
                    .replace(",", "."),
                    callback_data="vip:buy:" + plan_id,
                )
            ]
            for plan_id, plan in PLANS.items()
        ]

        await update.effective_message.reply_text(
            "Mau paket yang mana, le? 😄\n\n"
            + "\n".join(
                f"{plan.name}: Rp{plan.amount:,}"
                .replace(",", ".")
                for plan in PLANS.values()
            )
            + "\n\nAkses permanen, bayar sekali. "
            "TAKE ALL masuk ke satu grup gabungan.\n"
            "Bayar QRIS, kirim bukti dan nama pengirim, "
            "lalu tunggu owner cek uang masuk.\n"
            "Pakai /akses untuk mengambil akses yang "
            "sudah lunas. /batal untuk kembali ngobrol.",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode=None,
        )

    async def ask_proof(
        self,
        message,
        order,
        *,
        name_only=False,
    ):
        if name_only:
            text = (
                "Tulis nama pengirim yang tercantum "
                "di pembayaran, le.\n"
                "Contoh: Nama pengirim: Budi Santoso"
            )
        else:
            text = (
                "Kirim foto/screenshot bukti transfer "
                "ke sini, le, dan sertakan di caption:\n"
                "Nama pengirim: nama yang tercantum "
                "di pembayaran\n\n"
                "Bukti akan dicek owner dulu. "
                "Link dikirim setelah uang masuk "
                "dikonfirmasi."
            )

        prompt = await message.reply_text(
            f"Pesanan: {order['order_id']}\n\n" + text,
            reply_markup=ForceReply(selective=True),
            parse_mode=None,
        )

        self.payments.prompt(
            order["user_id"],
            prompt.message_id,
            order["order_id"],
        )

    async def callback(self, update, context):
        query = update.callback_query
        await query.answer()

        if (
            query.message is None
            or query.message.chat.type != "private"
        ):
            return

        parts = (query.data or "").split(":", 2)

        if len(parts) != 3:
            return

        _, action, reference = parts
        uid = query.from_user.id

        try:
            if action == "buy":
                plan = PLANS.get(reference)

                if plan is None or (
                    plan.kind == "group"
                    and reference not in self.groups
                ):
                    raise PaymentError(
                        "Paket ini belum siap. Hubungi admin."
                    )

                if plan.kind == "website":
                    website_settings(self.website_url)

                    existing = next(
                        (
                            row
                            for row in self.store.access_orders(uid)
                            if row["product_id"] == reference
                        ),
                        None,
                    )

                else:
                    await self.ready_group(
                        self.groups[reference]
                    )

                    existing = next(
                        (
                            row
                            for row in self.store.access_orders(uid)
                            if row["group_id"]
                            == self.groups[reference]
                        ),
                        None,
                    )

                if existing:
                    await self.deliver(
                        existing,
                        explicit=True,
                    )
                    return

                qr = self.payments.qris(self.bot.id)

                if not qr:
                    raise PaymentError(
                        "QRIS belum dipasang owner. "
                        "Hubungi admin dulu, le."
                    )

                order = self.payments.create(
                    uid,
                    reference,
                    self.groups,
                )

                amount = (
                    f"Rp{order['amount']:,}"
                    .replace(",", ".")
                )

                invoice = await query.message.reply_photo(
                    photo=qr,
                    parse_mode=None,
                    caption=(
                        f"{plan.name}\n"
                        f"{amount}\n"
                        f"Pesanan: {order['order_id']}\n\n"
                        "Bayar ke QRIS ini, lalu kirim "
                        "bukti transfer dan nama pengirim "
                        "ke chat ini.\n"
                        "Akses diberikan setelah owner "
                        "mengecek uang masuk dan "
                        "mengonfirmasinya."
                    ),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "Kirim bukti transfer",
                            callback_data=(
                                "vip:proof:"
                                + order["order_id"]
                            ),
                        )
                    ]]),
                )

                self.payments.prompt(
                    uid,
                    invoice.message_id,
                    order["order_id"],
                )

                await self.ask_proof(
                    query.message,
                    order,
                )

            elif action == "proof":
                order = self.payments.order(
                    reference,
                    uid,
                )

                if order["state"] == "paid":
                    await self.deliver(
                        order,
                        explicit=True,
                    )
                else:
                    await self.ask_proof(
                        query.message,
                        order,
                    )

            elif action == "check":
                order = self.payments.check(
                    reference,
                    user_id=uid,
                )

                if order["state"] == "paid":
                    await self.deliver(
                        order,
                        explicit=True,
                    )
                else:
                    await query.message.reply_text(
                        "Masih menunggu konfirmasi uang "
                        "masuk dari owner, le."
                    )

        except PaymentError as error:
            await query.message.reply_text(
                str(error),
                parse_mode=None,
            )

        except TelegramError as error:
            logger.warning(
                "Pesanan manual belum terkonfirmasi: jenis=%s",
                type(error).__name__,
            )

            await query.message.reply_text(
                "Pesanan belum selesai diproses. "
                "Kalau sudah bayar, jangan bayar ulang; "
                "hubungi owner."
            )

    async def set_qris(self, update, context):
        message = update.effective_message

        if (
            update.effective_chat.type != "private"
            or update.effective_user.id
            != self.payments.owner_id
        ):
            return

        quoted = message.reply_to_message

        if quoted is None or not quoted.photo:
            await message.reply_text(
                "Kirim gambar QRIS ke bot ini, lalu "
                "balas gambar itu dengan /setqris."
            )
            return

        self.payments.qris(
            self.bot.id,
            quoted.photo[-1].file_id,
        )

        await message.reply_text(
            "QRIS sudah disimpan. Pembayaran tetap "
            "harus lu cek dan konfirmasi sendiri, bos."
        )

    async def review(
        self,
        order,
        *,
        explicit=False,
    ):
        if (
            not order["sender_name"]
            or not order["proof_file_id"]
            or order["state"] != "pending"
        ):
            raise PaymentError(
                "Bukti transfer dan nama pengirim "
                "belum lengkap."
            )

        if (
            not explicit
            and not self.payments.claim_review(
                order["order_id"]
            )
        ):
            return

        plan = PLANS[order["plan_id"]]

        amount = (
            f"Rp{order['amount']:,}"
            .replace(",", ".")
        )

        caption = (
            "Bos, pembayaran dari atas nama "
            f"{order['sender_name']} sudah masuk belum?\n\n"
            f"Nama pengirim: {order['sender_name']}\n"
            f"Paket: {plan.name}\n"
            f"Nominal: {amount}\n"
            f"ID member: {order['user_id']}\n"
            f"Pesanan: {order['order_id']}\n\n"
            "Cek uang masuk di akun pembayaran lu.\n"
            "Balas pesan ini dengan done kalau sudah "
            "masuk, atau belum kalau belum masuk.\n"
            "Tanpa balasan done, akses tetap ditahan."
        )

        if order["proof_kind"] == "photo":
            sent = await self.bot.send_photo(
                chat_id=self.payments.owner_id,
                photo=order["proof_file_id"],
                caption=caption,
                parse_mode=None,
            )

        else:
            sent = await self.bot.send_document(
                chat_id=self.payments.owner_id,
                document=order["proof_file_id"],
                caption=caption,
                parse_mode=None,
            )

        self.payments.add_review(
            self.bot.id,
            sent.message_id,
            order,
        )

    async def owner_reply(self, message):
        decision = (
            (message.text or "")
            .strip()
            .casefold()
            .rstrip(".!?")
        )

        if decision not in ("done", "belum"):
            return

        quoted = message.reply_to_message

        if (
            quoted is None
            or quoted.from_user is None
            or quoted.from_user.id != self.bot.id
        ):
            await message.reply_text(
                "Balas pesan pemeriksaan pembayaran "
                "dari bot dengan done atau belum, bos."
            )
            return

        try:
            order, changed = self.payments.decide(
                message.from_user.id,
                self.bot.id,
                quoted.message_id,
                decision,
            )

            if not changed:
                await message.reply_text(
                    "Keputusan itu sudah tercatat. "
                    "Akses yang sudah lunas tetap aktif."
                )
                return order

            if decision == "done":
                await self.deliver(order)

                await message.reply_text(
                    "Done, bos. Pembayaran tercatat lunas "
                    "dan akses diproses. Member bisa pakai "
                    "/akses bila link belum masuk."
                )

            else:
                try:
                    await self.bot.send_message(
                        chat_id=order["user_id"],
                        text=(
                            f"Pesanan: {order['order_id']}\n"
                            "Owner belum melihat pembayaran "
                            "masuk, le. Akses masih menunggu "
                            "pengecekan. Kalau sudah bayar, "
                            "jangan bayar ulang dulu; "
                            "cek status transfernya."
                        ),
                    )

                except TelegramError as error:
                    logger.warning(
                        "Status belum masuk tetap tersimpan; "
                        "pemberitahuan member tertunda: jenis=%s",
                        type(error).__name__,
                    )

                await message.reply_text(
                    "Oke bos, tetap menunggu. Link belum "
                    "dibuka. Nanti balas done di pesan "
                    "pemeriksaan yang sama kalau sudah masuk."
                )

            return order

        except PaymentError as error:
            await message.reply_text(
                str(error),
                parse_mode=None,
            )

    async def private_message(self, update, context):
        message = update.effective_message

        if (
            message is None
            or message.chat.type != "private"
            or update.edited_message is not None
            or message.from_user is None
            or message.from_user.is_bot
        ):
            return

        if message.from_user.id == self.payments.owner_id:
            await self.owner_reply(message)
            return

        uid = message.from_user.id
        reply = message.reply_to_message

        try:
            order = self.payments.resolve(
                uid,
                reply.message_id if reply else None,
            )

            if order is None:
                await message.reply_text(
                    "Pilih paket lewat /start. Kalau ada "
                    "beberapa pesanan, tekan Kirim bukti "
                    "transfer pada pesanan yang sesuai, "
                    "lalu balas petunjuknya."
                )
                return

            proof = None

            if message.photo:
                proof = (
                    "photo",
                    message.photo[-1].file_id,
                )

            elif message.document and (
                (message.document.mime_type or "")
                .startswith("image/")
                or message.document.mime_type
                == "application/pdf"
            ):
                proof = (
                    "document",
                    message.document.file_id,
                )

            name = sender_name(
                message.text or message.caption,
                bare=bool(
                    order["proof_file_id"]
                    and not order["sender_name"]
                    and not proof
                ),
            )

            if proof is None and name is None:
                if (
                    order["proof_file_id"]
                    and order["sender_name"]
                ):
                    await message.reply_text(
                        "Bukti dan nama pengirim sudah "
                        "lengkap, le. Masih menunggu owner "
                        "cek uang masuk. Link baru dikirim "
                        "setelah owner konfirmasi."
                    )

                else:
                    await self.ask_proof(
                        message,
                        order,
                        name_only=bool(
                            order["proof_file_id"]
                        ),
                    )

                return

            order = self.payments.evidence(
                order["order_id"],
                uid,
                message.message_id,
                name=name,
                proof=proof,
            )

            if order is None:
                return

            if (
                not order["sender_name"]
                or not order["proof_file_id"]
            ):
                await self.ask_proof(
                    message,
                    order,
                    name_only=not order["sender_name"],
                )
                return

            await self.review(order)

            await message.reply_text(
                "Bukti dan nama pengirim sudah dikirim "
                "ke owner, le. Tunggu pengecekan uang "
                "masuk dulu ya. Link dikirim setelah "
                "owner konfirmasi."
            )

        except PaymentError as error:
            await message.reply_text(
                str(error),
                parse_mode=None,
            )

        except TelegramError as error:
            logger.warning(
                "Bukti pembayaran tersimpan; "
                "pemberitahuan belum terkonfirmasi: jenis=%s",
                type(error).__name__,
            )

            await message.reply_text(
                "Bukti sudah tersimpan, tetapi "
                "pemberitahuan belum terkonfirmasi. "
                "Hubungi owner dengan nomor pesanan; "
                "jangan bayar ulang."
            )

    async def pending_orders(self, update, context):
        if (
            update.effective_chat.type != "private"
            or update.effective_user.id
            != self.payments.owner_id
        ):
            return

        if context.args:
            try:
                order = self.payments.order(
                    context.args[0]
                )

                await self.review(
                    order,
                    explicit=True,
                )

            except PaymentError as error:
                await update.effective_message.reply_text(
                    str(error),
                    parse_mode=None,
                )

            return

        orders = self.payments.pending()

        if not orders:
            text = "Belum ada pesanan menunggu."

        else:
            rows = []

            for order in orders[:20]:
                name = (
                    order["sender_name"]
                    or "nama belum diisi"
                )

                proof_status = (
                    "bukti ada"
                    if order["proof_file_id"]
                    else "bukti belum ada"
                )

                rows.append(
                    f"{order['order_id']} — "
                    f"{name} — {proof_status}"
                )

            text = (
                "Pesanan menunggu "
                "(maksimal 20 ditampilkan):\n"
                + "\n".join(rows)
                + "\n\nKetik /pending NOMOR_PESANAN "
                "untuk memunculkan ulang bukti dan "
                "pertanyaan konfirmasi."
            )

        await update.effective_message.reply_text(
            text,
            parse_mode=None,
        )


class _CombinedHandlers:
    def bind_vip(self, context):
        self.vip.bot = context.bot
        self.qris_setup(context.bot.id)

    def qris_setup(self, bot_id, waiting=None):
        with self.vip_store.lock, self.vip_store.connection:
            if waiting is not None:
                self.vip_store.connection.execute(
                    "INSERT OR REPLACE INTO mcm_vip_setup "
                    "VALUES (?,?,?)",
                    (
                        self.settings.owner_id,
                        bot_id,
                        int(waiting),
                    ),
                )

            row = self.vip_store.connection.execute(
                "SELECT waiting FROM mcm_vip_setup "
                "WHERE owner_id=? AND bot_id=?",
                (
                    self.settings.owner_id,
                    bot_id,
                ),
            ).fetchone()

            self.vip_qris_waiting = bool(
                row and row[0]
            )

            return self.vip_qris_waiting

    async def vip_menu(self, update, context):
        if update.effective_chat.type != "private":
            await update.effective_message.reply_text(
                "Buka chat pribadi Emak lalu "
                "ketik /vip ya, le."
            )
            return

        self.bind_vip(context)
        self.vip_modes[update.effective_user.id] = "vip"

        await self.vip.start(update, context)

    async def vip_callback(self, update, context):
        query = update.callback_query

        if (
            query.message is None
            or query.message.chat.type != "private"
        ):
            await query.answer()
            return

        self.bind_vip(context)
        uid = query.from_user.id

        async with self.vip_private_locks.setdefault(
            uid,
            asyncio.Lock(),
        ):
            if query.data == "mcm:backup":
                await query.answer()
                self.vip_modes[uid] = "chat"

                await self.kirim_link_cadangan(
                    query.message
                )

            elif query.data == "mcm:video":
                await query.answer()
                self.vip_modes[uid] = "video"
                self.dm_waiting_video.add(uid)

                await query.message.reply_text(
                    "Boleh le, kirim link pesan videonya "
                    "dari grup MCM ke sini. "
                    "Emak ambilin yang lu pilih."
                )

            elif query.data == "mcm:vip":
                await query.answer()
                self.vip_modes[uid] = "vip"

                await self.vip.start(update, context)

            else:
                self.vip_modes[uid] = "vip"

                await self.vip.callback(update, context)

    async def vip_command(self, update, context):
        if update.effective_chat.type != "private":
            return

        self.bind_vip(context)

        command = (
            update.effective_message.text
            .split()[0]
            .split("@")[0]
            .lower()
        )

        if command == "/akses":
            await self.vip.access(update, context)

        elif command == "/cek":
            await self.vip.check_order(update, context)

        elif command == "/pending":
            await self.vip.pending_orders(update, context)

    async def vip_join(self, update, context):
        self.bind_vip(context)
        await self.vip.join(update, context)

    async def vip_qris(self, update, context):
        message = update.effective_message

        if (
            update.effective_chat.type != "private"
            or update.effective_user.id
            != self.settings.owner_id
        ):
            return

        self.bind_vip(context)

        async with self.vip_private_locks.setdefault(
            message.from_user.id,
            asyncio.Lock(),
        ):
            quoted = message.reply_to_message

            if quoted is not None and quoted.photo:
                self.vip.payments.qris(
                    context.bot.id,
                    quoted.photo[-1].file_id,
                )

                self.qris_setup(
                    context.bot.id,
                    False,
                )

                await message.reply_text(
                    "QRIS sudah disimpan, bos. "
                    "Member akan mendapat gambar ini "
                    "saat memilih paket."
                )

            else:
                self.qris_setup(
                    context.bot.id,
                    True,
                )

                await message.reply_text(
                    "Kirim foto QRIS lu sekarang, bos. "
                    "Foto berikutnya akan disimpan "
                    "untuk pembayaran VIP. "
                    "Ketik /batal untuk selesai "
                    "tanpa memasang QRIS."
                )

    async def vip_cancel(self, update, context):
        if update.effective_chat.type != "private":
            return

        uid = update.effective_user.id

        async with self.vip_private_locks.setdefault(
            uid,
            asyncio.Lock(),
        ):
            self.vip_modes[uid] = "chat"
            self.dm_waiting_video.discard(uid)

            if uid == self.settings.owner_id:
                self.qris_setup(
                    context.bot.id,
                    False,
                )

            await update.effective_message.reply_text(
                "Oke le, balik ngobrol. Ada yang "
                "mau ditanyain? Pesanan yang belum "
                "selesai tetap tersimpan."
            )


def pasang_vip_mcm(bot_class):
    if getattr(bot_class, "_vip_mcm_terpasang", False):
        return

    original_init = bot_class.__init__
    original_start = bot_class.start
    original_text = bot_class.private_text
    original_media = bot_class.private_media

    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

        if self.settings.owner_id is None:
            raise ValueError(
                "Isi MCM_OWNER_ID untuk persetujuan pembayaran VIP."
            )

        if (
            self.settings.target_group_id is None
            or self.settings.target_group_id
            in VIP_GROUPS.values()
        ):
            raise ValueError(
                "MCM_TARGET_GROUP_ID harus menunjuk grup "
                "MCM utama, terpisah dari grup VIP."
            )

        path = Path(
            os.environ.get(
                "MCM_VIP_DB",
                str(
                    Path(self.katalog.path).parent
                    / "mcm_vip.sqlite3"
                ),
            )
        )

        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.vip_store = Store(path)

        with self.vip_store.connection:
            self.vip_store.connection.execute(
                "CREATE TABLE IF NOT EXISTS mcm_vip_setup ("
                "owner_id INTEGER NOT NULL, "
                "bot_id INTEGER NOT NULL, "
                "waiting INTEGER NOT NULL, "
                "PRIMARY KEY(owner_id, bot_id))"
            )

        self.vip = ManualVipBot(
            ManualPayments(
                self.vip_store,
                self.settings.owner_id,
            ),
            self.vip_store,
            VIP_GROUPS,
            "manual",
        )

        self.vip_modes = {}
        self.vip_private_locks = {}
        self.vip_qris_waiting = False

        logger.info(
            "MCM gabungan v16 aktif: "
            "chat, video, dan VIP QRIS manual."
        )

    async def start(self, update, context):
        message = update.effective_message

        if message.chat.type != "private":
            await message.reply_text(
                "Buka chat pribadi Emak untuk link "
                "cadangan, minta video, atau paket VIP.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "Chat Emak",
                        url=(
                            f"https://t.me/{context.bot.username}"
                            "?start=menu"
                        ),
                    )
                ]]),
            )
            return

        args = getattr(context, "args", None) or []

        if args and args[0].lower() == "backup":
            return await original_start(
                self,
                update,
                context,
            )

        if args and args[0].lower() == "vip":
            return await self.vip_menu(update, context)

        uid = message.from_user.id

        self.dm_generation[uid] = (
            self.dm_generation.get(uid, 0) + 1
        )

        self.dm_history.pop(uid, None)
        self.dm_waiting_video.discard(uid)
        self.vip_modes[uid] = "chat"

        if uid == self.settings.owner_id:
            self.qris_setup(
                context.bot.id,
                False,
            )

        await message.reply_text(
            GREETING,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "Link cadangan grup",
                        callback_data="mcm:backup",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Minta video",
                        callback_data="mcm:video",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Paket VIP",
                        callback_data="mcm:vip",
                    )
                ],
            ]),
        )

    def chat_helpers():
        if __package__:
            from .chat_pribadi import (
                ASK_BACKUP,
                ASK_VIDEO,
                RELAY,
                parse_tautan,
            )
        else:
            from chat_pribadi import (
                ASK_BACKUP,
                ASK_VIDEO,
                RELAY,
                parse_tautan,
            )

        return (
            ASK_BACKUP,
            ASK_VIDEO,
            RELAY,
            parse_tautan,
        )

    async def private_text(self, update, context):
        message = update.effective_message

        if (
            message is None
            or message.chat.type != "private"
            or update.edited_message is not None
            or message.from_user is None
            or message.from_user.is_bot
        ):
            return

        uid = message.from_user.id
        self.bind_vip(context)

        async with self.vip_private_locks.setdefault(
            uid,
            asyncio.Lock(),
        ):
            backup, video, relay, parse_link = chat_helpers()
            text = message.text or ""

            if relay.match(text):
                return await original_text(
                    self,
                    update,
                    context,
                )

            if self.forbidden_request(message):
                return await original_text(
                    self,
                    update,
                    context,
                )

            if (
                uid == self.settings.owner_id
                and re.fullmatch(
                    r"(?:done|belum)[.!?]*",
                    text.strip(),
                    re.I,
                )
            ):
                result = await self.vip.owner_reply(
                    message
                )

                if (
                    result is not None
                    and result["state"] == "paid"
                ):
                    self.vip_modes[
                        result["user_id"]
                    ] = "chat"

                return

            if (
                uid == self.settings.owner_id
                and self.vip_qris_waiting
            ):
                await message.reply_text(
                    "Kirim foto QRIS ya, bos. Pakai "
                    "/batal kalau mau kembali ngobrol "
                    "atau kirim media biasa."
                )
                return

            if (
                uid != self.settings.owner_id
                and sender_name(text)
            ):
                self.vip_modes[uid] = "vip"

                return await self.vip.private_message(
                    update,
                    context,
                )

            try:
                reference = parse_link(text)
            except ValueError:
                reference = None

            if reference is not None:
                self.vip_modes[uid] = "video"

                return await original_text(
                    self,
                    update,
                    context,
                )

            if re.search(
                r"\b(?:vip|premium|paket|harga)\b|"
                r"\bvideo\s+ai\s+hd\b|"
                r"\btake\s+all\b",
                text,
                re.I,
            ):
                self.vip_modes[uid] = "vip"

                return await self.vip.start(
                    update,
                    context,
                )

            if (
                reference is not None
                or video.search(text)
                or re.search(
                    r"\bmau\b.*\bvideo\b",
                    text,
                    re.I,
                )
            ):
                self.vip_modes[uid] = "video"

                if (
                    reference is None
                    and not video.search(text)
                ):
                    self.dm_waiting_video.add(uid)

                    await message.reply_text(
                        "Boleh le, kirim link pesan video "
                        "dari grup MCM ke sini."
                    )
                    return

                return await original_text(
                    self,
                    update,
                    context,
                )

            if (
                backup.search(text)
                or text.strip().lower()
                in ("start", "star")
            ):
                self.vip_modes[uid] = "chat"

                return await original_text(
                    self,
                    update,
                    context,
                )

            if uid != self.settings.owner_id:
                reply = message.reply_to_message

                order = self.vip.payments.resolve(
                    uid,
                    reply.message_id if reply else None,
                )

                waiting_name = (
                    order
                    and order["proof_file_id"]
                    and not order["sender_name"]
                )

                explicit_name = sender_name(text)

                if (
                    self.vip_modes.get(uid) == "vip"
                    or (
                        waiting_name
                        and uid not in self.vip_modes
                    )
                    or explicit_name
                ):
                    return await self.vip.private_message(
                        update,
                        context,
                    )

            return await original_text(
                self,
                update,
                context,
            )

    async def private_media(self, update, context):
        message = update.effective_message

        if (
            message is None
            or message.chat.type != "private"
            or update.edited_message is not None
            or message.from_user is None
            or message.from_user.is_bot
        ):
            return

        self.bind_vip(context)
        uid = message.from_user.id

        async with self.vip_private_locks.setdefault(
            uid,
            asyncio.Lock(),
        ):
            if uid == self.settings.owner_id:
                caption = (
                    (message.caption or "")
                    .strip()
                    .split("@")[0]
                    .lower()
                )

                if (
                    self.vip_qris_waiting
                    or caption == "/setqris"
                ):
                    if not message.photo:
                        await message.reply_text(
                            "Kirim QRIS sebagai foto ya, bos. "
                            "/batal untuk kembali."
                        )
                        return

                    self.vip.payments.qris(
                        context.bot.id,
                        message.photo[-1].file_id,
                    )

                    self.qris_setup(
                        context.bot.id,
                        False,
                    )

                    await message.reply_text(
                        "QRIS sudah disimpan, bos. Gambar "
                        "ini akan dipakai untuk pembayaran VIP."
                    )
                    return

                document = message.document

                if document and (
                    document.file_name or ""
                ).lower().endswith((
                    ".db",
                    ".sqlite",
                    ".sqlite3",
                )):
                    await message.reply_text(
                        "Itu terlihat seperti database, bos. "
                        "Simpan sebagai arsip pribadi; kiriman "
                        "ini tidak diteruskan ke grup."
                    )
                    return

                return await original_media(
                    self,
                    update,
                    context,
                )

            reply = message.reply_to_message

            order = self.vip.payments.resolve(
                uid,
                reply.message_id if reply else None,
            )

            if (
                order is not None
                or self.vip_modes.get(uid) == "vip"
                or self.vip.payments.pending(uid)
            ):
                self.vip_modes[uid] = "vip"

                return await self.vip.private_message(
                    update,
                    context,
                )

            return await original_media(
                self,
                update,
                context,
            )

    def scope(method):
        async def scoped(self, update, context):
            chat = update.effective_chat

            if (
                chat is not None
                and chat.id
                == self.settings.target_group_id
            ):
                return await method(
                    self,
                    update,
                    context,
                )

        scoped.__name__ = method.__name__
        return scoped

    for name in (
        "moderate",
        "chatter",
        "service_notice",
        "welcome",
        "membership",
        "control",
        "remove_welcome",
    ):
        setattr(
            bot_class,
            name,
            scope(getattr(bot_class, name)),
        )

    bot_class.__init__ = initialize
    bot_class.start = start
    bot_class.private_text = private_text
    bot_class.private_media = private_media

    for name in (
        "bind_vip",
        "qris_setup",
        "vip_menu",
        "vip_callback",
        "vip_command",
        "vip_join",
        "vip_qris",
        "vip_cancel",
    ):
        setattr(
            bot_class,
            name,
            getattr(_CombinedHandlers, name),
        )

    bot_class._vip_mcm_terpasang = True
