import logging
import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi import _rate_limit_exceeded_handler
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.database import Base, engine, get_db
from backend.app.limiter import limiter
from backend.app.middleware import RequestProtectionMiddleware
from backend.app.outbox import deliver_pending
from backend.app.site_service import site_info as read_site_info
from backend.app.worker import maintenance
from backend.app.routers import admin, auth, bookings, contact, payments, rooms


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.environment != "production" and settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(bind=engine)
    if not settings.jwt_secret or len(settings.jwt_secret) < 32:
        logger.warning("Admin login is disabled until JWT_SECRET is configured (minimum 32 characters).")
    if settings.environment == "production" and settings.rate_limit_storage_uri == "memory://":
        logger.warning("Rate limits are per process. Configure shared Redis storage before running multiple API instances.")
    yield


def dispatch_notifications(db):
    # Some framework versions finalize yield dependencies after background tasks.
    # Release the request's transaction before opening the outbox worker session.
    db.close()
    try:
        deliver_pending(limit=2)
    except Exception:
        logger.warning("Email dispatch deferred; scheduled maintenance will retry.")


def deliver_after_mutation(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if request.method in {"POST", "PUT", "DELETE"} and not request.url.path.startswith("/api/internal/"):
        background_tasks.add_task(dispatch_notifications, db)


app = FastAPI(
    title="Devantara Resort API",
    description="Resort availability, booking, payments and administration API.",
    version="1.1.0",
    docs_url=None if settings.environment == "production" else "/docs",
    redoc_url=None if settings.environment == "production" else "/redoc",
    openapi_url=None if settings.environment == "production" else "/openapi.json",
    dependencies=[Depends(deliver_after_mutation)],
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Razorpay-Signature", "X-Razorpay-Event-Id", "Idempotency-Key"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
app.add_middleware(RequestProtectionMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError):
    # Do not echo submitted values such as passwords or contact details in API errors.
    errors = [
        {"loc": error.get("loc"), "msg": error.get("msg"), "type": error.get("type")}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


for route in (auth.router, rooms.router, bookings.router, payments.router, payments.admin_router, contact.router, admin.router):
    app.include_router(route)


@app.exception_handler(IntegrityError)
async def database_conflict(_request: Request, _exc: IntegrityError):
    return JSONResponse(status_code=409, content={"detail": "This change conflicts with an existing record. Refresh and retry."})


@app.exception_handler(OperationalError)
async def database_unavailable(_request: Request, _exc: OperationalError):
    return JSONResponse(status_code=503, content={"detail": "The service is temporarily unavailable. Please retry shortly."}, headers={"Retry-After": "5"})


@app.get("/api/health/live", tags=["health"])
@limiter.exempt
def liveness():
    return {"status": "ok"}


@app.get("/api/health", tags=["health"])
@limiter.exempt
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/site-info", tags=["site information"])
def site_info(db: Session = Depends(get_db)):
    return {**read_site_info(db), "online_payments_enabled": bool(settings.razorpay_key_id and settings.razorpay_key_secret)}


@app.post("/api/internal/maintenance", include_in_schema=False)
@limiter.exempt
def scheduled_maintenance(authorization: str | None = Header(default=None)):
    if not settings.maintenance_secret:
        raise HTTPException(status_code=404, detail="Not found.")
    expected = f"Bearer {settings.maintenance_secret}"
    if not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Authentication required.")
    return maintenance(email_limit=5)


@app.get("/", include_in_schema=False)
def website_home():
    return FileResponse(PROJECT_ROOT / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(PROJECT_ROOT / "admin.html")


@app.get("/googlef17034cc86bbb01d.html", include_in_schema=False)
@limiter.exempt
def google_site_verification():
    return FileResponse(PROJECT_ROOT / "googlef17034cc86bbb01d.html", media_type="text/html")


@app.get("/styles.css", include_in_schema=False)
def website_styles():
    return FileResponse(PROJECT_ROOT / "styles.css", media_type="text/css")


@app.get("/script.js", include_in_schema=False)
def website_script():
    return FileResponse(PROJECT_ROOT / "script.js", media_type="text/javascript")


@app.get("/admin.css", include_in_schema=False)
def admin_styles():
    return FileResponse(PROJECT_ROOT / "admin.css", media_type="text/css")


@app.get("/admin.js", include_in_schema=False)
def admin_script():
    return FileResponse(PROJECT_ROOT / "admin.js", media_type="text/javascript")


@app.get("/devantara-resort.jpg", include_in_schema=False)
def resort_image():
    return FileResponse(PROJECT_ROOT / "devantara-resort.jpg", media_type="image/jpeg")


@app.get("/file_00000000efec820b99f93b6e6968371f.png", include_in_schema=False)
def resort_logo():
    return FileResponse(PROJECT_ROOT / "file_00000000efec820b99f93b6e6968371f.png", media_type="image/png")
