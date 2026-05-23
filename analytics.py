from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException

from app_state import get_logger, log_execution
from auth import get_db_connection
from strategy import (
    DECIMAL_ZERO,
    PROFIT_TAX_RATE,
    TinkoffClient,
    coalesce_attr,
    get_favorites_from_db,
    get_strategy_settings,
    normalize_date,
    quantize_money,
    resolve_position_asset_type,
    to_decimal,
    utc_now,
)

logger = get_logger(__name__)
MONTH_START_FALLBACK_GROWTH = Decimal("0")


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return DECIMAL_ZERO
    return quantize_money(Decimal(str(value)))


def _month_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = now or utc_now()
    month_start = datetime.combine(
        current.date().replace(day=1),
        time.min,
        tzinfo=timezone.utc,
    )
    next_month_anchor = (month_start + timedelta(days=32)).replace(day=1)
    month_end = datetime.combine(next_month_anchor.date(), time.min, tzinfo=timezone.utc)
    return month_start, month_end


def _serialize_money(value: Decimal) -> float:
    return float(quantize_money(value))


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _build_diversification(
    positions: list[dict[str, Any]],
    favorites: list[dict[str, Any]],
    gold_figis: set[str],
) -> list[float]:
    buckets = {"ofz": DECIMAL_ZERO, "corp_bond": DECIMAL_ZERO, "gold": DECIMAL_ZERO}
    total = DECIMAL_ZERO
    for position in positions:
        asset_type = resolve_position_asset_type(position, favorites, gold_figis)
        if asset_type not in buckets:
            continue
        value = _to_decimal(position["position_value"])
        buckets[asset_type] += value
        total += value
    if total <= DECIMAL_ZERO:
        return [0.0, 0.0, 0.0]
    return [
        _serialize_money(buckets["ofz"] / total * Decimal("100")),
        _serialize_money(buckets["corp_bond"] / total * Decimal("100")),
        _serialize_money(buckets["gold"] / total * Decimal("100")),
    ]


@log_execution
def get_financial_reserves() -> dict[str, float]:
    connection = get_db_connection()
    try:
        row = connection.execute(
            """
            SELECT virtual_tax_pool, user_withdrawal_pool
            FROM financial_reserves
            WHERE id = 1
            """
        ).fetchone()
        if row is None:
            return {
                "virtual_tax_pool": 0.0,
                "user_withdrawal_pool": 0.0,
            }
        return {
            "virtual_tax_pool": float(row["virtual_tax_pool"]),
            "user_withdrawal_pool": float(row["user_withdrawal_pool"]),
        }
    finally:
        connection.close()


def _update_financial_reserves(
    connection: Any,
    virtual_tax_pool: Decimal,
    user_withdrawal_pool: Decimal,
) -> None:
    connection.execute(
        """
        INSERT INTO financial_reserves (
            id,
            virtual_tax_pool,
            user_withdrawal_pool,
            updated_at
        )
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            virtual_tax_pool = excluded.virtual_tax_pool,
            user_withdrawal_pool = excluded.user_withdrawal_pool,
            updated_at = excluded.updated_at
        """,
        (
            _serialize_money(virtual_tax_pool),
            _serialize_money(user_withdrawal_pool),
            utc_now().isoformat(),
        ),
    )


@log_execution
def register_closed_position(
    profit: float,
    is_coupon: bool,
    event_date: datetime | None = None,
    title: str | None = None,
    external_id: str | None = None,
) -> dict[str, float]:
    settings = get_strategy_settings()
    withdraw_percent = Decimal(
        str((settings or {}).get("profit_withdraw_percent", 0))
    )
    gross_profit = _to_decimal(profit)
    tax_reserved = DECIMAL_ZERO
    if gross_profit > DECIMAL_ZERO and not is_coupon:
        tax_reserved = quantize_money(gross_profit * PROFIT_TAX_RATE)
    net_profit = quantize_money(gross_profit - tax_reserved)
    withdraw_reserved = DECIMAL_ZERO
    if net_profit > DECIMAL_ZERO and withdraw_percent > DECIMAL_ZERO:
        withdraw_reserved = quantize_money(net_profit * withdraw_percent / Decimal("100"))
    reinvest_amount = quantize_money(net_profit - withdraw_reserved)

    connection = get_db_connection()
    try:
        current = connection.execute(
            """
            SELECT virtual_tax_pool, user_withdrawal_pool
            FROM financial_reserves
            WHERE id = 1
            """
        ).fetchone()
        current_tax_pool = _to_decimal(current["virtual_tax_pool"] if current else 0)
        current_withdraw_pool = _to_decimal(current["user_withdrawal_pool"] if current else 0)

        new_tax_pool = quantize_money(current_tax_pool + tax_reserved)
        new_withdraw_pool = quantize_money(current_withdraw_pool + withdraw_reserved)
        _update_financial_reserves(connection, new_tax_pool, new_withdraw_pool)

        effective_event_date = (event_date or utc_now()).isoformat()
        connection.execute(
            """
            INSERT INTO financial_events (
                external_id,
                event_kind,
                title,
                gross_profit,
                tax_reserved,
                net_profit,
                withdraw_reserved,
                reinvest_amount,
                is_coupon,
                event_date,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                external_id,
                "coupon" if is_coupon else "closed_position",
                title,
                _serialize_money(gross_profit),
                _serialize_money(tax_reserved),
                _serialize_money(net_profit),
                _serialize_money(withdraw_reserved),
                _serialize_money(reinvest_amount),
                int(is_coupon),
                effective_event_date,
                utc_now().isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    return {
        "gross_profit": _serialize_money(gross_profit),
        "tax_reserved": _serialize_money(tax_reserved),
        "net_profit": _serialize_money(net_profit),
        "withdraw_reserved": _serialize_money(withdraw_reserved),
        "reinvest_amount": _serialize_money(reinvest_amount),
    }


@log_execution
def record_user_withdrawal(amount: float) -> dict[str, float]:
    withdrawal_amount = _to_decimal(amount)
    if withdrawal_amount <= DECIMAL_ZERO:
        raise ValueError("Withdrawal amount must be positive")

    connection = get_db_connection()
    try:
        current = connection.execute(
            """
            SELECT user_withdrawal_pool, virtual_tax_pool
            FROM financial_reserves
            WHERE id = 1
            """
        ).fetchone()
        current_withdraw_pool = _to_decimal(current["user_withdrawal_pool"] if current else 0)
        current_tax_pool = _to_decimal(current["virtual_tax_pool"] if current else 0)
        if withdrawal_amount > current_withdraw_pool:
            raise ValueError("Withdrawal amount exceeds available user withdrawal pool")

        new_withdraw_pool = quantize_money(current_withdraw_pool - withdrawal_amount)
        _update_financial_reserves(connection, current_tax_pool, new_withdraw_pool)
        connection.execute(
            """
            INSERT INTO financial_events (
                external_id,
                event_kind,
                title,
                gross_profit,
                tax_reserved,
                net_profit,
                withdraw_reserved,
                reinvest_amount,
                is_coupon,
                event_date,
                created_at
            )
            VALUES (?, ?, ?, 0, 0, 0, ?, 0, 0, ?, ?)
            """,
            (
                None,
                "withdrawal",
                "Реальный вывод пользователем",
                _serialize_money(-withdrawal_amount),
                utc_now().isoformat(),
                utc_now().isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    return {
        "user_withdrawal_pool": _serialize_money(new_withdraw_pool),
    }


@log_execution
def sync_coupon_income_events(client: TinkoffClient) -> int:
    operations = client.get_operations_by_cursor(limit=1000)
    imported = 0
    for operation in operations:
        operation_type = str(
            coalesce_attr(operation, "type", "operation_type", default="")
        ).upper()
        if "COUPON" not in operation_type:
            continue

        external_id = str(coalesce_attr(operation, "id", "operation_id", default=""))
        if not external_id:
            continue

        payment = _to_decimal(to_decimal(coalesce_attr(operation, "payment", default=0)))
        if payment <= DECIMAL_ZERO:
            continue

        event_date = _coerce_datetime(coalesce_attr(operation, "date", default=None)) or utc_now()
        connection = get_db_connection()
        try:
            exists = connection.execute(
                """
                SELECT 1
                FROM financial_events
                WHERE external_id = ?
                """,
                (external_id,),
            ).fetchone()
        finally:
            connection.close()
        if exists:
            continue

        register_closed_position(
            profit=float(payment),
            is_coupon=True,
            event_date=event_date,
            title=str(coalesce_attr(operation, "name", "description", default="Купон")),
            external_id=external_id,
        )
        imported += 1
    return imported


@log_execution
def ensure_month_start_snapshot(portfolio_balance: dict[str, Any]) -> Decimal:
    month_start, _ = _month_bounds()
    snapshot_date = month_start.date().isoformat()
    connection = get_db_connection()
    try:
        row = connection.execute(
            """
            SELECT total_amount_rub
            FROM portfolio_snapshots
            WHERE snapshot_date = ?
            """,
            (snapshot_date,),
        ).fetchone()
        if row is not None:
            return _to_decimal(row["total_amount_rub"])

        current_total = _to_decimal(portfolio_balance["total_amount_rub"])
        connection.execute(
            """
            INSERT INTO portfolio_snapshots (
                snapshot_date,
                total_amount_rub,
                cash_rub,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                snapshot_date,
                _serialize_money(current_total),
                float(portfolio_balance["cash_rub"]),
                utc_now().isoformat(),
            ),
        )
        connection.commit()
        logger.warning(
            "Month start snapshot for %s was missing, current portfolio value was stored as fallback baseline",
            snapshot_date,
        )
        return current_total
    finally:
        connection.close()


@log_execution
def compute_monthly_total_growth(portfolio_balance: dict[str, Any]) -> float:
    baseline = ensure_month_start_snapshot(portfolio_balance)
    current_total = _to_decimal(portfolio_balance["total_amount_rub"])
    if baseline == current_total:
        return _serialize_money(MONTH_START_FALLBACK_GROWTH)
    return _serialize_money(current_total - baseline)


@log_execution
def get_monthly_event_stats() -> dict[str, Any]:
    month_start, month_end = _month_bounds()
    connection = get_db_connection()
    try:
        rows = connection.execute(
            """
            SELECT event_kind, title, gross_profit, net_profit, event_date
            FROM financial_events
            WHERE event_kind IN ('coupon', 'closed_position')
              AND event_date >= ?
              AND event_date < ?
            ORDER BY event_date ASC
            """,
            (month_start.isoformat(), month_end.isoformat()),
        ).fetchall()
    finally:
        connection.close()

    payouts_by_date: dict[str, Decimal] = {}
    monthly_cash_income = DECIMAL_ZERO
    for row in rows:
        gross_profit = _to_decimal(row["gross_profit"])
        monthly_cash_income += gross_profit
        event_day = normalize_date(row["event_date"])
        if event_day is None:
            continue
        date_key = event_day.isoformat()
        payouts_by_date[date_key] = quantize_money(
            payouts_by_date.get(date_key, DECIMAL_ZERO) + gross_profit
        )

    return {
        "monthly_cash_income": _serialize_money(monthly_cash_income),
        "monthly_payouts_history": {
            key: _serialize_money(value)
            for key, value in sorted(payouts_by_date.items())
        },
    }


@log_execution
def get_total_lifetime_income() -> float:
    connection = get_db_connection()
    try:
        row = connection.execute(
            """
            SELECT COALESCE(SUM(net_profit), 0) AS total_net_profit
            FROM financial_events
            WHERE event_kind IN ('coupon', 'closed_position')
            """
        ).fetchone()
        return float(row["total_net_profit"]) if row is not None else 0.0
    finally:
        connection.close()


@log_execution
def estimate_monthly_coupon_income(
    client: TinkoffClient,
    positions: list[dict[str, Any]],
) -> float:
    _, month_end = _month_bounds()
    favorites = get_favorites_from_db()
    try:
        gold_figis = {instrument["figi"] for instrument in client.find_gold_instruments()}
    except Exception:
        gold_figis = set()

    total = DECIMAL_ZERO
    for position in positions:
        if resolve_position_asset_type(position, favorites, gold_figis) not in {"ofz", "corp_bond"}:
            continue
        figi = str(position["figi"])
        instrument_response = client.get_instrument_by_figi(figi)
        instrument = getattr(instrument_response, "instrument", instrument_response)
        maturity_date = normalize_date(getattr(instrument, "maturity_date", None))
        if maturity_date is None:
            continue

        coupons = client.get_bond_coupons(
            figi,
            utc_now().date(),
            min(maturity_date, month_end.date()),
        )
        quantity = _to_decimal(position["quantity"])
        for coupon in coupons:
            coupon_date = normalize_date(coalesce_attr(coupon, "coupon_date", "pay_one_bond_date"))
            if coupon_date is None or coupon_date >= month_end.date() or coupon_date < utc_now().date():
                continue
            if getattr(coupon, "is_cancelled", False):
                continue
            coupon_per_bond = to_decimal(
                coalesce_attr(coupon, "pay_one_bond", "coupon_value", default=0)
            )
            total += quantize_money(coupon_per_bond * quantity)
    return _serialize_money(total)


def _operation_title(operation: Any) -> str:
    name = str(coalesce_attr(operation, "name", default="")).strip()
    if name:
        return name
    operation_type = str(coalesce_attr(operation, "type", "operation_type", default="Операция"))
    figi = str(coalesce_attr(operation, "figi", default="")).strip()
    return f"{operation_type} {figi}".strip()


@log_execution
def get_operations_history(client: TinkoffClient, limit: int = 10) -> list[dict[str, Any]]:
    operations = client.get_operations_by_cursor(limit=limit)
    dated_operations = sorted(
        operations,
        key=lambda item: _coerce_datetime(coalesce_attr(item, "date", default=None)) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    history: list[dict[str, Any]] = []
    for operation in dated_operations[:limit]:
        payment = _to_decimal(to_decimal(coalesce_attr(operation, "payment", default=0)))
        operation_date = _coerce_datetime(coalesce_attr(operation, "date", default=None))
        if operation_date is not None:
            date_iso = operation_date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        else:
            date_iso = utc_now().isoformat().replace("+00:00", "Z")
        history.append(
            {
                "operation_name": _operation_title(operation),
                "date": date_iso,
                "amount": _serialize_money(payment),
            }
        )
    return history


@log_execution
def build_data_payload(login: str, sandbox_mode: bool) -> dict[str, Any]:
    client = TinkoffClient(login=login, sandbox=sandbox_mode)
    portfolio_balance = client.get_portfolio_balance()
    if portfolio_balance is None:
        raise HTTPException(status_code=503, detail="Action blocked by emergency stop")

    sync_coupon_income_events(client)
    settings = get_strategy_settings() or {}
    favorites = get_favorites_from_db()
    try:
        gold_figis = {instrument["figi"] for instrument in client.find_gold_instruments()}
    except Exception:
        gold_figis = set()
    strategy_duration = (
        settings.get("end_date")
        or settings.get("target_date")
        or "бесконечно"
    )
    return {
        "portfolio_balance": float(portfolio_balance["total_amount_rub"]),
        "monthly_total_growth": compute_monthly_total_growth(portfolio_balance),
        "strategy_duration": strategy_duration,
        "diversification": _build_diversification(
            portfolio_balance["positions"],
            favorites,
            gold_figis,
        ),
    }


@log_execution
def build_analyze_payload(login: str, sandbox_mode: bool) -> dict[str, Any]:
    client = TinkoffClient(login=login, sandbox=sandbox_mode)
    portfolio_balance = client.get_portfolio_balance()
    if portfolio_balance is None:
        raise HTTPException(status_code=503, detail="Action blocked by emergency stop")

    sync_coupon_income_events(client)
    month_stats = get_monthly_event_stats()
    reserves = get_financial_reserves()
    monthly_total_growth = compute_monthly_total_growth(portfolio_balance)

    return {
        "monthly_cash_income": month_stats["monthly_cash_income"],
        "estimated_monthly_income": estimate_monthly_coupon_income(
            client,
            portfolio_balance["positions"],
        ),
        "monthly_payouts_history": month_stats["monthly_payouts_history"],
        "available_to_withdraw": reserves["user_withdrawal_pool"],
        "total_lifetime_income": get_total_lifetime_income(),
        "monthly_total_growth_duplicate": monthly_total_growth,
        "operations_history": get_operations_history(client, limit=10),
    }
