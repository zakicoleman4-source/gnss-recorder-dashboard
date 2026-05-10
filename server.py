from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from gnss_db import db_session, get_date_range, get_receivers, week_coverage


APP_DIR = Path(__file__).parent.resolve()
SITE_DIR = APP_DIR / "site"
security = HTTPBasic()


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _parse_iso_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid date (expected YYYY-MM-DD): {s}") from e


def _get_auth_config() -> tuple[Optional[str], Optional[str]]:
    """
    If GNSS_DASH_USER and GNSS_DASH_PASS are set, the server requires HTTP Basic Auth.
    """
    user = os.environ.get("GNSS_DASH_USER")
    pw = os.environ.get("GNSS_DASH_PASS")
    if user and pw:
        return user, pw
    return None, None


def _require_auth(creds: HTTPBasicCredentials = Depends(security)) -> None:
    user, pw = _get_auth_config()
    if not user or not pw:
        return
    ok_user = secrets.compare_digest(creds.username, user)
    ok_pw = secrets.compare_digest(creds.password, pw)
    if not (ok_user and ok_pw):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


def create_app(db_path: Path) -> FastAPI:
    app = FastAPI(title="GNSS Recorder Dashboard Server")

    # Serve the dashboard static site
    if SITE_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(SITE_DIR), html=True), name="static")

    @app.get("/")
    def root(_: None = Depends(_require_auth)):
        idx = SITE_DIR / "index.html"
        if not idx.exists():
            raise HTTPException(status_code=500, detail="Missing site/index.html")
        return FileResponse(str(idx))

    @app.get("/api/meta")
    def api_meta(_: None = Depends(_require_auth)):
        with db_session(db_path) as conn:
            receivers = get_receivers(conn)
            min_d, max_d = get_date_range(conn)
        return {
            "db_path": str(db_path),
            "receivers": receivers,
            "min_date": min_d,
            "max_date": max_d,
        }

    @app.get("/api/receivers")
    def api_receivers(_: None = Depends(_require_auth)):
        with db_session(db_path) as conn:
            receivers = get_receivers(conn)
        return {"receivers": receivers}

    @app.get("/api/week")
    def api_week(
        _: None = Depends(_require_auth),
        receiver: str = Query(..., description="Receiver prefix (from filename leading letters)"),
        week_start: str = Query(..., description="Week start date (YYYY-MM-DD). Any day will be rounded down to Monday."),
    ):
        day = _parse_iso_date(week_start)
        ws = _monday_of(day).isoformat()
        with db_session(db_path) as conn:
            data = week_coverage(conn, receiver_prefix=receiver.upper(), week_start_iso=ws)
        return {"receiver": receiver.upper(), "week_start": ws, "data": data}

    return app


def main() -> int:
    import uvicorn

    db_path = Path(os.environ.get("GNSS_DB_PATH", str(APP_DIR / "gnss.db"))).resolve()
    host = os.environ.get("GNSS_HOST", "127.0.0.1")
    port = int(os.environ.get("GNSS_PORT", "8501"))

    app = create_app(db_path)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

