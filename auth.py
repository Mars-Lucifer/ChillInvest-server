"""
Локальная авторизация и хранилище: консольная инициализация профиля, хеш пароля,
шифрование токена брокера, SQLite.
"""

from __future__ import annotations

import getpass
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

import bcrypt
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "vault.sqlite3"
KEY_PATH = DATA_DIR / "machine.key"

LoginValidator = re.compile(r"^[A-Za-z]{1,16}$")


def normalize_invest_token(raw: str) -> str:
    """
    Токен для gRPC: без пробелов/переносов, без префикса Bearer, без кавычек и BOM.
    SDK сам отправляет «Bearer …» в metadata (см. t_tech.invest.metadata).
    """
    s = (raw or "").strip()
    if s.startswith("\ufeff"):
        s = s.lstrip("\ufeff").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    low = s.lower()
    if low.startswith("bearer "):
        s = s[7:].strip()
    # токен — одна непрерывная строка; убираем любые пробельные символы внутри (ошибка вставки)
    s = "".join(s.split())
    return s


def _hash_password(plain: str) -> str:
    """Bcrypt-хеш (UTF-8 пароль, до 72 байт; у нас макс. 16 символов по валидации)."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def _verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_or_create_fernet() -> Fernet:
    _ensure_dirs()
    if not KEY_PATH.exists():
        KEY_PATH.write_bytes(Fernet.generate_key())
    return Fernet(KEY_PATH.read_bytes())


@dataclass
class ProfileRecord:
    login: str
    password_hash: str
    use_sandbox: bool = False


class VaultStore:
    """SQLite-хранилище приложения. Мутации из стратегии должны вызываться только после проверки avaria_stop снаружи или через halted callback."""

    def __init__(
        self,
        db_path: Path = DB_PATH,
        halted: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._db_path = db_path
        self._halted = halted or (lambda: False)
        _ensure_dirs()
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    login TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    use_sandbox INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS secrets (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    encrypted_token BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    duration_end_unix INTEGER,
                    mode TEXT NOT NULL,
                    profit_withdraw_percent REAL NOT NULL,
                    account_id TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    uid TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    figi TEXT NOT NULL,
                    ticker TEXT,
                    net_yield REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS price_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    figi TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    avg_buy_price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    logged_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tax_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    preliminary_tax REAL NOT NULL DEFAULT 0,
                    tax_limit REAL NOT NULL DEFAULT 50000,
                    last_harvest_unix INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS operations_cache (
                    op_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    op_date TEXT NOT NULL
                );
                """
            )
            cur = c.execute("SELECT COUNT(*) FROM tax_state")
            if cur.fetchone()[0] == 0:
                c.execute("INSERT INTO tax_state (id) VALUES (1)")
            self._migrate_profile_use_sandbox(c)

    def _migrate_profile_use_sandbox(self, c: sqlite3.Connection) -> None:
        cols = [r[1] for r in c.execute("PRAGMA table_info(profile)").fetchall()]
        if cols and "use_sandbox" not in cols:
            c.execute("ALTER TABLE profile ADD COLUMN use_sandbox INTEGER NOT NULL DEFAULT 0")

    # --- Профиль / токен (консоль и API не используют halted для первичной настройки) ---

    def has_profile(self) -> bool:
        with self._connect() as c:
            r = c.execute("SELECT 1 FROM profile WHERE id=1").fetchone()
            return r is not None

    def save_profile(self, login: str, password_plain: str, api_token: str, use_sandbox: bool = False) -> None:
        f = _load_or_create_fernet()
        api_token = normalize_invest_token(api_token)
        if not api_token:
            raise ValueError("Пустой токен Invest API после нормализации.")
        enc = f.encrypt(api_token.encode("utf-8"))
        ph = _hash_password(password_plain)
        sb = 1 if use_sandbox else 0
        with self._connect() as c:
            c.execute("DELETE FROM profile")
            c.execute("DELETE FROM secrets")
            c.execute(
                "INSERT INTO profile (id, login, password_hash, use_sandbox) VALUES (1, ?, ?, ?)",
                (login, ph, sb),
            )
            c.execute(
                "INSERT INTO secrets (id, encrypted_token) VALUES (1, ?)",
                (enc,),
            )

    def get_profile(self) -> Optional[ProfileRecord]:
        with self._connect() as c:
            row = c.execute(
                "SELECT login, password_hash, use_sandbox FROM profile WHERE id=1"
            ).fetchone()
            if not row:
                return None
            try:
                sb = bool(row["use_sandbox"])
            except (KeyError, IndexError):
                sb = False
            return ProfileRecord(login=row["login"], password_hash=row["password_hash"], use_sandbox=sb)

    def verify_password(self, password_plain: str) -> bool:
        pr = self.get_profile()
        if not pr:
            return False
        return _verify_password(password_plain, pr.password_hash)

    def get_decrypted_token(self) -> str:
        f = _load_or_create_fernet()
        with self._connect() as c:
            row = c.execute(
                "SELECT encrypted_token FROM secrets WHERE id=1"
            ).fetchone()
            if not row:
                raise RuntimeError("Токен не найден. Выполните инициализацию: python auth.py")
        return normalize_invest_token(f.decrypt(row["encrypted_token"]).decode("utf-8"))

    def get_use_sandbox(self) -> bool:
        pr = self.get_profile()
        return bool(pr.use_sandbox) if pr else False

    def set_use_sandbox(self, value: bool) -> None:
        if self._halted():
            return
        sb = 1 if value else 0
        with self._connect() as c:
            c.execute("UPDATE profile SET use_sandbox=? WHERE id=1", (sb,))

    # --- Настройки стратегии ---

    def save_settings(
        self,
        duration_end_unix: Optional[int],
        mode: str,
        profit_withdraw_percent: float,
        account_id: Optional[str],
        updated_at_iso: str,
    ) -> None:
        if self._halted():
            return
        with self._connect() as c:
            c.execute("DELETE FROM settings")
            c.execute(
                """INSERT INTO settings (id, duration_end_unix, mode, profit_withdraw_percent, account_id, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?)""",
                (duration_end_unix, mode, profit_withdraw_percent, account_id, updated_at_iso),
            )

    def get_settings_row(self) -> Optional[sqlite3.Row]:
        with self._connect() as c:
            return c.execute("SELECT * FROM settings WHERE id=1").fetchone()

    # --- Избранное ---

    def replace_favorites(self, rows: list[tuple[str, str, str, Optional[str], float, str]]) -> None:
        if self._halted():
            return
        with self._connect() as c:
            c.execute("DELETE FROM favorites")
            c.executemany(
                """INSERT INTO favorites (uid, kind, figi, ticker, net_yield, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )

    def list_favorites(self) -> list[sqlite3.Row]:
        with self._connect() as c:
            return list(c.execute("SELECT * FROM favorites ORDER BY net_yield DESC"))

    def prune_favorites_older_than(self, before_iso: str) -> None:
        if self._halted():
            return
        with self._connect() as c:
            c.execute("DELETE FROM favorites WHERE created_at < ?", (before_iso,))

    # --- Логи цен / налоги ---

    def append_price_log(
        self, figi: str, bucket: str, avg_buy_price: float, quantity: float, logged_at_iso: str
    ) -> None:
        if self._halted():
            return
        with self._connect() as c:
            c.execute(
                """INSERT INTO price_logs (figi, bucket, avg_buy_price, quantity, logged_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (figi, bucket, avg_buy_price, quantity, logged_at_iso),
            )

    def latest_price_log(self, figi: str) -> Optional[sqlite3.Row]:
        with self._connect() as c:
            return c.execute(
                """SELECT * FROM price_logs WHERE figi=? ORDER BY id DESC LIMIT 1""",
                (figi,),
            ).fetchone()

    def add_preliminary_tax(self, amount: float) -> None:
        if self._halted():
            return
        with self._connect() as c:
            c.execute(
                "UPDATE tax_state SET preliminary_tax = preliminary_tax + ? WHERE id=1",
                (amount,),
            )

    def get_tax_state(self) -> sqlite3.Row:
        with self._connect() as c:
            return c.execute("SELECT * FROM tax_state WHERE id=1").fetchone()

    def reset_preliminary_tax(self) -> None:
        if self._halted():
            return
        with self._connect() as c:
            c.execute("UPDATE tax_state SET preliminary_tax = 0 WHERE id=1")

    def set_last_harvest_unix(self, ts: int) -> None:
        if self._halted():
            return
        with self._connect() as c:
            c.execute("UPDATE tax_state SET last_harvest_unix=? WHERE id=1", (ts,))

    def set_tax_limit(self, limit: float) -> None:
        if self._halted():
            return
        with self._connect() as c:
            c.execute("UPDATE tax_state SET tax_limit=? WHERE id=1", (limit,))

    # --- Кэш операций ---

    def upsert_operations(self, rows: list[tuple[str, str, str]]) -> None:
        if self._halted():
            return
        with self._connect() as c:
            c.executemany(
                """INSERT INTO operations_cache (op_id, payload, op_date) VALUES (?,?,?)
                   ON CONFLICT(op_id) DO UPDATE SET payload=excluded.payload, op_date=excluded.op_date""",
                rows,
            )

    def list_operations_cache(self) -> list[sqlite3.Row]:
        with self._connect() as c:
            return list(
                c.execute("SELECT * FROM operations_cache ORDER BY op_date DESC LIMIT 5000")
            )


def validate_login(login: str) -> None:
    if not LoginValidator.match(login):
        raise ValueError(
            "Логин: до 16 символов, только английские буквы, без пробелов."
        )


def validate_password(pw: str) -> None:
    if not (8 <= len(pw) <= 16):
        raise ValueError("Пароль: от 8 до 16 символов.")


def prompt_and_save_profile(store: VaultStore) -> None:
    """Интерактивное создание профиля (консоль). Вызывается при первом старте main или вручную."""
    if store.has_profile():
        return
    print("=== ChillInvest: первичная инициализация ===")
    login = input("Логин (латиница, до 16): ").strip()
    validate_login(login)
    pw = getpass.getpass("Пароль (8–16 символов): ")
    validate_password(pw)
    pw2 = getpass.getpass("Повтор пароля: ")
    if pw != pw2:
        raise SystemExit("Пароли не совпадают.")
    ans = input("Песочница T-Invest (токен только для sandbox)? [y/N]: ").strip().lower()
    use_sandbox = ans in ("y", "yes", "д", "да", "1")
    token = getpass.getpass(
        "Токен T-Invest API (только значение токена, без «Bearer» и без кавычек): "
    ).strip()
    if not token:
        raise SystemExit("Пустой токен.")
    store.save_profile(login, pw, token, use_sandbox=use_sandbox)
    print("Профиль и зашифрованный токен сохранены в", DB_PATH)


def console_create_profile() -> None:
    """Точка входа `python auth.py` — создаёт профиль, если его ещё нет."""
    store = VaultStore(halted=lambda: False)
    if store.has_profile():
        raise SystemExit("Профиль уже существует. Удалите data/vault.sqlite3 для сброса (осторожно).")
    prompt_and_save_profile(store)


if __name__ == "__main__":
    console_create_profile()
