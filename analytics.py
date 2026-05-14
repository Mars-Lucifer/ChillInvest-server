"""
REST-обработчики, сводки портфеля, синхронизация операций, экстренный стоп.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from t_tech.invest.exceptions import UnauthenticatedError

import strategy
from auth import VaultStore
from strategy import BrokerFacade, adapt_operation

logger = logging.getLogger(__name__)

http_basic = HTTPBasic()


def _invest_token_rejected() -> HTTPException:
    return HTTPException(
        status.HTTP_400_BAD_REQUEST,
        detail=(
            "Брокер отклонил токен Invest API (UNAUTHENTICATED, код 40003). "
            "Проверьте соответствие: токен **песочницы** только при включённой песочнице (вопрос при первом запуске "
            "или поле use_sandbox в POST /settings). Боевой токен — только для боевого контура. "
            "Токен выпускается в Т‑Банк → Инвестиции → Настройки; вставляйте строку без «Bearer» и кавычек."
        ),
    )


def make_vault() -> VaultStore:
    return VaultStore(halted=strategy.is_halted)


def require_profile(vault: VaultStore) -> None:
    if not vault.has_profile():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Профиль не инициализирован. Перезапустите сервер в интерактивном терминале или выполните: python auth.py",
        )


def verify_user(vault: VaultStore, credentials: HTTPBasicCredentials) -> None:
    require_profile(vault)
    if credentials is None or not vault.verify_password(credentials.password):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Неверная авторизация",
            headers={"WWW-Authenticate": "Basic"},
        )
    pr = vault.get_profile()
    if not pr or pr.login != credentials.username:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин",
            headers={"WWW-Authenticate": "Basic"},
        )


class SettingsPayload(BaseModel):
    """Параметры стратегии с /settings."""

    duration: Optional[datetime] = Field(
        default=None,
        description="Дата окончания работы цикла; null — бесконечно",
    )
    mode: Literal["static", "adaptive"]
    profit_withdraw_percent: float = Field(ge=0.0, le=100.0)
    use_sandbox: Optional[bool] = Field(
        default=None,
        description="Если задано: true — токен и запросы к песочнице (sandbox-invest-public-api), false — прод",
    )


def _resolve_account_id(vault: VaultStore) -> str:
    if strategy.is_halted():
        return ""
    token = vault.get_decrypted_token()
    use_sb = vault.get_use_sandbox()
    try:
        br = BrokerFacade(token, "", use_sandbox=use_sb)
        accs = br.list_accounts()
    except UnauthenticatedError:
        raise _invest_token_rejected() from None
    if not accs:
        return ""
    return accs[0].id


async def handle_settings(body: SettingsPayload, vault: VaultStore, credentials: HTTPBasicCredentials):
    verify_user(vault, credentials)
    strategy.set_avaria_stop(False)
    if body.use_sandbox is not None:
        vault.set_use_sandbox(body.use_sandbox)
    account_id = _resolve_account_id(vault)
    end_unix = int(body.duration.timestamp()) if body.duration else None
    now = datetime.now(timezone.utc).isoformat()
    vault.save_settings(end_unix, body.mode, body.profit_withdraw_percent, account_id, now)
    eng = strategy.get_engine(vault, vault.get_decrypted_token)
    await eng.start()
    return {"ok": True, "account_id": account_id, "started": True, "use_sandbox": vault.get_use_sandbox()}


async def handle_emergency_stop(vault: VaultStore, credentials: HTTPBasicCredentials) -> Dict[str, Any]:
    verify_user(vault, credentials)
    token = vault.get_decrypted_token()
    row = vault.get_settings_row()
    account_id = row["account_id"] if row and row["account_id"] else ""
    br = BrokerFacade(token, account_id or "", use_sandbox=vault.get_use_sandbox())
    br.cancel_all_orders(ignore_halt=True)
    strategy.set_avaria_stop(True)
    await strategy.halt_strategy_engine()
    return {"ok": True, "avaria_stop": True}


def sync_operations_cache(vault: VaultStore) -> None:
    if strategy.is_halted():
        return
    token = vault.get_decrypted_token()
    row = vault.get_settings_row()
    account_id = row["account_id"] if row and row["account_id"] else ""
    br = BrokerFacade(token, account_id or "", use_sandbox=vault.get_use_sandbox())
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365 * 3)
    try:
        ops = br.operations_range(start, end)
    except UnauthenticatedError:
        raise _invest_token_rejected() from None
    rows: List[tuple[str, str, str]] = []
    for op in ops:
        if strategy.is_halted():
            return
        a = adapt_operation(op)
        payload = {
            "id": a.id,
            "figi": a.figi,
            "type": a.operation.name,
            "status": a.status.name,
            "date": str(a.date),
            "payment": float(a.payment.value),
            "currency": a.currency.value,
        }
        rows.append((a.id, json.dumps(payload, ensure_ascii=False), str(a.date)))
    vault.upsert_operations(rows)


def status_main(vault: VaultStore, credentials: HTTPBasicCredentials) -> Dict[str, Any]:
    verify_user(vault, credentials)
    token = vault.get_decrypted_token()
    row = vault.get_settings_row()
    account_id = row["account_id"] if row and row["account_id"] else ""
    br = BrokerFacade(token, account_id or "", use_sandbox=vault.get_use_sandbox())
    try:
        total, _ = br.portfolio_rub_value()
        div = br.diversification()
        month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = datetime.now(timezone.utc)
        ops = br.operations_range(month_start, end)
    except UnauthenticatedError:
        raise _invest_token_rejected() from None
    month_pnl = 0.0
    for op in ops:
        if strategy.is_halted():
            break
        a = adapt_operation(op)
        month_pnl += float(a.payment.value)
    return {
        "portfolio_rub": round(total, 2),
        "month_income_rub": round(month_pnl, 2),
        "diversification": div,
    }


def status_analytics(vault: VaultStore, credentials: HTTPBasicCredentials) -> Dict[str, Any]:
    verify_user(vault, credentials)
    sync_operations_cache(vault)
    token = vault.get_decrypted_token()
    row = vault.get_settings_row()
    account_id = row["account_id"] if row and row["account_id"] else ""
    br = BrokerFacade(token, account_id or "", use_sandbox=vault.get_use_sandbox())
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        ops_month = br.operations_range(month_start, now)
        payouts = 0.0
        for op in ops_month:
            if strategy.is_halted():
                break
            a = adapt_operation(op)
            if "COUPON" in a.operation.name.upper() or "DIVIDEND" in a.operation.name.upper():
                payouts += float(a.payment.value)
        upcoming: List[Dict[str, Any]] = []
        settings = vault.get_settings_row()
        wd_pct = float(settings["profit_withdraw_percent"]) if settings else 0.0
        total, _ = br.portfolio_rub_value()
        reserved = round(total * wd_pct / 100.0, 2)
        all_ops = br.operations_range(now - timedelta(days=365 * 5), now)
        all_pnl = sum(float(adapt_operation(o).payment.value) for o in all_ops if not strategy.is_halted())
        month_pnl = sum(float(adapt_operation(o).payment.value) for o in ops_month if not strategy.is_halted())
    except UnauthenticatedError:
        raise _invest_token_rejected() from None
    tax_row = vault.get_tax_state()
    cached = []
    for r in vault.list_operations_cache():
        try:
            cached.append(json.loads(r["payload"]))
        except (json.JSONDecodeError, KeyError):
            continue
    return {
        "month_payouts_rub": round(payouts, 2),
        "upcoming_payments": upcoming,
        "return_all_time_rub": round(all_pnl, 2),
        "return_month_rub": round(month_pnl, 2),
        "withdraw_reserve_rub": reserved,
        "preliminary_tax_rub": float(tax_row["preliminary_tax"]),
        "operations_history": cached,
    }
