import json
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
import uvicorn

from analytics import build_analyze_payload, build_data_payload
from app_state import (
    check_avaria,
    get_avaria_stop,
    get_logger,
    log_execution,
    set_avaria_stop,
)
from auth import ensure_initial_setup, get_user_mode, initialize_database, verify_credentials
from strategy import (
    TinkoffClient,
    get_strategy_settings,
    router as strategy_router,
    strategy_runtime,
)

logger = get_logger(__name__)
app = FastAPI(title="ChillInvest Server")
initialize_database()


@app.middleware("http")
async def log_http_requests(request: Request, call_next: Any) -> Response:
    started_at = time.perf_counter()
    logger.info(
        "[API] Request started | method=%s | path=%s | time=%s",
        request.method,
        request.url.path,
        time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    try:
        response = await call_next(request)
    except Exception as error:
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
        logger.exception(
            "[API] Request failed | method=%s | path=%s | duration_ms=%s | error=%s",
            request.method,
            request.url.path,
            elapsed_ms,
            error,
        )
        raise

    response_body = b""
    async for chunk in response.body_iterator:
        response_body += chunk
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
    body_preview = response_body.decode("utf-8", errors="replace")
    if len(body_preview) > 1000:
        body_preview = f"{body_preview[:1000]}..."
    logger.info(
        "[API] Request finished | method=%s | path=%s | status=%s | duration_ms=%s | body=%s",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        body_preview,
    )
    return Response(
        content=response_body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
    )


@app.on_event("startup")
@log_execution
async def resume_strategy_on_startup() -> None:
    settings = get_strategy_settings()
    if not settings:
        logger.info("[MODE] Startup: strategy settings are missing, runtime not started")
        return

    if not settings["is_active"]:
        logger.info("[MODE] Startup: strategy is inactive, runtime not started")
        return

    logger.info(
        "[MODE] Startup: resuming active strategy for %s",
        settings["started_by"],
    )
    strategy_runtime.start(
        login=settings["started_by"],
        sandbox_mode=get_user_mode(settings["started_by"]),
    )


@log_execution
def verify_avaria_status() -> None:
    if get_avaria_stop():
        raise HTTPException(
            status_code=503,
            detail="System is stopped due to emergency",
        )


@check_avaria
@log_execution
def run_test_action() -> dict[str, str] | None:
    return {"status": "ok", "message": "Test action completed"}


@app.post("/emergency_stop")
@log_execution
async def toggle_emergency_stop(
    user: dict[str, object] = Depends(verify_credentials),
) -> dict[str, bool | str]:
    result = await toggle_avaria_stop(user)
    return {
        "avaria_stop": result["avaria_stop"],
        "message": "Emergency stop enabled" if result["avaria_stop"] else "Emergency stop disabled",
    }


@app.get("/data", dependencies=[Depends(verify_avaria_status)])
@log_execution
async def get_data(
    user: dict[str, object] = Depends(verify_credentials),
) -> dict[str, Any]:
    return build_data_payload(
        login=str(user["login"]),
        sandbox_mode=bool(user["sandbox_mode"]),
    )


@app.get("/analyze", dependencies=[Depends(verify_avaria_status)])
@log_execution
async def analyze_portfolio(
    user: dict[str, object] = Depends(verify_credentials),
) -> dict[str, Any]:
    return build_analyze_payload(
        login=str(user["login"]),
        sandbox_mode=bool(user["sandbox_mode"]),
    )


@app.delete("/avaria_stop")
@log_execution
async def toggle_avaria_stop(
    user: dict[str, object] = Depends(verify_credentials),
) -> dict[str, bool]:
    new_state = not get_avaria_stop()
    set_avaria_stop(new_state)

    if new_state:
        client = TinkoffClient(
            login=str(user["login"]),
            sandbox=bool(user["sandbox_mode"]),
        )
        cancelled = client.cancel_all_active_orders()
        logger.critical(
            "[CRITICAL] Аварийный стоп включен пользователем. Отменено заявок: %s",
            json.dumps(cancelled, ensure_ascii=False),
        )
    else:
        logger.info("[INFO] Аварийный стоп снят. Система возвращается в штатный режим.")

    return {"avaria_stop": new_state}


@app.get("/test_action", dependencies=[Depends(verify_avaria_status)])
@log_execution
async def test_action(
    user: dict[str, object] = Depends(verify_credentials),
) -> dict[str, str]:
    result = run_test_action()
    if result is None:
        raise HTTPException(
            status_code=503,
            detail="Action blocked by emergency stop",
        )
    return result


app.include_router(strategy_router)


if __name__ == "__main__":
    ensure_initial_setup()
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
