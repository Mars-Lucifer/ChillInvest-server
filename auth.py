import getpass
import re
import sqlite3
from typing import Any

import bcrypt
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app_state import BASE_DIR, check_avaria, get_logger, log_execution

logger = get_logger(__name__)
security = HTTPBasic()

DATABASE_PATH = BASE_DIR / "database.db"
KEY_PATH = BASE_DIR / ".key"


@log_execution
def get_db_connection() -> sqlite3.Connection:
    logger.info("Подключение к БД: %s", DATABASE_PATH)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


@log_execution
def initialize_database() -> None:
    connection = get_db_connection()
    try:
        logger.info("Начало транзакции initialize_database")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login TEXT NOT NULL UNIQUE,
                password_hash BLOB NOT NULL,
                encrypted_token BLOB NOT NULL,
                sandbox_mode INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                figi TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                pure_yield REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                figi TEXT NOT NULL,
                purchase_price REAL NOT NULL,
                date TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                target_date TEXT,
                infinite_run INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL,
                profit_reserve_percent REAL NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                started_by TEXT NOT NULL
            )
            """
        )
        connection.commit()
        logger.info("Транзакция initialize_database успешно завершена")
    finally:
        connection.close()


@log_execution
def has_users() -> bool:
    connection = get_db_connection()
    try:
        logger.info("Начало транзакции has_users")
        row = connection.execute("SELECT EXISTS(SELECT 1 FROM users LIMIT 1)").fetchone()
        logger.info("Транзакция has_users успешно завершена")
        return bool(row[0]) if row is not None else False
    finally:
        connection.close()


@log_execution
def load_key() -> bytes:
    if not KEY_PATH.exists():
        key = Fernet.generate_key()
        KEY_PATH.write_bytes(key)
        logger.info("Создан новый файл ключа: %s", KEY_PATH)
        return key

    key = KEY_PATH.read_bytes()
    logger.info("Ключ шифрования загружен из файла")
    return key


@log_execution
def get_cipher() -> Fernet:
    return Fernet(load_key())


@log_execution
def validate_login(login: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]{1,16}", login))


@log_execution
def validate_password(password: str) -> bool:
    return 8 <= len(password) <= 16


@log_execution
def hash_password(password: str) -> bytes:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())


@log_execution
def verify_password(password: str, password_hash: bytes) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash)


@log_execution
def encrypt_token(token: str) -> bytes:
    return get_cipher().encrypt(token.encode("utf-8"))


@log_execution
def decrypt_token(encrypted_token: bytes) -> str:
    return get_cipher().decrypt(encrypted_token).decode("utf-8")


@log_execution
def get_user_by_login(login: str) -> sqlite3.Row | None:
    connection = get_db_connection()
    try:
        logger.info("Начало транзакции get_user_by_login")
        row = connection.execute(
            """
            SELECT id, login, password_hash, encrypted_token, sandbox_mode
            FROM users
            WHERE login = ?
            """,
            (login,),
        ).fetchone()
        logger.info("Транзакция get_user_by_login успешно завершена")
        return row
    finally:
        connection.close()


@log_execution
def create_user(login: str, password: str, token: str, sandbox_mode: bool) -> None:
    connection = get_db_connection()
    try:
        password_hash = hash_password(password)
        encrypted_token = encrypt_token(token)
        logger.info("Начало транзакции create_user")
        try:
            connection.execute(
                """
                INSERT INTO users (login, password_hash, encrypted_token, sandbox_mode)
                VALUES (?, ?, ?, ?)
                """,
                (login, password_hash, encrypted_token, int(sandbox_mode)),
            )
            connection.commit()
            logger.info("Транзакция create_user успешно завершена")
        except sqlite3.IntegrityError as error:
            logger.error("Пользователь с логином %s уже существует", login)
            raise ValueError("User already exists") from error
    finally:
        connection.close()


@check_avaria
@log_execution
def get_api_token(login: str) -> str | None:
    user = get_user_by_login(login)
    if user is None:
        raise ValueError(f"Пользователь {login} не найден")
    return decrypt_token(user["encrypted_token"])


@log_execution
def get_user_mode(login: str) -> bool:
    user = get_user_by_login(login)
    if user is None:
        raise ValueError(f"Пользователь {login} не найден")
    return bool(user["sandbox_mode"])


@log_execution
def authenticate_user(login: str, password: str) -> dict[str, Any]:
    user = get_user_by_login(login)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return {
        "id": user["id"],
        "login": user["login"],
        "sandbox_mode": bool(user["sandbox_mode"]),
    }


@log_execution
def verify_credentials(
    credentials: HTTPBasicCredentials = Depends(security),
) -> dict[str, Any]:
    return authenticate_user(credentials.username, credentials.password)


@log_execution
def prompt_login() -> str:
    while True:
        login = input("Введите логин: ").strip()
        if validate_login(login):
            return login
        logger.error(
            "Некорректный логин. Допустимы только латинские буквы без пробелов, до 16 символов."
        )


@log_execution
def prompt_password() -> str:
    while True:
        password = getpass.getpass("Введите пароль: ").strip()
        if validate_password(password):
            return password
        logger.error("Некорректный пароль. Допустима длина от 8 до 16 символов.")


@log_execution
def prompt_sandbox_mode() -> bool:
    while True:
        value = input("Использовать песочницу? [y/n]: ").strip().lower()
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        logger.error("Некорректный режим. Введите y или n.")


@log_execution
def prompt_token() -> str:
    while True:
        token = getpass.getpass("Введите T-Invest API токен: ").strip()
        if token:
            return token
        logger.error("Токен не может быть пустым.")


@log_execution
def run_setup() -> None:
    initialize_database()
    login = prompt_login()
    password = prompt_password()
    sandbox_mode = prompt_sandbox_mode()
    token = prompt_token()
    create_user(login, password, token, sandbox_mode)
    logger.info("Первичная настройка завершена для пользователя %s", login)


@log_execution
def ensure_initial_setup() -> None:
    initialize_database()
    if has_users():
        logger.info("Пользователь уже существует, первичная настройка не требуется")
        return
    logger.info("Пользователи не найдены, запускается первичная настройка")
    run_setup()


if __name__ == "__main__":
    run_setup()
