from fastapi import Depends, FastAPI, HTTPException
import uvicorn

from app_state import (
    check_avaria,
    get_avaria_stop,
    get_logger,
    log_execution,
    set_avaria_stop,
)
from auth import ensure_initial_setup, get_user_mode, initialize_database, verify_credentials
from strategy import (
    get_strategy_settings,
    router as strategy_router,
    strategy_runtime,
)

logger = get_logger(__name__)
app = FastAPI(title="ChillInvest Server")
initialize_database()


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
    new_state = not get_avaria_stop()
    set_avaria_stop(new_state)

    if new_state:
        logger.critical("АВАРИЙНЫЙ СТОП АКТИВИРОВАН!")
    else:
        logger.warning("Аварийный стоп деактивирован")

    return {
        "avaria_stop": new_state,
        "message": "Emergency stop enabled" if new_state else "Emergency stop disabled",
    }


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
