"""
Точка входа FastAPI: маршруты и жизненный цикл приложения.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import sys
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.security import HTTPBasicCredentials

from auth import prompt_and_save_profile
from analytics import (
    SettingsPayload,
    handle_emergency_stop,
    handle_settings,
    http_basic,
    make_vault,
    status_analytics,
    status_main,
)
import strategy


def configure_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )
    else:
        root.setLevel(logging.INFO)


configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    vault = make_vault()
    if not vault.has_profile():
        if not sys.stdin.isatty():
            print(
                "Ошибка: нет сохранённого профиля, а stdin не интерактивный.\n"
                "Запустите сервер в терминале (python main.py) для ввода логина/пароля/токена "
                "или выполните: python auth.py",
                file=sys.stderr,
            )
            raise SystemExit(2)
        prompt_and_save_profile(vault)
    app.state.vault = vault
    logger.info(
        "ChillInvest server started: sandbox=%s profile_present=%s",
        vault.get_use_sandbox(),
        vault.has_profile(),
    )
    settings_row = vault.get_settings_row()
    if settings_row:
        strategy.set_avaria_stop(False)
        engine = strategy.get_engine(vault, vault.get_decrypted_token)
        await engine.start()
        logger.info(
            "Autostart strategy from saved settings: mode=%s updated_at=%s sandbox=%s",
            settings_row["mode"],
            settings_row["updated_at"],
            vault.get_use_sandbox(),
        )
    else:
        logger.info("Autostart skipped: strategy settings not found, waiting for POST /settings")
    yield


app = FastAPI(title="ChillInvest Server", version="0.1.0", lifespan=lifespan)


@app.post("/settings")
async def post_settings(
    body: SettingsPayload,
    credentials: HTTPBasicCredentials = Depends(http_basic),
) -> Any:
    return await handle_settings(body, app.state.vault, credentials)


@app.post("/emergency_stop")
async def post_emergency_stop(
    credentials: HTTPBasicCredentials = Depends(http_basic),
) -> Any:
    return await handle_emergency_stop(app.state.vault, credentials)


@app.get("/status/main")
async def get_status_main(
    credentials: HTTPBasicCredentials = Depends(http_basic),
) -> Any:
    return status_main(app.state.vault, credentials)


@app.get("/status/analytics")
async def get_status_analytics(
    credentials: HTTPBasicCredentials = Depends(http_basic),
) -> Any:
    return status_analytics(app.state.vault, credentials)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
