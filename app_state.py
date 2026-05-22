import inspect
import json
import logging
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "app.log"
AVARIA_STATE_FILE = BASE_DIR / "avaria_state.json"

LOG_FORMAT = "[%(name)s] [%(levelname)s] [%(asctime)s] -> %(message)s"
_LOGGING_CONFIGURED = False
RESET_COLOR = "\033[0m"
LEVEL_COLORS = {
    logging.DEBUG: "\033[36m",
    logging.INFO: "\033[37m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[35m",
}
TAG_COLORS = {
    "[BUY]": "\033[32m",
    "[SELL]": "\033[31m",
    "[SIGNAL]": "\033[33m",
    "[FAVORITES]": "\033[36m",
    "[MODE]": "\033[35m",
}

F = TypeVar("F", bound=Callable[..., Any])
AVARIA_STOP: bool = False


class ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        color = LEVEL_COLORS.get(record.levelno, "")
        for tag, tag_color in TAG_COLORS.items():
            if tag in record.getMessage():
                color = tag_color
                break
        if not color:
            return formatted
        return f"{color}{formatted}{RESET_COLOR}"


def setup_logging() -> None:
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    formatter = logging.Formatter(LOG_FORMAT)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    has_console_handler = any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        for handler in root_logger.handlers
    )
    has_file_handler = any(
        isinstance(handler, logging.FileHandler)
        and Path(getattr(handler, "baseFilename", "")).resolve() == LOG_FILE.resolve()
        for handler in root_logger.handlers
    )

    if not has_console_handler:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(ColorFormatter(LOG_FORMAT))
        root_logger.addHandler(console_handler)

    if not has_file_handler:
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    _LOGGING_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, tag: str, message: str, *args: Any) -> None:
    rendered_message = message % args if args else message
    logger.log(level, "%s %s", tag, rendered_message)


def load_avaria_state() -> bool:
    logger = get_logger(__name__)
    logger.info("Начало load_avaria_state")
    if not AVARIA_STATE_FILE.exists():
        logger.info("Файл аварийного состояния отсутствует, используется False")
        logger.info("Успешное завершение load_avaria_state")
        return False

    try:
        with AVARIA_STATE_FILE.open("r", encoding="utf-8") as state_file:
            payload = json.load(state_file)
        state = bool(payload.get("avaria_stop", False))
        logger.info("Успешное завершение load_avaria_state")
        return state
    except (OSError, json.JSONDecodeError) as error:
        logger.error("Не удалось загрузить аварийное состояние: %s", error)
        logger.info("Успешное завершение load_avaria_state c fallback=False")
        return False


def save_avaria_state(value: bool) -> None:
    logger = get_logger(__name__)
    logger.info("Начало save_avaria_state")
    with AVARIA_STATE_FILE.open("w", encoding="utf-8") as state_file:
        json.dump({"avaria_stop": value}, state_file, ensure_ascii=False, indent=2)
    logger.info("Успешное завершение save_avaria_state")


def set_avaria_stop(value: bool) -> bool:
    global AVARIA_STOP
    logger = get_logger(__name__)
    logger.info("Начало set_avaria_stop")
    AVARIA_STOP = value
    save_avaria_state(value)
    logger.info("Успешное завершение set_avaria_stop")
    return AVARIA_STOP


def get_avaria_stop() -> bool:
    return AVARIA_STOP


def initialize_runtime_state() -> None:
    global AVARIA_STOP
    logger = get_logger(__name__)
    logger.info("Начало initialize_runtime_state")
    AVARIA_STOP = load_avaria_state()
    logger.info("Успешное завершение initialize_runtime_state")


def log_execution(func: F) -> F:
    logger = get_logger(func.__module__)

    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            logger.info("Начало %s", func.__name__)
            result = await func(*args, **kwargs)
            logger.info("Успешное завершение %s", func.__name__)
            return result

        return async_wrapper  # type: ignore[return-value]

    @wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        logger.info("Начало %s", func.__name__)
        result = func(*args, **kwargs)
        logger.info("Успешное завершение %s", func.__name__)
        return result

    return sync_wrapper  # type: ignore[return-value]


def check_avaria(func: F) -> F:
    logger = get_logger(func.__module__)

    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            if get_avaria_stop():
                logger.warning(
                    "Вызов функции %s заблокирован: АВАРИЙНЫЙ СТОП",
                    func.__name__,
                )
                return None
            return await func(*args, **kwargs)

        return async_wrapper  # type: ignore[return-value]

    @wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        if get_avaria_stop():
            logger.warning(
                "Вызов функции %s заблокирован: АВАРИЙНЫЙ СТОП",
                func.__name__,
            )
            return None
        return func(*args, **kwargs)

    return sync_wrapper  # type: ignore[return-value]


initialize_runtime_state()
