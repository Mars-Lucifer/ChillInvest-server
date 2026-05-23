from __future__ import annotations

import inspect
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from app_state import check_avaria, get_avaria_stop, get_logger, log_event, log_execution
from auth import get_api_token, get_db_connection, verify_credentials

try:
    from t_tech.invest import Client, MoneyValue
    from t_tech.invest.exceptions import AioRequestError, InvestError, RequestError
    from t_tech.invest.sandbox.client import SandboxClient
    from t_tech.invest.utils import decimal_to_quotation, quotation_to_decimal

    try:
        from t_tech.invest import InstrumentIdType, InstrumentStatus, OrderDirection, OrderType
    except ImportError:  # pragma: no cover
        InstrumentIdType = None  # type: ignore[assignment]
        InstrumentStatus = None  # type: ignore[assignment]
        OrderDirection = None  # type: ignore[assignment]
        OrderType = None  # type: ignore[assignment]
except ImportError:  # pragma: no cover
    Client = None  # type: ignore[assignment]
    MoneyValue = None  # type: ignore[assignment]
    SandboxClient = None  # type: ignore[assignment]
    InstrumentIdType = None  # type: ignore[assignment]
    InstrumentStatus = None  # type: ignore[assignment]
    OrderDirection = None  # type: ignore[assignment]
    OrderType = None  # type: ignore[assignment]
    AioRequestError = Exception
    InvestError = Exception
    RequestError = Exception

    def decimal_to_quotation(value: Decimal) -> Any:  # pragma: no cover
        raise RuntimeError("SDK is not installed")

    def quotation_to_decimal(value: Any) -> Decimal:  # pragma: no cover
        raise RuntimeError("SDK is not installed")


logger = get_logger(__name__)
router = APIRouter(tags=["portfolio"])

BUY_FEE_RATE = Decimal("0.003")
SELL_FEE_RATE = Decimal("0.003")
PROFIT_TAX_RATE = Decimal("0.13")
STATIC_MODE = "static"
ADAPTIVE_MODE = "adaptive"
TARGET_SHARE = Decimal("33")
GOLD_SELL_SIGNAL_SHARE = Decimal("42")
TOP_LIMIT = 3
MAX_BOND_CANDIDATES_PER_TYPE = 100
STRATEGY_LOOP_SANDBOX_SECONDS = 60
STRATEGY_LOOP_PRODUCTION_SECONDS = 60 * 60 * 24
TGOLD_TICKER = "TGLD"
GOLD_SEARCH_QUERIES = ("TGLD", "GOLD", "AKGD", "SBGD", "VTBG", "gold", "золото")
GOLD_TICKER_PRIORITY = ("TGLD", "GOLD", "AKGD", "SBGD", "VTBG")
RUB_CURRENCY = "rub"
DECIMAL_ZERO = Decimal("0")
DECIMAL_HUNDRED = Decimal("100")
MIN_REASONABLE_BOND_PRICE_PERCENT = Decimal("80")
MAX_REASONABLE_BOND_PRICE_PERCENT = Decimal("120")
MAX_REASONABLE_OFZ_PURE_YIELD = Decimal("20")
MAX_REASONABLE_CORP_PURE_YIELD = Decimal("25")


class BalanceChangeRequest(BaseModel):
    amount_rub: float = Field(..., description="Sandbox balance delta in RUB")


class StrategyStartRequest(BaseModel):
    target_date: date | None = Field(
        default=None,
        description="Target date for strategy completion. Ignored when infinite_run=true.",
    )
    infinite_run: bool = Field(default=False)
    mode: Literal["static", "adaptive"] = Field(...)
    profit_reserve_percent: float = Field(ge=0, le=100)


class StrategyRuntime:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._login: str | None = None
        self._sandbox_mode = True

    def start(self, login: str, sandbox_mode: bool) -> None:
        with self._lock:
            self._login = login
            self._sandbox_mode = sandbox_mode
            if self._thread and self._thread.is_alive():
                self._stop_event.set()
                self._thread.join(timeout=2)
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._worker,
                name="strategy-loop",
                daemon=True,
            )
            self._thread.start()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "login": self._login,
                "sandbox_mode": self._sandbox_mode,
            }

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            settings = get_strategy_settings()
            if not settings or not settings["is_active"]:
                return
            if get_avaria_stop():
                logger.warning("[MODE] Strategy loop paused by emergency stop")
            else:
                try:
                    update_favorites_list(self._login or settings["started_by"], self._sandbox_mode)
                    execute_trading_logic(
                        mode=settings["mode"],
                        login=self._login or settings["started_by"],
                        sandbox_mode=self._sandbox_mode,
                    )
                except Exception as error:  # pragma: no cover - defensive background loop
                    logger.exception("Strategy loop iteration failed: %s", error)
            interval = (
                STRATEGY_LOOP_SANDBOX_SECONDS
                if self._sandbox_mode
                else STRATEGY_LOOP_PRODUCTION_SECONDS
            )
            if self._stop_event.wait(interval):
                return


strategy_runtime = StrategyRuntime()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_decimal(value: Any) -> Decimal:
    if value is None:
        return DECIMAL_ZERO
    if isinstance(value, Decimal):
        return value
    if hasattr(value, "units") and hasattr(value, "nano"):
        return (
            Decimal(str(value.units))
            + (Decimal(str(value.nano)) / Decimal("1000000000"))
        ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    try:
        return quotation_to_decimal(value)
    except Exception:
        return Decimal(str(value))


def normalize_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def get_enum_value(enum_obj: Any, attr: str, fallback: Any) -> Any:
    if enum_obj is None:
        return fallback
    return getattr(enum_obj, attr, fallback)


def coalesce_attr(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def call_with_supported_kwargs(target: Any, method_name: str, **kwargs: Any) -> Any:
    method = getattr(target, method_name)
    signature = inspect.signature(method)
    supported: dict[str, Any] = {}
    for key, value in kwargs.items():
        if value is None:
            continue
        if key in signature.parameters:
            supported[key] = value
    return method(**supported)


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def get_reserved_cash_amount(cash_rub: Decimal, reserve_percent: Decimal) -> Decimal:
    if reserve_percent <= DECIMAL_ZERO:
        return DECIMAL_ZERO
    return quantize_money(cash_rub * reserve_percent / DECIMAL_HUNDRED)


@log_execution
def save_strategy_settings(
    login: str,
    payload: StrategyStartRequest,
) -> dict[str, Any]:
    now_iso = utc_now().isoformat()
    target_date_iso = payload.target_date.isoformat() if payload.target_date else None
    end_date = "бесконечно" if payload.infinite_run else target_date_iso
    connection = get_db_connection()
    try:
        connection.execute(
            """
            INSERT INTO strategy_settings (
                id,
                target_date,
                infinite_run,
                mode,
                profit_reserve_percent,
                profit_withdraw_percent,
                end_date,
                is_active,
                updated_at,
                started_by
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                target_date = excluded.target_date,
                infinite_run = excluded.infinite_run,
                mode = excluded.mode,
                profit_reserve_percent = excluded.profit_reserve_percent,
                profit_withdraw_percent = excluded.profit_withdraw_percent,
                end_date = excluded.end_date,
                is_active = excluded.is_active,
                updated_at = excluded.updated_at,
                started_by = excluded.started_by
            """,
            (
                target_date_iso,
                int(payload.infinite_run),
                payload.mode,
                float(payload.profit_reserve_percent),
                float(payload.profit_reserve_percent),
                end_date,
                now_iso,
                login,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return get_strategy_settings() or {}


@log_execution
def get_strategy_settings() -> dict[str, Any] | None:
    connection = get_db_connection()
    try:
        row = connection.execute(
            """
            SELECT id, target_date, infinite_run, mode, profit_reserve_percent,
                   profit_withdraw_percent, end_date, is_active, updated_at, started_by
            FROM strategy_settings
            WHERE id = 1
            """
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "target_date": row["target_date"],
            "infinite_run": bool(row["infinite_run"]),
            "mode": row["mode"],
            "profit_reserve_percent": float(row["profit_reserve_percent"]),
            "profit_withdraw_percent": float(row["profit_withdraw_percent"]),
            "end_date": row["end_date"],
            "is_active": bool(row["is_active"]),
            "updated_at": row["updated_at"],
            "started_by": row["started_by"],
        }
    finally:
        connection.close()


@log_execution
def get_favorites_from_db() -> list[dict[str, Any]]:
    connection = get_db_connection()
    try:
        rows = connection.execute(
            """
            SELECT id, figi, name, asset_type, pure_yield, updated_at
            FROM favorites
            ORDER BY asset_type, pure_yield DESC, name ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


@log_execution
def replace_favorites(entries: list[dict[str, Any]]) -> None:
    connection = get_db_connection()
    try:
        connection.execute("DELETE FROM favorites")
        connection.executemany(
            """
            INSERT INTO favorites (figi, name, asset_type, pure_yield, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    entry["figi"],
                    entry["name"],
                    entry["asset_type"],
                    entry["pure_yield"],
                    entry["updated_at"],
                )
                for entry in entries
            ],
        )
        connection.commit()
    finally:
        connection.close()


@log_execution
def add_portfolio_log(figi: str, purchase_price: Decimal) -> None:
    connection = get_db_connection()
    try:
        connection.execute(
            """
            INSERT INTO portfolio_logs (figi, purchase_price, date)
            VALUES (?, ?, ?)
            """,
            (
                figi,
                float(quantize_money(purchase_price)),
                utc_now().isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()


@log_execution
def get_average_purchase_price(figi: str) -> Decimal | None:
    connection = get_db_connection()
    try:
        row = connection.execute(
            """
            SELECT AVG(purchase_price) AS avg_purchase_price
            FROM portfolio_logs
            WHERE figi = ?
            """,
            (figi,),
        ).fetchone()
        if row is None or row["avg_purchase_price"] is None:
            return None
        return Decimal(str(row["avg_purchase_price"]))
    finally:
        connection.close()


class TinkoffClient:
    @log_execution
    def __init__(self, login: str, sandbox: bool = True) -> None:
        self.login = login
        self.sandbox = sandbox
        self.token = get_api_token(login)
        self._gold_instruments_cache: list[dict[str, Any]] | None = None
        if not self.token:
            if get_avaria_stop():
                raise HTTPException(status_code=503, detail="Action blocked by emergency stop")
            raise RuntimeError("Failed to load API token")

    def _ensure_sdk_available(self) -> None:
        if Client is None or SandboxClient is None or MoneyValue is None:
            raise RuntimeError(
                "Package t-tech-investments is not installed. Install dependencies from requirements.txt."
            )

    @log_execution
    def _decimal_to_money(self, amount: Decimal, currency: str = RUB_CURRENCY) -> Any:
        decimal_amount = quantize_money(amount)
        quotation = decimal_to_quotation(decimal_amount)
        return MoneyValue(currency=currency, units=quotation.units, nano=quotation.nano)

    def _money_to_decimal(self, money: Any) -> Decimal:
        return quantize_money(to_decimal(money))

    def _create_client(self) -> Any:
        self._ensure_sdk_available()
        if self.sandbox:
            return SandboxClient(self.token)
        return Client(self.token)

    def _get_primary_account_id(self, client: Any) -> str:
        if self.sandbox:
            accounts = client.sandbox.get_sandbox_accounts().accounts
            if not accounts:
                return client.sandbox.open_sandbox_account().account_id
            account_id = coalesce_attr(accounts[0], "id", "account_id")
            if not account_id:
                raise RuntimeError("Sandbox account id was not found in account payload")
            return str(account_id)

        accounts = client.users.get_accounts().accounts
        if not accounts:
            raise RuntimeError("Broker account was not found")
        account_id = coalesce_attr(accounts[0], "id", "account_id")
        if not account_id:
            raise RuntimeError("Broker account id was not found in account payload")
        return str(account_id)

    @log_execution
    def _get_portfolio(self, client: Any, account_id: str) -> Any:
        if self.sandbox:
            return client.sandbox.get_sandbox_portfolio(account_id=account_id)
        return client.operations.get_portfolio(account_id=account_id)

    @log_execution
    def _get_withdraw_limits(self, client: Any, account_id: str) -> Any:
        if self.sandbox:
            return client.sandbox.get_sandbox_withdraw_limits(account_id=account_id)
        return client.operations.get_withdraw_limits(account_id=account_id)

    def _get_orders_service(self, client: Any) -> Any:
        return client.sandbox if self.sandbox else client.orders

    def _get_stop_orders_service(self, client: Any) -> Any:
        return client.sandbox if self.sandbox else client.stop_orders

    def _get_operations_service(self, client: Any) -> Any:
        return client.sandbox if self.sandbox else client.operations

    @log_execution
    def get_instrument_by_figi(self, figi: str) -> Any:
        with self._create_client() as client:
            instrument_service = client.instruments
            fallback_id_type = get_enum_value(
                InstrumentIdType,
                "INSTRUMENT_ID_TYPE_FIGI",
                1,
            )
            return call_with_supported_kwargs(
                instrument_service,
                "get_instrument_by",
                id_type=fallback_id_type,
                id=figi,
                instrument_id=figi,
            )

    @log_execution
    def get_operations(
        self,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        limit: int = 100,
    ) -> list[Any]:
        with self._create_client() as client:
            account_id = self._get_primary_account_id(client)
            operations_service = self._get_operations_service(client)
            if from_dt is None:
                from_dt = utc_now() - timedelta(days=365 * 5)
            if to_dt is None:
                to_dt = utc_now()

            if self.sandbox:
                response = call_with_supported_kwargs(
                    operations_service,
                    "get_sandbox_operations",
                    account_id=account_id,
                    from_=from_dt,
                    to=to_dt,
                )
                return list(coalesce_attr(response, "operations", "items", default=[]))

            response = call_with_supported_kwargs(
                operations_service,
                "get_operations",
                account_id=account_id,
                from_=from_dt,
                to=to_dt,
            )
            return list(coalesce_attr(response, "operations", "items", default=[]))

    @log_execution
    def get_operations_by_cursor(
        self,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        limit: int = 100,
    ) -> list[Any]:
        with self._create_client() as client:
            account_id = self._get_primary_account_id(client)
            operations_service = self._get_operations_service(client)
            if from_dt is None:
                from_dt = utc_now() - timedelta(days=365 * 5)
            if to_dt is None:
                to_dt = utc_now()

            if self.sandbox:
                return self.get_operations(from_dt=from_dt, to_dt=to_dt, limit=limit)

            cursor: str | None = None
            collected: list[Any] = []
            while len(collected) < limit:
                response = call_with_supported_kwargs(
                    operations_service,
                    "get_operations_by_cursor",
                    account_id=account_id,
                    from_=from_dt,
                    to=to_dt,
                    cursor=cursor,
                    limit=min(limit - len(collected), 1000),
                )
                items = list(coalesce_attr(response, "items", "operations", default=[]))
                collected.extend(items)
                has_next = bool(coalesce_attr(response, "has_next", default=False))
                cursor = coalesce_attr(response, "next_cursor", default=None)
                if not has_next or not cursor or not items:
                    break
            return collected[:limit]

    @log_execution
    def get_active_orders(self) -> list[Any]:
        with self._create_client() as client:
            account_id = self._get_primary_account_id(client)
            order_service = self._get_orders_service(client)
            method_name = "get_sandbox_orders" if self.sandbox else "get_orders"
            response = call_with_supported_kwargs(order_service, method_name, account_id=account_id)
            return list(getattr(response, "orders", []))

    @log_execution
    def cancel_order_by_id(self, order_id: str) -> None:
        with self._create_client() as client:
            account_id = self._get_primary_account_id(client)
            order_service = self._get_orders_service(client)
            method_name = "cancel_sandbox_order" if self.sandbox else "cancel_order"
            call_with_supported_kwargs(
                order_service,
                method_name,
                account_id=account_id,
                order_id=order_id,
            )

    @log_execution
    def get_active_stop_orders(self) -> list[Any]:
        with self._create_client() as client:
            account_id = self._get_primary_account_id(client)
            stop_orders_service = self._get_stop_orders_service(client)
            method_name = "get_sandbox_stop_orders" if self.sandbox else "get_stop_orders"
            response = call_with_supported_kwargs(
                stop_orders_service,
                method_name,
                account_id=account_id,
            )
            return list(getattr(response, "stop_orders", []))

    @log_execution
    def cancel_stop_order_by_id(self, stop_order_id: str) -> None:
        with self._create_client() as client:
            account_id = self._get_primary_account_id(client)
            stop_orders_service = self._get_stop_orders_service(client)
            method_name = "cancel_sandbox_stop_order" if self.sandbox else "cancel_stop_order"
            call_with_supported_kwargs(
                stop_orders_service,
                method_name,
                account_id=account_id,
                stop_order_id=stop_order_id,
            )

    @log_execution
    def cancel_all_active_orders(self) -> dict[str, int]:
        cancelled_orders = 0
        cancelled_stop_orders = 0

        for order in self.get_active_orders():
            order_id = coalesce_attr(order, "order_id", "id")
            if not order_id:
                continue
            self.cancel_order_by_id(str(order_id))
            cancelled_orders += 1

        try:
            for stop_order in self.get_active_stop_orders():
                stop_order_id = coalesce_attr(stop_order, "stop_order_id", "id")
                if not stop_order_id:
                    continue
                self.cancel_stop_order_by_id(str(stop_order_id))
                cancelled_stop_orders += 1
        except AttributeError:
            logger.info("[MODE] Stop orders service is not available in current SDK build")

        return {
            "cancelled_orders": cancelled_orders,
            "cancelled_stop_orders": cancelled_stop_orders,
        }

    @check_avaria
    @log_execution
    def get_portfolio_balance(self) -> dict[str, Any] | None:
        try:
            with self._create_client() as client:
                account_id = self._get_primary_account_id(client)
                portfolio = self._get_portfolio(client, account_id)
                withdraw_limits = self._get_withdraw_limits(client, account_id)

                total_amount = self._money_to_decimal(portfolio.total_amount_portfolio)
                positions = []
                for position in portfolio.positions:
                    current_price = self._money_to_decimal(getattr(position, "current_price", DECIMAL_ZERO))
                    current_nkd = self._money_to_decimal(getattr(position, "current_nkd", DECIMAL_ZERO))
                    expected_yield = self._money_to_decimal(getattr(position, "expected_yield", DECIMAL_ZERO))
                    quantity = to_decimal(getattr(position, "quantity", DECIMAL_ZERO))
                    average_price = self._money_to_decimal(
                        getattr(position, "average_position_price", DECIMAL_ZERO)
                    )
                    positions.append(
                        {
                            "figi": getattr(position, "figi", ""),
                            "instrument_type": getattr(position, "instrument_type", ""),
                            "quantity": float(quantity),
                            "current_price": float(current_price),
                            "current_nkd": float(current_nkd),
                            "expected_yield": float(expected_yield),
                            "average_position_price": float(average_price),
                            "position_value": float(
                                quantize_money((current_price + current_nkd) * quantity)
                            ),
                        }
                    )

                cash_rub = DECIMAL_ZERO
                for money in getattr(withdraw_limits, "money", []):
                    if getattr(money, "currency", "").lower() == RUB_CURRENCY:
                        cash_rub += self._money_to_decimal(money)

                return {
                    "account_id": account_id,
                    "sandbox": self.sandbox,
                    "total_amount_rub": float(total_amount),
                    "cash_rub": float(quantize_money(cash_rub)),
                    "positions": positions,
                }
        except (
            AioRequestError,
            InvestError,
            RequestError,
            OSError,
            RuntimeError,
            ValueError,
            AttributeError,
        ) as error:
            logger.error("Failed to fetch portfolio data: %s", error)
            raise HTTPException(status_code=502, detail="Failed to fetch portfolio data") from error

    @check_avaria
    @log_execution
    def change_sandbox_balance(self, amount_rub: Decimal) -> dict[str, Any] | None:
        if not self.sandbox:
            raise HTTPException(status_code=400, detail="Balance change is available only in sandbox mode")

        try:
            with self._create_client() as client:
                account_id = self._get_primary_account_id(client)
                current_balance = self.get_portfolio_balance()
                if current_balance is None:
                    raise HTTPException(status_code=503, detail="Action blocked by emergency stop")

                current_cash = Decimal(str(current_balance["cash_rub"]))
                if amount_rub >= DECIMAL_ZERO:
                    client.sandbox.sandbox_pay_in(
                        account_id=account_id,
                        amount=self._decimal_to_money(amount_rub),
                    )
                else:
                    if current_balance["positions"]:
                        raise HTTPException(
                            status_code=400,
                            detail="Negative balance change requires an empty sandbox portfolio",
                        )
                    target_cash = current_cash + amount_rub
                    if target_cash < DECIMAL_ZERO:
                        raise HTTPException(
                            status_code=400,
                            detail="Cannot reduce sandbox balance below zero",
                        )
                    client.sandbox.close_sandbox_account(account_id=account_id)
                    account_id = client.sandbox.open_sandbox_account().account_id
                    if target_cash > DECIMAL_ZERO:
                        client.sandbox.sandbox_pay_in(
                            account_id=account_id,
                            amount=self._decimal_to_money(target_cash),
                        )

                updated_balance = self.get_portfolio_balance()
                return {
                    "message": "Sandbox balance updated",
                    "account_id": account_id,
                    "balance": updated_balance,
                }
        except HTTPException:
            raise
        except (
            AioRequestError,
            InvestError,
            RequestError,
            OSError,
            RuntimeError,
            ValueError,
            AttributeError,
        ) as error:
            logger.error("Failed to change sandbox balance: %s", error)
            raise HTTPException(status_code=502, detail="Failed to change sandbox balance") from error

    @check_avaria
    @log_execution
    def reset_sandbox_balance_to_zero(self) -> dict[str, Any] | None:
        if not self.sandbox:
            raise HTTPException(status_code=400, detail="Balance reset is available only in sandbox mode")

        try:
            with self._create_client() as client:
                account_id = self._get_primary_account_id(client)
                client.sandbox.close_sandbox_account(account_id=account_id)
                new_account_id = client.sandbox.open_sandbox_account().account_id
                return {
                    "message": "Sandbox portfolio reset to zero",
                    "old_account_id": account_id,
                    "new_account_id": new_account_id,
                    "balance": self.get_portfolio_balance(),
                }
        except (
            AioRequestError,
            InvestError,
            RequestError,
            OSError,
            RuntimeError,
            ValueError,
            AttributeError,
        ) as error:
            logger.error("Failed to reset sandbox balance: %s", error)
            raise HTTPException(status_code=502, detail="Failed to reset sandbox balance") from error

    @log_execution
    def list_bonds(self) -> list[Any]:
        with self._create_client() as client:
            instrument_service = client.instruments
            candidates: list[dict[str, Any]] = []
            if InstrumentStatus is not None:
                candidates.append(
                    {
                        "instrument_status": get_enum_value(
                            InstrumentStatus,
                            "INSTRUMENT_STATUS_BASE",
                            1,
                        )
                    }
                )
                candidates.append(
                    {
                        "instrument_status": get_enum_value(
                            InstrumentStatus,
                            "INSTRUMENT_STATUS_ALL",
                            0,
                        )
                    }
                )
            candidates.append({})
            last_error: Exception | None = None
            for kwargs in candidates:
                try:
                    response = call_with_supported_kwargs(instrument_service, "bonds", **kwargs)
                    return list(getattr(response, "instruments", []))
                except Exception as error:  # pragma: no cover - depends on SDK version
                    last_error = error
            raise RuntimeError(f"Failed to load bonds list: {last_error}")

    def get_last_price(self, figi: str) -> Decimal:
        with self._create_client() as client:
            market_data = client.market_data
            response = call_with_supported_kwargs(
                market_data,
                "get_last_prices",
                figi=[figi],
                instrument_id=[figi],
            )
            prices = getattr(response, "last_prices", [])
            if not prices:
                raise RuntimeError(f"No last price for FIGI {figi}")
            price_value = getattr(prices[0], "price", None)
            if price_value is None:
                raise RuntimeError(f"Last price payload is empty for FIGI {figi}")
            return to_decimal(price_value)

    def get_bond_coupons(self, figi: str, from_date: date, to_date: date) -> list[Any]:
        with self._create_client() as client:
            response = call_with_supported_kwargs(
                client.instruments,
                "get_bond_coupons",
                figi=figi,
                from_=datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc),
                to=datetime.combine(to_date, datetime.max.time(), tzinfo=timezone.utc),
            )
            return list(coalesce_attr(response, "events", "coupons", default=[]))

    def get_accrued_interests(self, figi: str, from_date: date, to_date: date) -> list[Any]:
        with self._create_client() as client:
            response = call_with_supported_kwargs(
                client.instruments,
                "get_accrued_interests",
                figi=figi,
                from_=datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc),
                to=datetime.combine(to_date, datetime.max.time(), tzinfo=timezone.utc),
            )
            return list(getattr(response, "accrued_interests", []))

    def _normalize_gold_instrument(self, instrument: Any) -> dict[str, Any] | None:
        figi = getattr(instrument, "figi", "")
        ticker = getattr(instrument, "ticker", "")
        name = getattr(instrument, "name", "")
        if not figi or not ticker or not name:
            return None
        ticker_upper = str(ticker).upper()
        name_lower = str(name).lower()
        if (
            "gold" not in ticker_upper.lower()
            and "золото" not in name_lower
            and ticker_upper not in GOLD_TICKER_PRIORITY
        ):
            return None
        if getattr(instrument, "buy_available_flag", True) is False:
            return None
        if getattr(instrument, "sell_available_flag", True) is False:
            return None
        if getattr(instrument, "api_trade_available_flag", True) is False:
            return None
        return {
            "figi": figi,
            "ticker": ticker_upper,
            "name": name,
            "lot": int(getattr(instrument, "lot", 1) or 1),
            "asset_type": "gold",
        }

    def _score_gold_instrument(self, instrument: dict[str, Any]) -> tuple[int, int, str]:
        ticker = str(instrument["ticker"]).upper()
        priority = GOLD_TICKER_PRIORITY.index(ticker) if ticker in GOLD_TICKER_PRIORITY else len(GOLD_TICKER_PRIORITY)
        return (-priority, int(instrument.get("lot", 1) == 1), ticker)

    @log_execution
    def find_gold_instruments(self) -> list[dict[str, Any]]:
        if self._gold_instruments_cache is not None:
            return list(self._gold_instruments_cache)

        found_by_figi: dict[str, dict[str, Any]] = {}
        with self._create_client() as client:
            for query in GOLD_SEARCH_QUERIES:
                try:
                    response = call_with_supported_kwargs(
                        client.instruments,
                        "find_instrument",
                        query=query,
                    )
                except Exception:
                    continue

                for instrument in list(getattr(response, "instruments", [])):
                    normalized = self._normalize_gold_instrument(instrument)
                    if normalized is None:
                        continue
                    found_by_figi.setdefault(normalized["figi"], normalized)

        instruments = sorted(found_by_figi.values(), key=self._score_gold_instrument, reverse=True)
        if not instruments:
            raise RuntimeError("No gold instruments available for trading were found")
        self._gold_instruments_cache = instruments
        return list(instruments)

    @log_execution
    def find_tgld(self) -> dict[str, Any]:
        for instrument in self.find_gold_instruments():
            if instrument["ticker"] == TGOLD_TICKER:
                return instrument
        return self.find_gold_instruments()[0]

    @log_execution
    def place_market_order(self, figi: str, quantity_lots: int, direction: str) -> dict[str, Any]:
        if quantity_lots <= 0:
            raise ValueError("Order quantity must be positive")
        with self._create_client() as client:
            account_id = self._get_primary_account_id(client)
            order_service = client.sandbox if self.sandbox else client.orders
            method_name = "post_sandbox_order" if self.sandbox else "post_order"
            order_direction = (
                get_enum_value(OrderDirection, "ORDER_DIRECTION_BUY", 1)
                if direction == "buy"
                else get_enum_value(OrderDirection, "ORDER_DIRECTION_SELL", 2)
            )
            order_type = get_enum_value(OrderType, "ORDER_TYPE_MARKET", 2)
            response = call_with_supported_kwargs(
                order_service,
                method_name,
                figi=figi,
                instrument_id=figi,
                quantity=quantity_lots,
                direction=order_direction,
                account_id=account_id,
                order_type=order_type,
                order_id=str(uuid.uuid4()),
            )
            return {
                "figi": figi,
                "direction": direction,
                "quantity_lots": quantity_lots,
                "order_id": getattr(response, "order_id", None),
            }


def classify_bond(instrument: Any) -> str | None:
    name = getattr(instrument, "name", "").lower()
    ticker = getattr(instrument, "ticker", "").lower()
    bond_type = str(getattr(instrument, "bond_type", "")).lower()
    if "офз" in name or "ofz" in ticker or "government" in bond_type:
        return "ofz"
    if "municip" in name or "муниц" in name:
        return None
    return "corp_bond"


def is_bond_candidate_for_favorites(
    bond: Any,
    today: date,
    strategy_target_date: date | None,
    infinite_run: bool,
) -> bool:
    figi = getattr(bond, "figi", "")
    name = getattr(bond, "name", "")
    maturity_date = normalize_date(getattr(bond, "maturity_date", None))
    currency = getattr(getattr(bond, "nominal", None), "currency", RUB_CURRENCY).lower()
    asset_type = classify_bond(bond)

    if not figi or not name or asset_type is None:
        return False
    if currency != RUB_CURRENCY:
        return False
    if maturity_date is None or maturity_date <= today:
        return False
    if strategy_target_date and not infinite_run and maturity_date > strategy_target_date:
        return False
    if getattr(bond, "buy_available_flag", True) is False:
        return False
    if getattr(bond, "sell_available_flag", True) is False:
        return False
    if getattr(bond, "api_trade_available_flag", True) is False:
        return False
    return True


def score_bond_candidate(bond: Any, today: date) -> tuple[int, int, int, int, int]:
    maturity_date = normalize_date(getattr(bond, "maturity_date", None)) or today
    days_to_maturity = max((maturity_date - today).days, 1)
    liquidity_score = 1 if getattr(bond, "liquidity_flag", False) else 0
    api_score = 1 if getattr(bond, "api_trade_available_flag", False) else 0
    buy_score = 1 if getattr(bond, "buy_available_flag", False) else 0
    coupon_score = int(getattr(bond, "coupon_quantity_per_year", 0) or 0)
    short_maturity_score = -days_to_maturity
    return (liquidity_score, api_score, buy_score, coupon_score, short_maturity_score)


def select_bond_candidates(
    bonds: list[Any],
    today: date,
    strategy_target_date: date | None,
    infinite_run: bool,
) -> list[Any]:
    ofz_candidates: list[Any] = []
    corp_candidates: list[Any] = []

    for bond in bonds:
        if not is_bond_candidate_for_favorites(
            bond=bond,
            today=today,
            strategy_target_date=strategy_target_date,
            infinite_run=infinite_run,
        ):
            continue
        asset_type = classify_bond(bond)
        if asset_type == "ofz":
            ofz_candidates.append(bond)
        elif asset_type == "corp_bond":
            corp_candidates.append(bond)

    ofz_candidates.sort(key=lambda bond: score_bond_candidate(bond, today), reverse=True)
    corp_candidates.sort(key=lambda bond: score_bond_candidate(bond, today), reverse=True)

    selected = (
        ofz_candidates[:MAX_BOND_CANDIDATES_PER_TYPE]
        + corp_candidates[:MAX_BOND_CANDIDATES_PER_TYPE]
    )
    logger.info(
        "[FAVORITES] Candidates selected for deep scan: ofz=%s corp=%s total=%s",
        min(len(ofz_candidates), MAX_BOND_CANDIDATES_PER_TYPE),
        min(len(corp_candidates), MAX_BOND_CANDIDATES_PER_TYPE),
        len(selected),
    )
    return selected


def get_bond_nkd(client: TinkoffClient, bond: Any, today: date) -> Decimal:
    direct_value = getattr(bond, "aci_value", None)
    if direct_value not in (None, ""):
        return quantize_money(to_decimal(direct_value))
    accrued = client.get_accrued_interests(figi=getattr(bond, "figi", ""), from_date=today, to_date=today)
    if not accrued:
        return DECIMAL_ZERO
    last_item = accrued[-1]
    value = coalesce_attr(last_item, "value", "aci_value", default=DECIMAL_ZERO)
    return quantize_money(to_decimal(value))


def sum_future_coupons(coupons: list[Any], today: date) -> Decimal:
    total = DECIMAL_ZERO
    for coupon in coupons:
        coupon_date = normalize_date(coalesce_attr(coupon, "coupon_date", "pay_one_bond_date"))
        if coupon_date is None or coupon_date < today:
            continue
        if getattr(coupon, "is_cancelled", False):
            continue
        total += to_decimal(coalesce_attr(coupon, "pay_one_bond", "coupon_value", default=DECIMAL_ZERO))
    return quantize_money(total)


def calculate_bond_pure_yield(
    nominal: Decimal,
    price_percent: Decimal,
    nkd: Decimal,
    coupon_total: Decimal,
    maturity_date: date,
    today: date,
) -> dict[str, Decimal]:
    clean_price = quantize_money(nominal * price_percent / DECIMAL_HUNDRED)
    purchase_base = clean_price + nkd
    buy_commission = quantize_money(purchase_base * BUY_FEE_RATE)
    sell_commission = quantize_money(nominal * SELL_FEE_RATE)
    gross_profit = nominal + coupon_total - purchase_base - buy_commission - sell_commission
    tax = quantize_money(gross_profit * PROFIT_TAX_RATE) if gross_profit > DECIMAL_ZERO else DECIMAL_ZERO
    net_profit = gross_profit - tax
    invested = purchase_base + buy_commission
    days_to_maturity = max((maturity_date - today).days, 1)
    pure_yield = (
        (net_profit / invested) * (Decimal("365") / Decimal(days_to_maturity)) * DECIMAL_HUNDRED
        if invested > DECIMAL_ZERO
        else DECIMAL_ZERO
    )
    return {
        "purchase_base": quantize_money(purchase_base),
        "buy_commission": buy_commission,
        "sell_commission": sell_commission,
        "coupon_total": quantize_money(coupon_total),
        "tax": tax,
        "net_profit": quantize_money(net_profit),
        "pure_yield": pure_yield.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
    }


@log_execution
def update_favorites_list(login: str, sandbox_mode: bool) -> list[dict[str, Any]]:
    client = TinkoffClient(login=login, sandbox=sandbox_mode)
    settings = get_strategy_settings()
    strategy_target_date = normalize_date(settings["target_date"]) if settings else None
    today = utc_now().date()
    scored: list[dict[str, Any]] = []

    log_event(logger, 20, "[FAVORITES]", "Loading bonds universe for favorites refresh")
    infinite_run = bool(settings["infinite_run"]) if settings else False
    bond_candidates = select_bond_candidates(
        bonds=client.list_bonds(),
        today=today,
        strategy_target_date=strategy_target_date,
        infinite_run=infinite_run,
    )
    for bond in bond_candidates:
        figi = getattr(bond, "figi", "")
        name = getattr(bond, "name", "")
        maturity_date = normalize_date(getattr(bond, "maturity_date", None))
        asset_type = classify_bond(bond)

        if not figi or not name or asset_type is None or maturity_date is None:
            continue

        try:
            nominal = quantize_money(to_decimal(getattr(bond, "nominal", Decimal("1000"))))
            price_percent = to_decimal(client.get_last_price(figi))
            if (
                price_percent < MIN_REASONABLE_BOND_PRICE_PERCENT
                or price_percent > MAX_REASONABLE_BOND_PRICE_PERCENT
            ):
                logger.info(
                    "[FAVORITES] Skip %s: price_pct=%s is outside reliable range",
                    name,
                    price_percent,
                )
                continue
            nkd = get_bond_nkd(client, bond, today)
            coupons = client.get_bond_coupons(figi=figi, from_date=today, to_date=maturity_date)
            coupon_total = sum_future_coupons(coupons, today)
            metrics = calculate_bond_pure_yield(
                nominal=nominal,
                price_percent=price_percent,
                nkd=nkd,
                coupon_total=coupon_total,
                maturity_date=maturity_date,
                today=today,
            )
            pure_yield = metrics["pure_yield"]
            max_reasonable_yield = (
                MAX_REASONABLE_OFZ_PURE_YIELD
                if asset_type == "ofz"
                else MAX_REASONABLE_CORP_PURE_YIELD
            )
            if pure_yield > max_reasonable_yield:
                logger.info(
                    "[FAVORITES] Skip %s: pure_yield=%s exceeds reliable limit for %s",
                    name,
                    pure_yield,
                    asset_type,
                )
                continue
            scored.append(
                {
                    "figi": figi,
                    "name": name,
                    "asset_type": asset_type,
                    "pure_yield": float(pure_yield),
                    "updated_at": utc_now().isoformat(),
                }
            )
            logger.info(
                "[FAVORITES] %s | type=%s | price_pct=%s | nkd=%s | coupons=%s | pure_yield=%s",
                name,
                asset_type,
                price_percent,
                nkd,
                coupon_total,
                pure_yield,
            )
        except Exception as error:
            logger.warning("[FAVORITES] Skip %s (%s): %s", name or figi, figi, error)

    ofz_top = sorted(
        [item for item in scored if item["asset_type"] == "ofz"],
        key=lambda item: item["pure_yield"],
        reverse=True,
    )[:TOP_LIMIT]
    corp_top = sorted(
        [item for item in scored if item["asset_type"] == "corp_bond"],
        key=lambda item: item["pure_yield"],
        reverse=True,
    )[:TOP_LIMIT]
    favorites = ofz_top + corp_top
    replace_favorites(favorites)
    logger.info("[FAVORITES] Final favorites list: %s", favorites)
    return favorites


def build_favorites_map(favorites: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {"ofz": [], "corp_bond": []}
    for favorite in favorites:
        grouped.setdefault(favorite["asset_type"], []).append(favorite)
    return grouped


def resolve_position_asset_type(
    position: dict[str, Any],
    favorites: list[dict[str, Any]],
    gold_figis: set[str],
) -> str | None:
    if position["figi"] in gold_figis:
        return "gold"
    for favorite in favorites:
        if favorite["figi"] == position["figi"]:
            return str(favorite["asset_type"])
    if position["instrument_type"] == "bond":
        return "corp_bond"
    return None


def calculate_allocations(
    positions: list[dict[str, Any]],
    favorites: list[dict[str, Any]],
    gold_figis: set[str],
    total_amount_rub: Decimal,
) -> dict[str, Decimal]:
    allocations = {"gold": DECIMAL_ZERO, "ofz": DECIMAL_ZERO, "corp_bond": DECIMAL_ZERO}
    if total_amount_rub <= DECIMAL_ZERO:
        return allocations
    for position in positions:
        asset_type = resolve_position_asset_type(position, favorites, gold_figis)
        if asset_type is None:
            continue
        value = Decimal(str(position["position_value"]))
        allocations[asset_type] += value
    for key, value in allocations.items():
        allocations[key] = (value / total_amount_rub * DECIMAL_HUNDRED).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    return allocations


def calculate_lost_nkd_percent(position: dict[str, Any]) -> Decimal:
    current_nkd = Decimal(str(position.get("current_nkd", 0)))
    base_value = Decimal(str(position.get("position_value", 0)))
    if base_value <= DECIMAL_ZERO:
        return DECIMAL_ZERO
    return (current_nkd / base_value * DECIMAL_HUNDRED).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_tax_percent_for_sale(position: dict[str, Any]) -> Decimal:
    average_purchase_price = get_average_purchase_price(position["figi"])
    if average_purchase_price is None:
        return DECIMAL_ZERO
    current_price = Decimal(str(position["current_price"]))
    quantity = Decimal(str(position["quantity"]))
    current_total = current_price * quantity
    cost_basis = average_purchase_price * quantity
    taxable_profit = current_total - cost_basis
    if taxable_profit <= DECIMAL_ZERO or current_total <= DECIMAL_ZERO:
        return DECIMAL_ZERO
    tax_amount = taxable_profit * PROFIT_TAX_RATE
    return (tax_amount / current_total * DECIMAL_HUNDRED).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def choose_underweight_asset(
    allocations: dict[str, Decimal],
    positions: list[dict[str, Any]],
    favorites: list[dict[str, Any]],
    gold_figis: set[str],
) -> str | None:
    underweight: list[tuple[str, Decimal]] = []
    for asset_type in ("gold", "ofz", "corp_bond"):
        if allocations[asset_type] < TARGET_SHARE:
            underweight.append((asset_type, TARGET_SHARE - allocations[asset_type]))
    if not underweight:
        return None

    for asset_type, _ in sorted(underweight, key=lambda item: item[1], reverse=True):
        if asset_type == "gold":
            return asset_type
        for position in positions:
            if resolve_position_asset_type(position, favorites, gold_figis) != asset_type:
                continue
            purchase_price = get_average_purchase_price(position["figi"])
            current_price = Decimal(str(position["current_price"]))
            if purchase_price is not None and current_price < purchase_price:
                return asset_type
        if asset_type in {"ofz", "corp_bond"}:
            return asset_type
    return None


def choose_underweight_bond_asset(
    allocations: dict[str, Decimal],
    positions: list[dict[str, Any]],
    favorites: list[dict[str, Any]],
    gold_figis: set[str],
) -> str | None:
    underweight_bonds: list[tuple[str, Decimal]] = []
    for asset_type in ("ofz", "corp_bond"):
        if allocations[asset_type] < TARGET_SHARE:
            underweight_bonds.append((asset_type, TARGET_SHARE - allocations[asset_type]))

    for asset_type, _ in sorted(underweight_bonds, key=lambda item: item[1], reverse=True):
        for position in positions:
            if resolve_position_asset_type(position, favorites, gold_figis) != asset_type:
                continue
            purchase_price = get_average_purchase_price(position["figi"])
            current_price = Decimal(str(position["current_price"]))
            if purchase_price is not None and current_price < purchase_price:
                return asset_type
        return asset_type
    return None


def find_favorite_to_buy(asset_type: str, favorites_by_type: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    candidates = favorites_by_type.get(asset_type, [])
    return candidates[0] if candidates else None


def get_favorite_candidates(asset_type: str, favorites_by_type: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return list(favorites_by_type.get(asset_type, []))


def compute_lot_quantity(budget: Decimal, price_per_piece: Decimal, lot_size: int) -> int:
    if budget <= DECIMAL_ZERO or price_per_piece <= DECIMAL_ZERO or lot_size <= 0:
        return 0
    lot_cost = price_per_piece * Decimal(lot_size)
    lots = (budget / lot_cost).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return int(lots)


def get_asset_target_budget(
    asset_type: str,
    allocations: dict[str, Decimal],
    total_amount_rub: Decimal,
    available_cash: Decimal,
) -> Decimal:
    current_value = quantize_money(total_amount_rub * allocations.get(asset_type, DECIMAL_ZERO) / DECIMAL_HUNDRED)
    target_value = quantize_money(total_amount_rub * TARGET_SHARE / DECIMAL_HUNDRED)
    deficit = max(target_value - current_value, DECIMAL_ZERO)
    return min(available_cash, deficit)


def fetch_bond_lot_and_price(client: TinkoffClient, figi: str) -> tuple[int, Decimal]:
    instrument_response = client.get_instrument_by_figi(figi)
    instrument = getattr(instrument_response, "instrument", instrument_response)
    lot = int(getattr(instrument, "lot", 1) or 1)
    nominal = to_decimal(getattr(instrument, "nominal", Decimal("1000")))
    price_percent = to_decimal(client.get_last_price(figi))
    nkd = get_bond_nkd(client, instrument, utc_now().date())
    price = quantize_money((nominal * price_percent / DECIMAL_HUNDRED) + nkd)
    return lot, price


def get_instrument_lot_size(client: TinkoffClient, figi: str) -> int:
    instrument_response = client.get_instrument_by_figi(figi)
    instrument = getattr(instrument_response, "instrument", instrument_response)
    return int(getattr(instrument, "lot", 1) or 1)


def maybe_buy_gold_first(
    client: TinkoffClient,
    available_cash: Decimal,
    allocations: dict[str, Decimal],
    total_amount_rub: Decimal,
) -> Decimal:
    if allocations["gold"] >= TARGET_SHARE or available_cash <= DECIMAL_ZERO:
        return available_cash

    target_value = quantize_money(total_amount_rub * TARGET_SHARE / DECIMAL_HUNDRED)
    current_value = quantize_money(total_amount_rub * allocations["gold"] / DECIMAL_HUNDRED)
    needed = min(available_cash, max(target_value - current_value, DECIMAL_ZERO))
    if needed <= DECIMAL_ZERO:
        return available_cash

    try:
        gold_candidates = client.find_gold_instruments()
    except (AioRequestError, InvestError, RequestError, OSError, RuntimeError, ValueError) as error:
        logger.warning("[BUY] Gold instruments lookup skipped: %s", error)
        return available_cash
    saw_budget_limit = False
    for gold_candidate in gold_candidates:
        price = client.get_last_price(gold_candidate["figi"])
        lot = int(gold_candidate["lot"])
        quantity_lots = compute_lot_quantity(needed, price, lot)
        if quantity_lots <= 0:
            saw_budget_limit = True
            logger.info(
                "[BUY] Gold allocation is under target, but available cash is not enough for one %s lot",
                gold_candidate["ticker"],
            )
            continue

        try:
            client.place_market_order(gold_candidate["figi"], quantity_lots, "buy")
        except (AioRequestError, InvestError, RequestError, OSError, RuntimeError, ValueError) as error:
            logger.warning("[BUY] %s buy skipped: %s", gold_candidate["ticker"], error)
            continue

        add_portfolio_log(gold_candidate["figi"], price)
        spent = quantize_money(price * Decimal(quantity_lots * lot))
        log_event(
            logger,
            20,
            "[BUY]",
            "Bought %s first for gold allocation: lots=%s, spent=%s RUB",
            gold_candidate["ticker"],
            quantity_lots,
            spent,
        )
        return max(available_cash - spent, DECIMAL_ZERO)

    if saw_budget_limit:
        logger.info("[BUY] No gold instrument could be bought within current gold budget")
    else:
        logger.warning("[BUY] No tradable gold instrument was purchasable via API")
    return available_cash


def maybe_signal_gold_overweight(allocations: dict[str, Decimal]) -> None:
    if allocations["gold"] > GOLD_SELL_SIGNAL_SHARE:
        log_event(
            logger,
            30,
            "[SIGNAL]",
            "Gold share is above 42%%: current_share=%s%%. Manual sell signal generated.",
            allocations["gold"],
        )


def buy_favorite_with_cash(
    client: TinkoffClient,
    asset_type: str,
    favorites_by_type: dict[str, list[dict[str, Any]]],
    available_cash: Decimal,
    purchase_budget: Decimal,
) -> Decimal:
    candidates = get_favorite_candidates(asset_type, favorites_by_type)
    if not candidates:
        logger.info("[BUY] No favorite candidate for asset_type=%s", asset_type)
        return available_cash

    if purchase_budget <= DECIMAL_ZERO:
        logger.info("[BUY] No budget available for asset_type=%s target rebalance", asset_type)
        return available_cash

    saw_budget_limit = False
    for favorite in candidates:
        try:
            lot, market_price = fetch_bond_lot_and_price(client, favorite["figi"])
            quantity_lots = compute_lot_quantity(purchase_budget, market_price, lot)
            if quantity_lots <= 0:
                saw_budget_limit = True
                logger.info(
                    "[BUY] Not enough budget to buy %s | budget=%s | lot_cost=%s",
                    favorite["name"],
                    purchase_budget,
                    quantize_money(market_price * Decimal(lot)),
                )
                continue

            client.place_market_order(favorite["figi"], quantity_lots, "buy")
            add_portfolio_log(favorite["figi"], market_price)
            spent = quantize_money(market_price * Decimal(quantity_lots * lot))
            log_event(
                logger,
                20,
                "[BUY]",
                "Bought favorite %s (%s): lots=%s, spent=%s RUB, budget=%s RUB",
                favorite["name"],
                favorite["asset_type"],
                quantity_lots,
                spent,
                purchase_budget,
            )
            return max(available_cash - spent, DECIMAL_ZERO)
        except (AioRequestError, InvestError, RequestError, OSError, RuntimeError, ValueError) as error:
            logger.warning(
                "[BUY] Favorite %s (%s) skipped: %s",
                favorite["name"],
                favorite["figi"],
                error,
            )
            continue

    if saw_budget_limit:
        logger.info("[BUY] No purchasable favorites within current budget for asset_type=%s", asset_type)
    else:
        logger.warning("[BUY] No tradable favorites available for asset_type=%s", asset_type)
    return available_cash


def evaluate_current_position_yield(client: TinkoffClient, figi: str) -> Decimal:
    instrument_response = client.get_instrument_by_figi(figi)
    instrument = getattr(instrument_response, "instrument", instrument_response)
    maturity_date = normalize_date(getattr(instrument, "maturity_date", None))
    if maturity_date is None or maturity_date <= utc_now().date():
        return DECIMAL_ZERO
    nominal = to_decimal(getattr(instrument, "nominal", Decimal("1000")))
    price_percent = to_decimal(client.get_last_price(figi))
    nkd = get_bond_nkd(client, instrument, utc_now().date())
    coupons = client.get_bond_coupons(figi, utc_now().date(), maturity_date)
    coupon_total = sum_future_coupons(coupons, utc_now().date())
    return calculate_bond_pure_yield(
        nominal=nominal,
        price_percent=price_percent,
        nkd=nkd,
        coupon_total=coupon_total,
        maturity_date=maturity_date,
        today=utc_now().date(),
    )["pure_yield"]


def maybe_replace_with_better_favorite(
    client: TinkoffClient,
    position: dict[str, Any],
    asset_type: str,
    favorites_by_type: dict[str, list[dict[str, Any]]],
) -> None:
    candidate = find_favorite_to_buy(asset_type, favorites_by_type)
    if candidate is None or candidate["figi"] == position["figi"]:
        return

    current_yield = evaluate_current_position_yield(client, position["figi"])
    candidate_yield = Decimal(str(candidate["pure_yield"]))
    sell_buy_cost_pct = Decimal("0.6")
    lost_nkd_pct = calculate_lost_nkd_percent(position)
    tax_pct = calculate_tax_percent_for_sale(position)
    threshold = current_yield + sell_buy_cost_pct + lost_nkd_pct + tax_pct

    logger.info(
        "[MODE] Adaptive compare %s -> %s | candidate=%s | threshold=%s",
        position["figi"],
        candidate["figi"],
        candidate_yield - Decimal("5"),
        threshold,
    )
    if candidate_yield - Decimal("5") <= threshold:
        return

    lot_size = get_instrument_lot_size(client, position["figi"])
    sell_lots = int(
        (
            Decimal(str(position["quantity"])) / Decimal(lot_size)
        ).quantize(Decimal("1"), rounding=ROUND_DOWN)
    )
    if sell_lots <= 0:
        return
    client.place_market_order(position["figi"], sell_lots, "sell")
    average_purchase_price = get_average_purchase_price(position["figi"])
    if average_purchase_price is not None:
        realized_profit = quantize_money(
            (Decimal(str(position["current_price"])) - average_purchase_price)
            * Decimal(str(position["quantity"]))
        )
        if realized_profit > DECIMAL_ZERO:
            from analytics import register_closed_position

            register_closed_position(
                profit=float(realized_profit),
                is_coupon=False,
                event_date=utc_now(),
                title=f"Продажа {position['figi']}",
            )
    log_event(
        logger,
        30,
        "[SELL]",
        "Adaptive replacement sell: figi=%s lots=%s",
        position["figi"],
        sell_lots,
    )

    balance = client.get_portfolio_balance()
    if balance is None:
        return
    available_cash = Decimal(str(balance["cash_rub"]))
    buy_favorite_with_cash(client, asset_type, favorites_by_type, available_cash, available_cash)


@check_avaria
@log_execution
def execute_trading_logic(mode: str, login: str, sandbox_mode: bool) -> dict[str, Any] | None:
    if mode not in {STATIC_MODE, ADAPTIVE_MODE}:
        raise ValueError(f"Unsupported mode: {mode}")

    settings = get_strategy_settings()
    if not settings:
        raise RuntimeError("Strategy settings are missing. Call /start first.")

    client = TinkoffClient(login=login, sandbox=sandbox_mode)
    balance = client.get_portfolio_balance()
    if balance is None:
        raise HTTPException(status_code=503, detail="Action blocked by emergency stop")

    favorites = get_favorites_from_db()
    favorites_by_type = build_favorites_map(favorites)
    try:
        gold_figis = {instrument["figi"] for instrument in client.find_gold_instruments()}
    except (AioRequestError, InvestError, RequestError, OSError, RuntimeError, ValueError) as error:
        logger.warning("[MODE] Gold instruments lookup failed: %s", error)
        gold_figis = set()
    total_amount = Decimal(str(balance["total_amount_rub"]))
    reserve_percent = Decimal(str(settings["profit_reserve_percent"]))
    reserved_cash = get_reserved_cash_amount(Decimal(str(balance["cash_rub"])), reserve_percent)
    try:
        from analytics import get_financial_reserves

        financial_reserves = get_financial_reserves()
    except Exception as error:  # pragma: no cover - defensive fallback
        logger.warning("[MODE] Failed to load financial reserves, fallback to zero: %s", error)
        financial_reserves = {
            "virtual_tax_pool": 0.0,
            "user_withdrawal_pool": 0.0,
        }
    locked_cash = quantize_money(
        Decimal(str(financial_reserves["virtual_tax_pool"]))
        + Decimal(str(financial_reserves["user_withdrawal_pool"]))
    )
    available_cash = max(
        Decimal(str(balance["cash_rub"])) - reserved_cash - locked_cash,
        DECIMAL_ZERO,
    )
    allocations = calculate_allocations(balance["positions"], favorites, gold_figis, total_amount)

    logger.info(
        "[MODE] Execute trading logic | mode=%s | cash=%s | reserved=%s | locked=%s | allocations=%s",
        mode,
        available_cash,
        reserved_cash,
        locked_cash,
        allocations,
    )

    maybe_signal_gold_overweight(allocations)
    available_cash = maybe_buy_gold_first(client, available_cash, allocations, total_amount)

    if mode == STATIC_MODE:
        if available_cash <= DECIMAL_ZERO:
            return {"mode": mode, "status": "no_free_cash"}
        underweight_asset = choose_underweight_asset(
            allocations,
            balance["positions"],
            favorites,
            gold_figis,
        ) or "ofz"
        if underweight_asset == "gold":
            logger.info("[MODE] Gold is underweight but unavailable for purchase, falling back to bonds")
            underweight_asset = choose_underweight_bond_asset(
                allocations,
                balance["positions"],
                favorites,
                gold_figis,
            ) or "ofz"
        if underweight_asset != "gold":
            purchase_budget = get_asset_target_budget(
                asset_type=underweight_asset,
                allocations=allocations,
                total_amount_rub=total_amount,
                available_cash=available_cash,
            )
            logger.info(
                "[MODE] Planned purchase budget for %s: %s RUB",
                underweight_asset,
                purchase_budget,
            )
            available_cash = buy_favorite_with_cash(
                client,
                underweight_asset,
                favorites_by_type,
                available_cash,
                purchase_budget,
            )
        return {
            "mode": mode,
            "status": "completed",
            "remaining_cash_rub": float(available_cash),
        }

    for position in balance["positions"]:
        asset_type = resolve_position_asset_type(position, favorites, gold_figis)
        if asset_type not in {"ofz", "corp_bond"}:
            continue
        maybe_replace_with_better_favorite(client, position, asset_type, favorites_by_type)

    if available_cash > DECIMAL_ZERO:
        underweight_asset = choose_underweight_asset(
            allocations,
            balance["positions"],
            favorites,
            gold_figis,
        )
        if underweight_asset == "gold":
            logger.info("[MODE] Gold is underweight but unavailable for purchase, falling back to bonds")
            underweight_asset = choose_underweight_bond_asset(
                allocations,
                balance["positions"],
                favorites,
                gold_figis,
            )
        if underweight_asset and underweight_asset != "gold":
            purchase_budget = get_asset_target_budget(
                asset_type=underweight_asset,
                allocations=allocations,
                total_amount_rub=total_amount,
                available_cash=available_cash,
            )
            logger.info(
                "[MODE] Planned purchase budget for %s: %s RUB",
                underweight_asset,
                purchase_budget,
            )
            available_cash = buy_favorite_with_cash(
                client,
                underweight_asset,
                favorites_by_type,
                available_cash,
                purchase_budget,
            )

    return {
        "mode": mode,
        "status": "completed",
        "remaining_cash_rub": float(available_cash),
    }


@router.get("/balance")
@router.post("/balance")
@log_execution
async def get_balance(user: dict[str, Any] = Depends(verify_credentials)) -> dict[str, Any]:
    client = TinkoffClient(login=user["login"], sandbox=user["sandbox_mode"])
    result = client.get_portfolio_balance()
    if result is None:
        raise HTTPException(status_code=503, detail="Action blocked by emergency stop")
    return result


@router.post("/balance/change")
@log_execution
async def change_balance(
    payload: BalanceChangeRequest = Body(...),
    user: dict[str, Any] = Depends(verify_credentials),
) -> dict[str, Any]:
    client = TinkoffClient(login=user["login"], sandbox=user["sandbox_mode"])
    result = client.change_sandbox_balance(Decimal(str(payload.amount_rub)))
    if result is None:
        raise HTTPException(status_code=503, detail="Action blocked by emergency stop")
    return result


@router.post("/balance/zero")
@log_execution
async def zero_balance(user: dict[str, Any] = Depends(verify_credentials)) -> dict[str, Any]:
    client = TinkoffClient(login=user["login"], sandbox=user["sandbox_mode"])
    result = client.reset_sandbox_balance_to_zero()
    if result is None:
        raise HTTPException(status_code=503, detail="Action blocked by emergency stop")
    return result


@router.post("/start")
@log_execution
async def start_strategy(
    payload: StrategyStartRequest = Body(...),
    user: dict[str, Any] = Depends(verify_credentials),
) -> dict[str, Any]:
    if not payload.infinite_run and payload.target_date is None:
        raise HTTPException(
            status_code=422,
            detail="target_date is required when infinite_run is false",
        )
    settings = save_strategy_settings(login=user["login"], payload=payload)
    strategy_runtime.start(login=user["login"], sandbox_mode=bool(user["sandbox_mode"]))
    logger.info(
        "[MODE] Strategy started by %s | mode=%s | sandbox=%s | target_date=%s | infinite=%s | reserve=%s%%",
        user["login"],
        payload.mode,
        user["sandbox_mode"],
        payload.target_date,
        payload.infinite_run,
        payload.profit_reserve_percent,
    )
    return {
        "status": "started",
        "settings": settings,
        "runtime": strategy_runtime.status(),
    }


@router.get("/favorites")
@log_execution
async def get_favorites(user: dict[str, Any] = Depends(verify_credentials)) -> dict[str, Any]:
    return {"items": get_favorites_from_db()}


@router.get("/strategy/status")
@log_execution
async def get_strategy_status(user: dict[str, Any] = Depends(verify_credentials)) -> dict[str, Any]:
    return {
        "settings": get_strategy_settings(),
        "runtime": strategy_runtime.status(),
    }
