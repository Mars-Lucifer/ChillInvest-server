# ChillInvest-server

Автономный сервер на **FastAPI** + **T-Invest API v2** (пакет **`t-tech-investments`**, импорт **`t_tech.invest`**, gRPC). Локальная SQLite, HTTP Basic.

- Репозиторий SDK: [invest-python](https://opensource.tbank.ru/invest/invest-python)  
- Документация API: [developer.tbank.ru/invest/api](https://developer.tbank.ru/invest/api)

## Установка

Зависимости тянутся с индекса Т‑Банка (см. `requirements.txt`: `--extra-index-url ...`). Клиент в коде: `from t_tech.invest import Client`.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Отдельно (эквивалентно верхней строке в `requirements.txt`):

```bash
pip install t-tech-investments --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
```

Токен: [Т‑Банк → токен для Invest API](https://developer.tbank.ru/invest/intro/intro/).

## Первый запуск и профиль

При **первом** запуске `python main.py` (или `uvicorn main:app`), если профиля ещё нет, в **том же терминале** запрашиваются:

1. Логин (латиница, до 16 символов)  
2. Пароль и повтор (8–16 символов)  
3. **Песочница?** (`y` / `N`) — для токена **только** из [песочницы Invest API](https://developer.tbank.ru/invest/intro/intro) нужно ответить `y`, иначе запросы пойдут на **боевой** контур и песочничный токен будет отклонён (40003).  
4. Токен Invest API (ввод скрыт)

Данные пишутся в каталог `data/` (`vault.sqlite3`, `machine.key`).

Если процесс запущен **без интерактивного stdin** (например, как служба) и профиля нет, сервер завершится с кодом 2 — тогда один раз выполните вручную:

```bash
python auth.py
```

## Запуск API

```bash
python main.py
```

По умолчанию: `http://127.0.0.1:8000`. Документация Swagger: `http://127.0.0.1:8000/docs`.

---

## Тестирование API (кратко)

Все защищённые маршруты требуют **HTTP Basic**: пользователь = сохранённый логин, пароль = сохранённый пароль.

### `POST /settings`

**Назначение:** сохранить параметры стратегии, снять аварийный стоп, подключить первый брокерский счёт и запустить фоновый цикл (суточный пайплайн).

**Тело (JSON):**

| Поле | Тип | Обязательно | Описание |
|------|-----|-------------|----------|
| `duration` | string (ISO 8601 datetime) или `null` | нет | Дата окончания работы цикла; `null` — без ограничения по дате |
| `mode` | string | да | `"static"` или `"adaptive"` |
| `profit_withdraw_percent` | number | да | Доля портфеля под резерв вывода прибыли, **0–100** |
| `use_sandbox` | bool или `null` | нет | Если `true` / `false` — сохранить режим песочницы (`true` = хост `sandbox-invest-public-api`, токен песочницы). `null` — не менять сохранённый режим |

**Пример (curl):**

```bash
curl -u "MYLOGIN:mypassword" -H "Content-Type: application/json" ^
  -d "{\"duration\": null, \"mode\": \"static\", \"profit_withdraw_percent\": 10, \"use_sandbox\": true}" ^
  http://127.0.0.1:8000/settings
```

**Ответ:** JSON с полями `ok`, `account_id`, `started`, `use_sandbox`.

---

### `POST /emergency_stop`

**Назначение:** аварийная остановка — отмена активных заявок (рыночные/лимитные и стопы) через API, установка глобального `avaria_stop`, остановка воркера стратегии.

**Тело:** не требуется.

**Пример:**

```bash
curl -u "MYLOGIN:mypassword" -X POST http://127.0.0.1:8000/emergency_stop
```

**Ответ:** `{"ok": true, "avaria_stop": true}`.

Повторный запуск стратегии: снова вызвать `POST /settings` (стоп при этом снимается в обработчике настроек).

---

### `GET /status/main`

**Назначение:** краткий статус портфеля.

**Параметры:** нет (только Basic auth).

**Ответ (пример структуры):**

| Поле | Описание |
|------|----------|
| `portfolio_rub` | Оценка портфеля в рублях |
| `month_income_rub` | Сумма полей `payment` по операциям за текущий месяц (упрощённо) |
| `diversification` | Доли `OFZ`, `Bonds`, `Gold` в процентах |

```bash
curl -u "MYLOGIN:mypassword" http://127.0.0.1:8000/status/main
```

---

### `GET /status/analytics`

**Назначение:** расширенная аналитика: синхронизация операций в SQLite, выплаты за месяц, доходность (грубо по суммам операций), резерв под вывод, предварительный налог, история из кэша.

**Параметры:** нет.

**Ответ (основные поля):**

| Поле | Описание |
|------|----------|
| `month_payouts_rub` | Сумма операций типа купон/дивиденд за месяц |
| `upcoming_payments` | Зарезервировано под будущие выплаты (сейчас может быть пустым списком) |
| `return_all_time_rub` | Сумма `payment` по операциям за длинный период (оценка) |
| `return_month_rub` | То же за текущий месяц |
| `withdraw_reserve_rub` | Резерв = портфель × `profit_withdraw_percent` / 100 |
| `preliminary_tax_rub` | Накопленный «предварительный налог» из логики стратегии |
| `operations_history` | Массив объектов операций из локального кэша |

```bash
curl -u "MYLOGIN:mypassword" http://127.0.0.1:8000/status/analytics
```

---

## Примечания

- Клиент брокера: **`t_tech.invest.Client`** (пакет `t-tech-investments`, Invest API v2).  
- Интерактивная настройка при старте `main` работает только если **stdin — TTY** (обычный терминал).
