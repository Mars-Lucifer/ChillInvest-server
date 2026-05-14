"""
Движок стратегии: очередь задач, суточный цикл, избранное, режимы static/adaptive,
ребаланс 33/33/33, tax-loss harvesting. Глобальный аварийный стоп.

Брокер: T-Invest API v2 через пакет **t-tech-investments** (модуль `t_tech.invest`, gRPC).
См. https://opensource.tbank.ru/invest/invest-python и https://developer.tbank.ru/invest/api
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from t_tech.invest import Client
from t_tech.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX
from t_tech.invest.exceptions import RequestError
from t_tech.invest.schemas import (
    Bond,
    CancelStopOrderRequest,
    GetStopOrdersRequest,
    InstrumentIdType,
    InstrumentStatus,
    MoneyValue,
    Operation,
    OrderDirection,
    OrderType,
    PortfolioPosition,
    SecurityTradingStatus,
)
from t_tech.invest.utils import money_to_decimal, quotation_to_decimal

from auth import VaultStore, normalize_invest_token

logger = logging.getLogger(__name__)

avaria_stop: bool = False


def is_halted() -> bool:
    return avaria_stop


def set_avaria_stop(value: bool) -> None:
    global avaria_stop
    avaria_stop = value


COMMISSION_BOND = 0.003
COMMISSION_GOLD = 0.019
TAX_RATE = 0.13
TARGET_WEIGHT = 1.0 / 3.0
GOLD_OVERWEIGHT_TRIM = TARGET_WEIGHT + 0.09
HARVEST_COOLDOWN_SEC = 7 * 24 * 3600
GOLD_TICKER = "GLDRUB_TOM"
# В песочнице цикл гоняем раз в минуту, в проде — раз в сутки.
PIPELINE_INTERVAL_PROD_SEC = 24 * 60 * 60
PIPELINE_INTERVAL_SANDBOX_SEC = 15 * 60
SANDBOX_TARGET_CASH_RUB = 100_000.0
SANDBOX_MIN_CASH_RUB = 20_000.0

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="broker")


async def _to_thread(fn: Callable, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, lambda: fn(*args, **kwargs))


def _normalize_ytm(raw_val: Optional[float]) -> float:
    if raw_val is None:
        return 0.0
    try:
        v = float(raw_val)
    except (TypeError, ValueError):
        return 0.0
    if abs(v) > 1.5:
        return v / 100.0
    return v


def net_annual_yield_bonds(ytm_raw: Optional[float]) -> float:
    ytm = _normalize_ytm(ytm_raw)
    if ytm <= 0:
        return 0.0
    return max(0.0, ytm * (1.0 - TAX_RATE) - COMMISSION_BOND)


def adaptive_swap_threshold(
    new_yield: float,
    current_yield: float,
    lost_nkd_frac: float,
) -> bool:
    if avaria_stop:
        return False
    sell_buy = 2.0 * COMMISSION_BOND
    tax_on_sale_gain = TAX_RATE * max(0.0, current_yield)
    return new_yield > (current_yield + sell_buy + lost_nkd_frac + tax_on_sale_gain)


def is_ofz_ticker(ticker: str) -> bool:
    t = (ticker or "").upper()
    return t.startswith("SU") or "OFZ" in t


def bond_bucket(bond: Bond) -> str:
    ticker = (bond.ticker or "").upper()
    issue_kind = (getattr(bond, "issue_kind", None) or "").upper()
    name = (getattr(bond, "name", None) or "").upper()
    if (
        is_ofz_ticker(ticker)
        or "ОФЗ" in name
        or "OFZ" in name
        or "GOV" in issue_kind
        or "GOVERNMENT" in issue_kind
        or "STATE" in issue_kind
    ):
        return "OFZ"
    return "CORP"


def _bond_has_identity(bond: Bond) -> bool:
    return bool((bond.figi or "").strip() and (bond.ticker or "").strip())


def _bond_base_candidate(bond: Bond) -> bool:
    if not _bond_has_identity(bond):
        return False
    if bond.perpetual_flag:
        return False
    if bond.maturity_date is None:
        return False
    return True


def _bond_strict_candidate(bond: Bond) -> bool:
    return (
        _bond_base_candidate(bond) and bond.buy_available_flag and bond.liquidity_flag
    )


def _bond_sandbox_candidate(bond: Bond) -> bool:
    if not _bond_base_candidate(bond):
        return False
    tradable_statuses = (
        SecurityTradingStatus.SECURITY_TRADING_STATUS_NORMAL_TRADING,
        SecurityTradingStatus.SECURITY_TRADING_STATUS_DEALER_NORMAL_TRADING,
    )
    return (
        bond.api_trade_available_flag
        or bond.buy_available_flag
        or bond.trading_status in tradable_statuses
    )


def _inst_kind(p: Any) -> str:
    return (getattr(p, "instrument_type", None) or "").lower()


def _is_currency(p: Any) -> bool:
    return _inst_kind(p) == "currency"


def _is_bond(p: Any) -> bool:
    return _inst_kind(p) == "bond"


def _compat_avg_price(p: PortfolioPosition) -> SimpleNamespace:
    v = 0.0
    if p.average_position_price:
        v = float(money_to_decimal(p.average_position_price))
    return SimpleNamespace(value=v)


def _compat_pos(p: PortfolioPosition) -> SimpleNamespace:
    qty = float(quotation_to_decimal(p.quantity)) if p.quantity else 0.0
    lots = int(quotation_to_decimal(p.quantity_lots)) if p.quantity_lots else 0
    return SimpleNamespace(
        figi=p.figi,
        ticker=p.ticker or "",
        instrument_type=p.instrument_type or "",
        balance=qty,
        lots=lots,
        average_price=_compat_avg_price(p),
        _raw=p,
    )


def estimate_bond_yield_percent(b: Bond, last_price: Decimal) -> float:
    """Грубая оценка годовой доходности % для ранжирования (номинал, цена, срок)."""
    if last_price <= 0:
        return 0.0
    try:
        nom = money_to_decimal(b.nominal)
        if nom <= 0:
            return 0.0
        mat = b.maturity_date
        if mat.tzinfo is None:
            mat = mat.replace(tzinfo=timezone.utc)
        years = max(0.25, (mat - datetime.now(timezone.utc)).days / 365.25)
        discount_yield = float((nom - last_price) / last_price) / years * 100.0
        coupon_guess = 4.0 + min(4.0, float(b.coupon_quantity_per_year or 2) * 2.0)
        return max(0.0, discount_yield + coupon_guess)
    except Exception:
        return 0.0


def adapt_operation(op: Operation) -> SimpleNamespace:
    pay = float(money_to_decimal(op.payment)) if op.payment else 0.0
    ot = getattr(op, "operation_type", None)
    oname = ot.name if ot is not None else ""
    if not oname:
        oname = str(getattr(op, "type", "") or "")
    st = getattr(op, "state", None)
    return SimpleNamespace(
        id=op.id,
        figi=op.figi or "",
        operation=SimpleNamespace(name=oname),
        status=SimpleNamespace(name=st.name if st is not None else ""),
        date=op.date,
        payment=SimpleNamespace(value=pay),
        currency=SimpleNamespace(
            value=(getattr(op, "currency", None) or "rub").upper()
        ),
    )


class BrokerFacade:
    """Клиент Invest API v2; публичные методы учитывают avaria_stop."""

    def __init__(
        self, token: str, account_id: str, *, use_sandbox: bool = False
    ) -> None:
        self._token = normalize_invest_token(token)
        self._account_id = account_id
        self._use_sandbox = use_sandbox

    @property
    def _grpc_target(self) -> str:
        return INVEST_GRPC_API_SANDBOX if self._use_sandbox else INVEST_GRPC_API

    def create_market_order(
        self, direction: OrderDirection, figi: str, lots: int
    ) -> bool:
        if avaria_stop or not self._account_id or lots <= 0:
            return False
        dir_label = getattr(direction, "name", None) or str(direction)
        # PostOrder: figi deprecated; в instrument_id передают UID или figi.
        inst = self.get_instrument_by_figi(figi)
        instrument_id = figi
        if inst is not None:
            uid = (getattr(inst, "uid", None) or "").strip()
            if uid:
                instrument_id = uid
        with Client(self._token, target=self._grpc_target) as client:
            try:
                if self._use_sandbox:
                    # Счёт песочницы: обычный PostOrder недоступен; sandbox — PostSandboxOrder.
                    resp = client.sandbox.post_sandbox_order(
                        figi="",
                        instrument_id=instrument_id,
                        quantity=lots,
                        direction=direction,
                        account_id=self._account_id,
                        order_type=OrderType.ORDER_TYPE_MARKET,
                    )
                else:
                    resp = client.orders.post_order(
                        figi="",
                        instrument_id=instrument_id,
                        quantity=lots,
                        direction=direction,
                        account_id=self._account_id,
                        order_type=OrderType.ORDER_TYPE_MARKET,
                    )
            except RequestError as e:
                if self._use_sandbox:
                    logger.warning(
                        "[sandbox] заявка отклонена брокером: %s instrument_id=%s lots=%s details=%s",
                        dir_label,
                        instrument_id,
                        lots,
                        getattr(e, "details", "") or str(e),
                    )
                    return False
                raise
            if self._use_sandbox:
                oid = getattr(resp, "order_id", None) or ""
                logger.info(
                    "[sandbox] рыночная заявка: %s instrument_id=%s lots=%s order_id=%s account_id=%s",
                    dir_label,
                    instrument_id,
                    lots,
                    oid,
                    self._account_id,
                )
            return True

    def get_trading_status(self, figi: str):
        if avaria_stop:
            return None
        inst = self.get_instrument_by_figi(figi)
        instrument_id = getattr(inst, "uid", None) or figi
        with Client(self._token, target=self._grpc_target) as client:
            return client.market_data.get_trading_status(
                figi="",
                instrument_id=instrument_id,
            )

    def cancel_all_orders(self, *, ignore_halt: bool = False) -> None:
        if not ignore_halt and avaria_stop:
            return
        if not self._account_id:
            return
        if self._use_sandbox:
            logger.info(
                "[sandbox] снятие всех заявок: account_id=%s ignore_halt=%s",
                self._account_id,
                ignore_halt,
            )
        with Client(self._token, target=self._grpc_target) as client:
            if self._use_sandbox:
                orders_response = client.sandbox.get_sandbox_orders(
                    account_id=self._account_id
                )
                for order in orders_response.orders:
                    client.sandbox.cancel_sandbox_order(
                        account_id=self._account_id,
                        order_id=order.order_id,
                    )
                so_req = GetStopOrdersRequest()
                so_req.account_id = self._account_id
                stop_resp = client.sandbox.get_sandbox_stop_orders(request=so_req)
                for so in stop_resp.stop_orders:
                    cs_req = CancelStopOrderRequest()
                    cs_req.account_id = self._account_id
                    cs_req.stop_order_id = so.stop_order_id
                    client.sandbox.cancel_sandbox_stop_order(request=cs_req)
            else:
                client.cancel_all_orders(account_id=self._account_id)

    def portfolio_rub_value(self) -> Tuple[float, List[SimpleNamespace]]:
        if avaria_stop or not self._account_id:
            return 0.0, []
        with Client(self._token, target=self._grpc_target) as client:
            port = client.operations.get_portfolio(account_id=self._account_id)
            total = 0.0
            if port.total_amount_portfolio:
                total = float(money_to_decimal(port.total_amount_portfolio))
            positions = [_compat_pos(p) for p in port.positions]
            return total, positions

    def classify_position(self, pos: SimpleNamespace) -> str:
        t = (pos.ticker or "").upper()
        if t == GOLD_TICKER:
            return "GOLD"
        if _is_bond(pos):
            return "OFZ" if is_ofz_ticker(pos.ticker) else "BONDS"
        if _inst_kind(pos) == "etf" and "GLD" in t:
            return "GOLD"
        return "OTHER"

    def diversification(self) -> Dict[str, float]:
        if avaria_stop or not self._account_id:
            return {"OFZ": 0.0, "Bonds": 0.0, "Gold": 0.0}
        total, positions = self.portfolio_rub_value()
        if total <= 0:
            return {"OFZ": 0.0, "Bonds": 0.0, "Gold": 0.0}
        acc = {"OFZ": 0.0, "BONDS": 0.0, "GOLD": 0.0, "OTHER": 0.0}
        for p in positions:
            if _is_currency(p):
                continue
            raw: PortfolioPosition = p._raw
            qty = quotation_to_decimal(raw.quantity) if raw.quantity else Decimal(0)
            cur = (
                money_to_decimal(raw.current_price) if raw.current_price else Decimal(0)
            )
            val = float(qty * cur)
            bucket = self.classify_position(p)
            if bucket == "OFZ":
                acc["OFZ"] += val
            elif bucket == "BONDS":
                acc["BONDS"] += val
            elif bucket == "GOLD":
                acc["GOLD"] += val
            else:
                acc["OTHER"] += val
        investable = sum(acc[k] for k in ("OFZ", "BONDS", "GOLD", "OTHER"))
        if investable <= 0:
            investable = total

        def pct(x: float) -> float:
            return round(100.0 * x / investable, 2) if investable > 0 else 0.0

        return {
            "OFZ": pct(acc["OFZ"]),
            "Bonds": pct(acc["BONDS"]),
            "Gold": pct(acc["GOLD"]),
        }

    def operations_range(self, start: datetime, end: datetime) -> List[Operation]:
        if avaria_stop or not self._account_id:
            return []
        with Client(self._token, target=self._grpc_target) as client:
            resp = client.operations.get_operations(
                account_id=self._account_id,
                from_=start,
                to=end,
            )
            return list(resp.operations)

    def bonds_universe(self) -> Dict[str, Bond]:
        if avaria_stop:
            return {}
        with Client(self._token, target=self._grpc_target) as client:
            r = client.instruments.bonds(
                instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
            )
            return {b.ticker: b for b in r.instruments if b.ticker}

    def bond_yield_for_ticker(self, ticker: str) -> float:
        if avaria_stop:
            return 0.0
        with Client(self._token, target=self._grpc_target) as client:
            bonds = client.instruments.bonds(
                instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
            ).instruments
            b = next((x for x in bonds if x.ticker == ticker), None)
            if not b:
                return 0.0
            lp = client.market_data.get_last_prices(figi=[b.figi])
            price = Decimal(0)
            for item in lp.last_prices:
                if item.figi == b.figi:
                    price = quotation_to_decimal(item.price)
                    break
            yp = estimate_bond_yield_percent(b, price)
            return net_annual_yield_bonds(yp)

    def get_orderbook(self, figi: str, depth: int):
        if avaria_stop:
            return None
        with Client(self._token, target=self._grpc_target) as client:
            ob = client.market_data.get_order_book(figi=figi, depth=depth)
            lp = ob.last_price
            px = float(quotation_to_decimal(lp)) if lp else 0.0
            return SimpleNamespace(last_price=px)

    def get_instrument_by_figi(self, figi: str):
        if avaria_stop:
            return None
        with Client(self._token, target=self._grpc_target) as client:
            r = client.instruments.get_instrument_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI,
                id=figi,
            )
            inst = r.instrument
            return SimpleNamespace(
                lot_size=int(inst.lot or 1), figi=inst.figi, uid=inst.uid
            )

    def get_portfolio_currencies(self) -> List[SimpleNamespace]:
        if avaria_stop or not self._account_id:
            return []
        with Client(self._token, target=self._grpc_target) as client:
            pos = client.operations.get_positions(account_id=self._account_id)
            out: List[SimpleNamespace] = []
            for m in pos.money:
                cur = (m.currency or "rub").upper()
                bal = float(money_to_decimal(m))
                name = SimpleNamespace(value=cur)
                out.append(
                    SimpleNamespace(
                        name=name,
                        balance=SimpleNamespace(value=bal),
                        blocked=0.0,
                    )
                )
            return out

    def ensure_sandbox_cash(self) -> float:
        if avaria_stop or not self._use_sandbox or not self._account_id:
            return 0.0
        cash_rub = 0.0
        for cur in self.get_portfolio_currencies():
            if cur.name.value == "RUB":
                cash_rub = float(cur.balance.value) - float(
                    getattr(cur, "blocked", 0.0) or 0.0
                )
                break
        if cash_rub >= SANDBOX_MIN_CASH_RUB:
            logger.info(
                "[sandbox] свободный RUB остаток достаточный: %.2f",
                cash_rub,
            )
            return cash_rub
        top_up_amount = max(0.0, SANDBOX_TARGET_CASH_RUB - cash_rub)
        if top_up_amount <= 0:
            return cash_rub
        units = int(top_up_amount)
        nano = int(round((top_up_amount - units) * 1_000_000_000))
        if nano >= 1_000_000_000:
            units += 1
            nano -= 1_000_000_000
        with Client(self._token, target=self._grpc_target) as client:
            client.sandbox.sandbox_pay_in(
                account_id=self._account_id,
                amount=MoneyValue(currency="rub", units=units, nano=nano),
            )
        logger.info(
            "[sandbox] счёт пополнен: +%.2f RUB, новый целевой остаток %.2f RUB",
            top_up_amount,
            cash_rub + top_up_amount,
        )
        return cash_rub + top_up_amount

    def list_accounts(self):
        if avaria_stop:
            return []
        with Client(self._token, target=self._grpc_target) as client:
            accs = list(client.users.get_accounts().accounts)
            # В песочнице до первого OpenSandboxAccount GetAccounts часто пустой.
            if self._use_sandbox and not accs:
                opened = client.sandbox.open_sandbox_account("ChillInvest")
                logger.info(
                    "[sandbox] открыт счёт песочницы: account_id=%s",
                    getattr(opened, "account_id", "") or "",
                )
                accs = list(client.users.get_accounts().accounts)
            return accs


class StrategyEngine:
    """Очередь и суточный пайплайн Task1 → Task2 → Task3."""

    def __init__(self, store: VaultStore, token_getter: Callable[[], str]) -> None:
        self._store = store
        self._token_getter = token_getter
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()

    def _broker(self) -> BrokerFacade:
        row = self._store.get_settings_row()
        account_id = row["account_id"] if row and row["account_id"] else ""
        return BrokerFacade(
            self._token_getter(),
            account_id,
            use_sandbox=self._store.get_use_sandbox(),
        )

    async def enqueue_pipeline(self) -> None:
        await self._queue.put("daily_pipeline")

    async def _worker_loop(self) -> None:
        while not self._stopped.is_set():
            job = await self._queue.get()
            try:
                if job == "daily_pipeline":
                    await self._run_daily_pipeline()
            except Exception:
                logger.exception("Ошибка в воркере стратегии")
            finally:
                self._queue.task_done()

    async def _scheduler_loop(self) -> None:
        await self.enqueue_pipeline()
        while not self._stopped.is_set():
            interval = (
                PIPELINE_INTERVAL_SANDBOX_SEC
                if self._store.get_use_sandbox()
                else PIPELINE_INTERVAL_PROD_SEC
            )
            await asyncio.sleep(interval)
            await self.enqueue_pipeline()

    async def start(self) -> None:
        if self._worker_task and not self._worker_task.done():
            logger.info("Стратегия уже запущена, повторный старт пропущен")
            return
        self._stopped.clear()
        self._worker_task = asyncio.create_task(
            self._worker_loop(), name="strategy-worker"
        )
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(), name="strategy-scheduler"
        )
        logger.info(
            "Стратегия запущена: sandbox=%s interval_sec=%s",
            self._store.get_use_sandbox(),
            (
                PIPELINE_INTERVAL_SANDBOX_SEC
                if self._store.get_use_sandbox()
                else PIPELINE_INTERVAL_PROD_SEC
            ),
        )

    async def stop(self) -> None:
        self._stopped.set()
        for t in (self._worker_task, self._scheduler_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    async def _run_daily_pipeline(self) -> None:
        if avaria_stop:
            return
        sandbox = self._store.get_use_sandbox()
        logger.info(
            "Пайплайн стартовал: sandbox=%s queued_jobs=%s",
            sandbox,
            self._queue.qsize(),
        )
        if sandbox:
            await _to_thread(self._ensure_sandbox_ready)
        if avaria_stop:
            return
        if sandbox:
            logger.info("[sandbox] пайплайн: Task1 избранное (облигации)")
        await _to_thread(self._task1_favorites)
        if avaria_stop:
            return
        if sandbox:
            logger.info("[sandbox] пайплайн: Task2 режимы static/adaptive")
        await _to_thread(self._task2_modes)
        if avaria_stop:
            return
        if sandbox:
            logger.info("[sandbox] пайплайн: Task3 ребаланс и tax-loss")
        await _to_thread(self._task3_rebalance_and_tax)
        if sandbox:
            logger.info("[sandbox] пайплайн: цикл завершён")

    def _ensure_sandbox_ready(self) -> None:
        if avaria_stop:
            return
        br = self._broker()
        if not br._use_sandbox:
            return
        if not br._account_id:
            logger.warning("[sandbox] нет account_id, подготовка счёта пропущена")
            return
        br.ensure_sandbox_cash()

    def _task1_favorites(self) -> None:
        if avaria_stop:
            return
        br = self._broker()
        if not br._account_id:
            logger.warning("Task1 пропущен: не найден broker account_id")
            return
        with Client(br._token, target=br._grpc_target) as client:
            all_bonds = client.instruments.bonds(
                instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
            ).instruments
            base_bonds = [b for b in all_bonds if _bond_base_candidate(b)]
            bonds = [b for b in base_bonds if _bond_strict_candidate(b)]
            if br._use_sandbox:
                sandbox_bonds = [b for b in base_bonds if _bond_sandbox_candidate(b)]
                if sandbox_bonds:
                    bonds = sandbox_bonds
                elif not bonds:
                    bonds = base_bonds
            elif not bonds:
                bonds = base_bonds
            logger.info(
                "Task1 фильтрация bonds: total=%s base=%s selected=%s sandbox=%s",
                len(all_bonds),
                len(base_bonds),
                len(bonds),
                br._use_sandbox,
            )
            bonds = bonds[:500]
            price_by_figi: Dict[str, Decimal] = {}
            chunk = 80
            for i in range(0, len(bonds), chunk):
                if avaria_stop:
                    return
                part = bonds[i : i + chunk]
                figis = [b.figi for b in part]
                lp = client.market_data.get_last_prices(figi=figis)
                for item in lp.last_prices:
                    price_by_figi[item.figi] = quotation_to_decimal(item.price)
            scored: List[Tuple[float, str, str, str, str]] = []
            for b in bonds:
                if avaria_stop:
                    return
                px = price_by_figi.get(b.figi, Decimal(0))
                yp = estimate_bond_yield_percent(b, px)
                ny = net_annual_yield_bonds(yp)
                kind = bond_bucket(b)
                row_id = str(uuid.uuid4())
                scored.append((ny, row_id, kind, b.figi, b.ticker))
        scored.sort(key=lambda x: x[0], reverse=True)
        ofz = [x for x in scored if x[2] == "OFZ"][:3]
        corp = [x for x in scored if x[2] == "CORP"][:3]
        now = datetime.now(timezone.utc).isoformat()
        rows = [(x[1], x[2], x[3], x[4], x[0], now) for x in ofz + corp]
        self._store.replace_favorites(rows)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        self._store.prune_favorites_older_than(cutoff)
        logger.info(
            "Task1 обновил favorites: OFZ=%s CORP=%s",
            [f"{x[4]}:{round(x[0] * 100, 2)}%" for x in ofz],
            [f"{x[4]}:{round(x[0] * 100, 2)}%" for x in corp],
        )

    def _task2_modes(self) -> None:
        if avaria_stop:
            return
        row = self._store.get_settings_row()
        if not row:
            logger.warning("Task2 пропущен: настройки стратегии не сохранены")
            return
        mode = row["mode"]
        br = self._broker()
        _, positions = br.portfolio_rub_value()
        fav_rows = self._store.list_favorites()
        if not fav_rows:
            logger.info("Task2: избранное пока пустое, торговые действия пропущены")
            return
        logger.info(
            "Task2 старт: mode=%s current_positions=%s favorites=%s free_rub=%.2f",
            mode,
            len([p for p in positions if not _is_currency(p)]),
            len(fav_rows),
            self._free_cash_rub(br),
        )
        if mode == "adaptive":
            swaps_done = 0
            for pos in positions:
                if avaria_stop:
                    return
                if not _is_bond(pos):
                    continue
                figi = pos.figi
                cur_y = br.bond_yield_for_ticker(pos.ticker)
                pos_kind = "OFZ" if is_ofz_ticker(pos.ticker) else "CORP"
                cands = [
                    r for r in fav_rows if r["kind"] == pos_kind and r["figi"] != figi
                ]
                if not cands:
                    cands = [r for r in fav_rows if r["figi"] != figi]
                if not cands:
                    continue
                best_row = max(cands, key=lambda r: float(r["net_yield"]))
                new_y = float(best_row["net_yield"])
                lost_nkd = 0.0
                if adaptive_swap_threshold(new_y, cur_y, lost_nkd):
                    lots = int(pos.lots or 0)
                    if lots > 0:
                        logger.info(
                            "Task2 adaptive swap: sell=%s current_yield=%.4f best_candidate=%s new_yield=%.4f lots=%s",
                            pos.ticker,
                            cur_y,
                            best_row["ticker"],
                            new_y,
                            lots,
                        )
                        self._session_market_sell(br, figi, lots)
                        ap = pos.average_price
                        notional = (
                            float(ap.value) * float(pos.balance)
                            if ap and ap.value
                            else 0.0
                        )
                        self._store.add_preliminary_tax(
                            max(0.0, notional * 0.01) * TAX_RATE
                        )
                        if avaria_stop:
                            return
                        best_figi = best_row["figi"]
                        if best_figi != figi:
                            self._session_market_buy(br, best_figi, max(1, lots))
                            swaps_done += 1
            if avaria_stop:
                return
            _, refreshed_positions = br.portfolio_rub_value()
            buys_done = self._buy_new_funds(
                br,
                refreshed_positions,
                fav_rows,
                max_orders=2 if br._use_sandbox else 1,
            )
            logger.info(
                "Task2 adaptive завершён: swaps=%s bootstrap_buys=%s",
                swaps_done,
                buys_done,
            )
        elif mode == "static":
            buys_done = self._buy_new_funds(br, positions, fav_rows, max_orders=1)
            logger.info("Task2 static завершён: buys=%s", buys_done)

    def _buy_new_funds(
        self,
        br: BrokerFacade,
        positions,
        fav_rows: list,
        *,
        max_orders: int,
    ) -> int:
        if avaria_stop or not fav_rows:
            return 0
        held = {p.figi for p in positions}
        rub = self._free_cash_rub(br)
        if rub < 2000:
            logger.info(
                "Покупка новых бумаг пропущена: свободных RUB недостаточно (%.2f)", rub
            )
            return 0
        buys_done = 0
        bought_kinds: set[str] = set()
        ordered_rows = sorted(
            fav_rows, key=lambda r: float(r["net_yield"]), reverse=True
        )
        for distinct_kinds_only in (True, False):
            for row in ordered_rows:
                if avaria_stop:
                    return buys_done
                if buys_done >= max_orders:
                    return buys_done
                if distinct_kinds_only and row["kind"] in bought_kinds:
                    continue
                figi = row["figi"]
                if figi in held:
                    continue
                inst = br.get_instrument_by_figi(figi)
                if not inst:
                    continue
                trading = br.get_trading_status(figi)
                if trading is not None and (
                    not getattr(trading, "api_trade_available_flag", False)
                    or not getattr(trading, "market_order_available_flag", False)
                ):
                    logger.info(
                        "Кандидат пропущен по trading status: ticker=%s figi=%s api_trade=%s market_order=%s",
                        row["ticker"],
                        figi,
                        getattr(trading, "api_trade_available_flag", None),
                        getattr(trading, "market_order_available_flag", None),
                    )
                    continue
                ob = br.get_orderbook(figi, 10)
                px = float(ob.last_price) if ob else 0.0
                if px <= 0:
                    continue
                lot = int(inst.lot_size)
                lots = int(rub // max(1.0, px * lot))
                lots = max(1, min(lots, 5))
                if lots <= 0:
                    continue
                logger.info(
                    "Покупка из favorites: ticker=%s figi=%s lots=%s free_rub_before=%.2f",
                    row["ticker"],
                    figi,
                    lots,
                    rub,
                )
                if not self._session_market_buy(br, figi, lots):
                    logger.info(
                        "Покупка не прошла, пробую следующий инструмент: ticker=%s figi=%s",
                        row["ticker"],
                        figi,
                    )
                    continue
                buys_done += 1
                held.add(figi)
                bought_kinds.add(str(row["kind"]))
                rub = self._free_cash_rub(br)
                if rub < 2000:
                    return buys_done
        return buys_done

    def _session_market_sell(self, br: BrokerFacade, figi: str, lots: int) -> bool:
        if avaria_stop:
            return False
        return br.create_market_order(OrderDirection.ORDER_DIRECTION_SELL, figi, lots)

    def _session_market_buy(self, br: BrokerFacade, figi: str, lots: int) -> bool:
        if avaria_stop:
            return False
        return br.create_market_order(OrderDirection.ORDER_DIRECTION_BUY, figi, lots)

    def _task3_rebalance_and_tax(self) -> None:
        if avaria_stop:
            return
        br = self._broker()
        total, positions = br.portfolio_rub_value()
        if total <= 0:
            logger.info("Task3 пропущен: портфель пуст или недоступен")
            return
        self._sync_price_logs(br, positions)
        div = br.diversification()
        logger.info(
            "Task3 анализ портфеля: total_rub=%.2f diversification=%s", total, div
        )
        gold_share = div.get("Gold", 0.0) / 100.0
        if gold_share > GOLD_OVERWEIGHT_TRIM:
            self._trim_gold(br, positions, gold_share)
        self._buy_the_dip(br, positions, total, div)
        self._tax_loss_harvest(br, positions)

    def _trim_gold(self, br: BrokerFacade, positions, gold_share: float) -> None:
        if avaria_stop:
            return
        excess = gold_share - TARGET_WEIGHT
        if excess <= 0:
            return
        gold_pos = next((p for p in positions if p.ticker.upper() == GOLD_TICKER), None)
        if not gold_pos or not gold_pos.lots:
            return
        lots_to_sell = max(1, int(gold_pos.lots * excess / gold_share))
        self._session_market_sell(
            br, gold_pos.figi, min(lots_to_sell, int(gold_pos.lots))
        )

    def _buy_the_dip(
        self,
        br: BrokerFacade,
        positions,
        total: float,
        div: Dict[str, float],
    ) -> None:
        if avaria_stop:
            return
        for label, key in (("OFZ", "OFZ"), ("BONDS", "Bonds"), ("GOLD", "Gold")):
            share = div.get(key, 0.0) / 100.0
            if share >= TARGET_WEIGHT * 0.95:
                continue
            target = self._find_position_for_bucket(br, positions, label)
            if not target:
                continue
            log = self._store.latest_price_log(target.figi)
            if not log:
                continue
            px_now = float(getattr(target.average_price, "value", 0) or 0)
            if px_now <= 0:
                continue
            if px_now >= float(log["avg_buy_price"]) * 0.98:
                continue
            rub_cash = self._free_cash_rub(br)
            if rub_cash < 1000:
                return
            inst = br.get_instrument_by_figi(target.figi)
            if not inst:
                continue
            lot = inst.lot_size
            price = px_now
            lots = int(min(10, rub_cash // max(1.0, price * lot)))
            if lots > 0:
                self._session_market_buy(br, target.figi, lots)

    def _find_position_for_bucket(self, br: BrokerFacade, positions, label: str):
        for p in positions:
            if _is_currency(p):
                continue
            cls = br.classify_position(p)
            if label == "OFZ" and cls == "OFZ":
                return p
            if label == "BONDS" and cls == "BONDS":
                return p
            if label == "GOLD" and cls == "GOLD":
                return p
        return None

    def _free_cash_rub(self, br: BrokerFacade) -> float:
        if avaria_stop:
            return 0.0
        for c in br.get_portfolio_currencies():
            if c.name.value == "RUB":
                return float(c.balance.value) - float(getattr(c, "blocked", 0) or 0.0)
        return 0.0

    def _tax_loss_harvest(self, br: BrokerFacade, positions) -> None:
        if avaria_stop:
            return
        state = self._store.get_tax_state()
        if float(state["preliminary_tax"]) <= float(state["tax_limit"]):
            return
        now = int(time.time())
        if now - int(state["last_harvest_unix"] or 0) < HARVEST_COOLDOWN_SEC:
            return
        gold_pos = next((p for p in positions if p.ticker.upper() == GOLD_TICKER), None)
        if not gold_pos:
            return
        avg = gold_pos.average_price
        px = float(avg.value) if avg and avg.value is not None else 0.0
        last = br.get_orderbook(gold_pos.figi, 10)
        cur = float(last.last_price) if last else px
        if cur >= px:
            return
        tax_saved = float(state["preliminary_tax"])
        trade_cost_frac = 2.0 * COMMISSION_GOLD
        notional = cur * float(gold_pos.balance)
        if tax_saved <= trade_cost_frac * notional:
            return
        lots = int(gold_pos.lots or 0)
        if lots <= 0:
            return
        self._session_market_sell(br, gold_pos.figi, lots)
        if avaria_stop:
            return
        self._session_market_buy(br, gold_pos.figi, lots)
        self._store.reset_preliminary_tax()
        self._store.set_last_harvest_unix(now)

    def _sync_price_logs(self, br: BrokerFacade, positions) -> None:
        if avaria_stop:
            return
        now = datetime.now(timezone.utc).isoformat()
        for p in positions:
            if avaria_stop:
                return
            if _is_currency(p):
                continue
            bucket = br.classify_position(p)
            if bucket == "OTHER":
                continue
            ap = p.average_price
            avg = float(ap.value) if ap and ap.value is not None else 0.0
            if avg <= 0:
                continue
            self._store.append_price_log(
                p.figi,
                bucket,
                avg,
                float(p.balance),
                now,
            )


_engine: Optional[StrategyEngine] = None


def get_engine(store: VaultStore, token_getter: Callable[[], str]) -> StrategyEngine:
    global _engine
    if _engine is None:
        _engine = StrategyEngine(store, token_getter)
    return _engine


def reset_engine() -> None:
    global _engine
    _engine = None


async def halt_strategy_engine() -> None:
    global _engine
    if _engine is None:
        return
    await _engine.stop()
    _engine = None
