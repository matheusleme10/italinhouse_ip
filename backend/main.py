from __future__ import annotations

import os
import re
import gzip
import json
import shutil
import secrets
import hmac
import hashlib
import base64
import time
import asyncio
import smtplib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .catalog import items_to_csv, normalize_categories
from .ifood_client import IFoodAPIError, IFoodClient

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
DATA_DIR = ROOT / "data"
STAGING_DIR = DATA_DIR / "staging"
CURRENT_DATA = DATA_DIR / "current.json.gz"
BLOB_PATH = "ital-dashboard/current.json.gz"
ACCESS_LOG_PATH = DATA_DIR / "access-logs.jsonl"
ACCESS_LOG_PREFIX = "ital-dashboard/access-logs/"
ACCESS_SETTINGS_PATH = DATA_DIR / "access-settings.json"
ACCESS_SETTINGS_BLOB_PATH = "ital-dashboard/access-settings.json"
NOTIFICATION_SETTINGS_PATH = DATA_DIR / "notification-settings.json"
NOTIFICATION_SETTINGS_BLOB_PATH = "ital-dashboard/notification-settings.json"
FEEDBACK_SETTINGS_PATH = DATA_DIR / "feedback-settings.json"
FEEDBACK_SETTINGS_BLOB_PATH = "ital-dashboard/feedback-settings.json"
PRICE_OVERRIDES_PATH = DATA_DIR / "price-overrides.json"
PRICE_OVERRIDES_BLOB_PATH = "ital-dashboard/price-overrides.json"
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)


def _resolve_blob_token() -> tuple[str, str]:
    """Retorna o token do Vercel Blob, mesmo que a integracao tenha criado a
    variavel com um prefixo customizado (ex: BLOB_READ_WRITE_TOKEN_READ_WRITE_TOKEN
    em vez do nome padrao BLOB_READ_WRITE_TOKEN)."""
    direct = os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
    if direct:
        return direct, "BLOB_READ_WRITE_TOKEN"
    for key, value in os.environ.items():
        if key.endswith("_READ_WRITE_TOKEN") and value.strip():
            return value.strip(), "integration-prefixed"
    return "", "missing"


BLOB_TOKEN, BLOB_TOKEN_SOURCE = _resolve_blob_token()
CURRENT_PAYLOAD_CACHE_TTL_SECONDS = 60
_CURRENT_PAYLOAD_CACHE: dict | None = None
_CURRENT_PAYLOAD_CACHE_AT = 0.0
_CURRENT_PAYLOAD_LOCAL_MTIME_NS: int | None = None


class CloudStorageError(RuntimeError):
    pass


def _is_vercel_runtime() -> bool:
    return bool(os.getenv("VERCEL", "").strip())


def _report_blob_error(operation: str, error: Exception) -> None:
    print(json.dumps({
        "event": "vercel_blob_error",
        "operation": operation,
        "errorType": type(error).__name__,
    }))


async def _blob_result_bytes(result) -> bytes | None:
    """Extrai o corpo nas versões atuais e anteriores do SDK do Vercel Blob."""
    if result is None or getattr(result, "status_code", 0) != 200:
        return None
    content = getattr(result, "content", None)
    if isinstance(content, bytes):
        return content
    stream = getattr(result, "stream", None)
    if stream is None:
        return None
    return b"".join([chunk async for chunk in stream])


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ifood = IFoodClient()
    yield


app = FastAPI(
    title="Ital in House Dashboard API",
    docs_url="/api/docs" if os.getenv("NODE_ENV") != "production" else None,
    redoc_url=None,
    lifespan=lifespan,
)
origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class SyncRequest(BaseModel):
    merchantId: str


class DataAction(BaseModel):
    action: str
    uploadId: str | None = None
    chunkIndex: int | None = None
    rows: list[dict] | None = None
    totalRows: int | None = None
    uploadedAt: str | None = None


class LoginRequest(BaseModel):
    password: str


class NotificationRequest(BaseModel):
    message: str | None = None
    subject: str | None = None


class NotificationSettingsRequest(BaseModel):
    autoEnabled: bool
    emailRecipients: list[str] = Field(default_factory=list)
    senderEmail: str = ""
    senderName: str = "Ital in House"
    messageTemplate: str = ""


class NotificationTestRequest(BaseModel):
    recipient: str


class FeedbackSettingsRequest(BaseModel):
    sheetCsvUrl: str = ""


class PotentialAccessRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class FranchiseIdentityRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=5, max_length=254)


class FranchiseContextRequest(BaseModel):
    brandId: str = Field(min_length=1, max_length=30)
    store: str = Field(min_length=2, max_length=180)


class AccessSettingsRequest(BaseModel):
    allowedDomains: list[str] = Field(default_factory=list, max_length=100)


class PriceOverrideRequest(BaseModel):
    store: str = Field(min_length=1, max_length=180)
    item: str = Field(min_length=1, max_length=300)
    categoria: str = ""
    price: float = Field(gt=0, le=100000)


def client() -> IFoodClient:
    return app.state.ifood


def api_error(error: IFoodAPIError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=str(error))


SESSION_COOKIE = "ital_portal_session"
POTENTIAL_COOKIE = "ital_potential_access"
IDENTITY_COOKIE = "ital_franchise_identity"
FRANCHISE_CONTEXT_COOKIE = "ital_franchise_context"
SESSION_TTL_SECONDS = 8 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LAST_NOTIFICATION: dict = {"status": "never", "sentAt": None, "emailCount": 0, "whatsappCount": 0}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Botão admin "Atualizar dados" — dispara o workflow_dispatch do GitHub
# Actions (mesmo workflow do sync automático, .github/workflows/sync-postgres.yml)
# a partir do backend, nunca do navegador: o PAT do GitHub só existe aqui,
# como variável de ambiente do servidor (GITHUB_SYNC_TOKEN). O frontend nunca
# vê esse token — só chama POST /api/data/sync-now, que está atrás de
# require_session("admin").
GITHUB_SYNC_TOKEN = os.getenv("GITHUB_SYNC_TOKEN", "").strip()
GITHUB_SYNC_REPO = os.getenv("GITHUB_SYNC_REPO", "matheusleme10/italinhouse_ip").strip()
GITHUB_SYNC_WORKFLOW = os.getenv("GITHUB_SYNC_WORKFLOW", "sync-postgres.yml").strip()
GITHUB_SYNC_REF = os.getenv("GITHUB_SYNC_REF", "main").strip()
SYNC_TRIGGER_COOLDOWN_SECONDS = 120
SYNC_TRIGGER_STATE: dict = {
    "lastTriggeredAt": 0.0,
    "inFlight": False,
    "lastTriggeredBy": None,
    # Preenchidos em trigger_sync_now e lidos em sync_trigger_status pra
    # acompanhar o run real do GitHub Actions (ver _find_matching_run /
    # _map_run_status_to_outcome). triggeredAtIso é o carimbo do disparo;
    # uploadedAtAtTrigger é o uploadedAt do payload ANTES do disparo, usado
    # só pra distinguir "concluiu sem dado novo" de "concluiu com dado novo".
    "triggeredAtIso": None,
    "uploadedAtAtTrigger": None,
}


def _session_secret() -> bytes:
    secret = os.getenv("SESSION_SECRET", "").strip()
    if len(secret) < 32:
        raise HTTPException(status_code=503, detail="Sessão segura não configurada.")
    return secret.encode("utf-8")


def _password_role(password: str) -> str | None:
    supplied = hashlib.sha256(password.strip().encode("utf-8")).hexdigest()
    admin_hash = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
    franchise_hash = os.getenv("FRANCHISE_PASSWORD_HASH", "").strip()
    if admin_hash and hmac.compare_digest(supplied, admin_hash):
        return "admin"
    if franchise_hash and hmac.compare_digest(supplied, franchise_hash):
        return "franchise"
    return None


def _create_session(role: str) -> str:
    payload = f"{role}:{int(time.time()) + SESSION_TTL_SECONDS}"
    signature = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode("utf-8")).decode("ascii")


def _create_potential_access() -> str:
    payload = f"potential:{int(time.time()) + SESSION_TTL_SECONDS}"
    signature = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode("utf-8")).decode("ascii")


def _create_identity(name: str, email: str) -> str:
    identity = {
        "name": name,
        "email": email,
        "expires": int(time.time()) + SESSION_TTL_SECONDS,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(_session_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _get_identity(request: Request) -> dict | None:
    token = request.cookies.get(IDENTITY_COOKIE, "")
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(_session_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        padding = "=" * (-len(encoded) % 4)
        identity = json.loads(base64.urlsafe_b64decode(f"{encoded}{padding}").decode("utf-8"))
        if int(identity.get("expires", 0)) <= int(time.time()):
            return None
        if not identity.get("name") or not EMAIL_PATTERN.match(str(identity.get("email", ""))):
            return None
        return identity
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None


def _create_franchise_context(brand_id: str, store: str) -> str:
    context = {
        "brandId": brand_id,
        "store": store,
        "expires": int(time.time()) + SESSION_TTL_SECONDS,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(context, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(_session_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _get_franchise_context(request: Request) -> dict | None:
    token = request.cookies.get(FRANCHISE_CONTEXT_COOKIE, "")
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(_session_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        padding = "=" * (-len(encoded) % 4)
        context = json.loads(base64.urlsafe_b64decode(f"{encoded}{padding}").decode("utf-8"))
        if int(context.get("expires", 0)) <= int(time.time()):
            return None
        if not context.get("brandId") or not context.get("store"):
            return None
        return context
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None


def _access_event(request: Request, identity: dict, action: str, **details) -> dict:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    address = forwarded or (request.client.host if request.client else "unknown")
    address_hash = hmac.new(_session_secret(), address.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return {
        "id": secrets.token_hex(12),
        "name": identity["name"],
        "email": identity["email"],
        "action": action,
        "accessedAt": datetime.now(timezone.utc).isoformat(),
        "clientHash": address_hash,
        "userAgent": request.headers.get("user-agent", "")[:220],
        **details,
    }


async def _save_access_event(event: dict) -> None:
    serialized = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if BLOB_TOKEN:
        from vercel.blob import AsyncBlobClient

        reverse_timestamp = 9_999_999_999_999 - int(time.time() * 1000)
        path = f"{ACCESS_LOG_PREFIX}{reverse_timestamp:013d}-{event['id']}.json"
        async with AsyncBlobClient(token=BLOB_TOKEN) as blob_client:
            await blob_client.put(
                path,
                serialized,
                access="private",
                content_type="application/json",
                overwrite=True,
                cache_control_max_age=0,
            )
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with ACCESS_LOG_PATH.open("a", encoding="utf-8") as file:
        file.write(serialized.decode("utf-8") + "\n")


async def _record_access_event(event: dict) -> bool:
    """Auditoria não pode impedir o franqueado de acessar o portal."""
    try:
        await _save_access_event(event)
        return True
    except Exception as error:
        _report_blob_error("access-log-write", error)
        return False


async def _read_access_events(limit: int = 250) -> list[dict]:
    if BLOB_TOKEN:
        from vercel.blob import AsyncBlobClient

        async with AsyncBlobClient(token=BLOB_TOKEN) as blob_client:
            listing = await blob_client.list_objects(prefix=ACCESS_LOG_PREFIX, limit=limit)
            semaphore = asyncio.Semaphore(12)

            async def read_event(item) -> dict | None:
                async with semaphore:
                    result = await blob_client.get(item.pathname, access="private")
                    raw = await _blob_result_bytes(result)
                    try:
                        return json.loads(raw.decode("utf-8")) if raw else None
                    except (UnicodeError, json.JSONDecodeError):
                        return None

            events = await asyncio.gather(*(read_event(item) for item in listing.blobs))
            return sorted((event for event in events if event), key=lambda entry: entry["accessedAt"], reverse=True)
    if not ACCESS_LOG_PATH.exists():
        return []
    events = []
    for line in ACCESS_LOG_PATH.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return sorted(events, key=lambda entry: entry["accessedAt"], reverse=True)


def _normalize_domains(domains: list[str]) -> list[str]:
    normalized = []
    for domain in domains:
        value = str(domain).strip().lower().lstrip("@").rstrip(".")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\.[a-z]{2,63}", value):
            raise HTTPException(status_code=400, detail=f"Domínio de e-mail inválido: {domain}")
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise HTTPException(status_code=400, detail="Cadastre pelo menos um domínio permitido.")
    return normalized


async def read_access_settings() -> dict:
    defaults = {"allowedDomains": ["italinhouse.com"]}
    if BLOB_TOKEN:
        from vercel.blob import AsyncBlobClient, BlobNotFoundError
        try:
            async with AsyncBlobClient(token=BLOB_TOKEN) as blob_client:
                result = await blob_client.get(ACCESS_SETTINGS_BLOB_PATH, access="private")
                raw = await _blob_result_bytes(result)
                return {**defaults, **(json.loads(raw.decode("utf-8")) if raw else {})}
        except BlobNotFoundError:
            return defaults
        except Exception as error:
            _report_blob_error("access-settings-read", error)
            return defaults
    if not ACCESS_SETTINGS_PATH.is_file():
        return defaults
    return {**defaults, **json.loads(ACCESS_SETTINGS_PATH.read_text(encoding="utf-8"))}


async def write_access_settings(settings: dict) -> None:
    serialized = json.dumps(settings, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if BLOB_TOKEN:
        from vercel.blob import AsyncBlobClient
        async with AsyncBlobClient(token=BLOB_TOKEN) as blob_client:
            await blob_client.put(
                ACCESS_SETTINGS_BLOB_PATH, serialized, access="private",
                content_type="application/json", overwrite=True, cache_control_max_age=0,
            )
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ACCESS_SETTINGS_PATH.write_bytes(serialized)


async def delete_access_events() -> int:
    if BLOB_TOKEN:
        from vercel.blob import AsyncBlobClient
        deleted = 0
        async with AsyncBlobClient(token=BLOB_TOKEN) as blob_client:
            while True:
                listing = await blob_client.list_objects(prefix=ACCESS_LOG_PREFIX, limit=1000)
                paths = [item.pathname for item in listing.blobs]
                if not paths:
                    break
                await blob_client.delete(paths)
                deleted += len(paths)
        return deleted
    if not ACCESS_LOG_PATH.exists():
        return 0
    count = len(ACCESS_LOG_PATH.read_text(encoding="utf-8").splitlines())
    ACCESS_LOG_PATH.unlink()
    return count


def _has_potential_access(request: Request) -> bool:
    token = request.cookies.get(POTENTIAL_COOKIE, "")
    try:
        scope, expires, signature = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8").split(":")
        payload = f"{scope}:{expires}"
        expected = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return scope == "potential" and int(expires) > int(time.time()) and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def _login_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def _check_login_rate(request: Request) -> str:
    key = _login_key(request)
    cutoff = time.time() - LOGIN_WINDOW_SECONDS
    LOGIN_ATTEMPTS[key] = [attempt for attempt in LOGIN_ATTEMPTS.get(key, []) if attempt > cutoff]
    if len(LOGIN_ATTEMPTS[key]) >= LOGIN_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail="Muitas tentativas. Aguarde 15 minutos antes de tentar novamente.",
            headers={"Retry-After": str(LOGIN_WINDOW_SECONDS)},
        )
    return key


def require_session(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE, "")
    try:
        role, expires, signature = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8").split(":")
        payload = f"{role}:{expires}"
        expected = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if role not in {"admin", "franchise"} or int(expires) <= int(time.time()):
            raise ValueError
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        return role
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(status_code=401, detail="Não autorizado.")


def redact_paused_revenue(payload: dict) -> dict:
    def redact_row(row: dict) -> dict:
        safe = dict(row)
        if safe.get("status") == "Pausado":
            safe["preco"] = ""
            safe["precoNum"] = 0
        if isinstance(safe.get("catalogRows"), list):
            safe["catalogRows"] = [redact_row(entry) for entry in safe["catalogRows"]]
        if isinstance(safe.get("catalogHistory"), list):
            safe["catalogHistory"] = [redact_row(entry) for entry in safe["catalogHistory"]]
        if isinstance(safe.get("productHistory"), list):
            safe["productHistory"] = [redact_row(entry) for entry in safe["productHistory"]]
        cube = safe.get("catalogCube")
        if isinstance(cube, dict) and isinstance(cube.get("records"), list):
            safe_cube = dict(cube)
            safe_records = []
            for record in cube["records"]:
                safe_record = list(record) if isinstance(record, list) else record
                if isinstance(safe_record, list) and len(safe_record) >= 7 and safe_record[5] == 1:
                    safe_record[6] = 0
                safe_records.append(safe_record)
            safe_cube["records"] = safe_records
            safe["catalogCube"] = safe_cube
        return safe

    safe_payload = dict(payload)
    safe_payload["rows"] = [redact_row(row) for row in payload.get("rows", [])]
    return safe_payload


def _store_key(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def filter_payload_for_store(payload: dict, selected_store: str) -> dict:
    """Return only the selected unit's rows while preserving required metadata."""
    target = _store_key(selected_store)
    source_rows = payload.get("rows", [])
    metadata = next((row for row in source_rows if row.get("networkSummary") or row.get("catalogRows")), {})
    scoped_rows = [dict(row) for row in source_rows if _store_key(row.get("loja")) == target]
    if not scoped_rows:
        scoped_rows = [{"loja": selected_store}]

    meta = dict(metadata)
    meta["networkHistory"] = []
    meta["networkSummary"] = None
    # O ranking do franqueado compara disponibilidade entre unidades. Mantemos
    # os totais operacionais da rede, mas retiramos qualquer valor financeiro
    # das lojas que não são a unidade selecionada.
    meta["unitHistory"] = [
        {
            **entry,
            "pausedRevenue": (entry.get("pausedRevenue", 0) if _store_key(entry.get("label")) == target else 0),
        }
        for entry in metadata.get("unitHistory", [])
    ]
    meta["unitStats"] = [
        entry for entry in metadata.get("unitStats", [])
        if _store_key(entry.get("label") or entry.get("loja")) == target
    ]
    for field in ("catalogRows", "catalogHistory", "productHistory"):
        meta[field] = [
            entry for entry in metadata.get(field, [])
            if _store_key(entry.get("loja")) == target
        ]
    meta["forneriaSummaryHistory"] = []

    cube = metadata.get("catalogCube")
    if isinstance(cube, dict):
        stores = cube.get("stores", [])
        allowed_indexes = {index for index, store in enumerate(stores) if _store_key(store) == target}
        selected_records = [
            record for record in cube.get("records", [])
            if isinstance(record, list) and len(record) >= 7 and record[0] in allowed_indexes
        ]
        dimensions = ["stores", "items", "categories", "dates", "shifts"]
        used_indexes = [{record[position] for record in selected_records} for position in range(5)]
        index_maps = [
            {old_index: new_index for new_index, old_index in enumerate(sorted(indexes))}
            for indexes in used_indexes
        ]
        meta["catalogCube"] = {
            **cube,
            **{
                dimension: [cube.get(dimension, [])[old_index] for old_index in sorted(indexes)]
                for dimension, indexes in zip(dimensions, used_indexes)
            },
            "records": [
                [*(index_maps[position][record[position]] for position in range(5)), *record[5:]]
                for record in selected_records
            ],
        }

    metadata_fields = {
        "networkSummary", "networkHistory", "unitStats", "unitHistory", "dataShift",
        "catalogRows", "catalogHistory", "productHistory", "forneriaSummaryHistory", "catalogCube",
    }
    for row in scoped_rows:
        for field in metadata_fields:
            row.pop(field, None)
    scoped_rows[0].update({field: meta.get(field) for field in metadata_fields if field in meta})
    return {**payload, "rows": scoped_rows, "totalRows": len(scoped_rows)}


async def read_current_payload() -> dict | None:
    global _CURRENT_PAYLOAD_CACHE, _CURRENT_PAYLOAD_CACHE_AT, _CURRENT_PAYLOAD_LOCAL_MTIME_NS
    now = time.monotonic()
    if BLOB_TOKEN and _CURRENT_PAYLOAD_CACHE is not None:
        if now - _CURRENT_PAYLOAD_CACHE_AT < CURRENT_PAYLOAD_CACHE_TTL_SECONDS:
            return _CURRENT_PAYLOAD_CACHE
    elif not BLOB_TOKEN and not _is_vercel_runtime() and CURRENT_DATA.is_file():
        current_mtime_ns = CURRENT_DATA.stat().st_mtime_ns
        if _CURRENT_PAYLOAD_CACHE is not None and current_mtime_ns == _CURRENT_PAYLOAD_LOCAL_MTIME_NS:
            return _CURRENT_PAYLOAD_CACHE

    if BLOB_TOKEN:
        from vercel.blob import AsyncBlobClient, BlobNotFoundError

        try:
            async with AsyncBlobClient(token=BLOB_TOKEN) as blob_client:
                result = await blob_client.get(BLOB_PATH, access="private")
                compressed = await _blob_result_bytes(result)
                if compressed is None:
                    return None
        except BlobNotFoundError:
            return None
        except Exception as error:
            _report_blob_error("read", error)
            raise CloudStorageError(
                "Não foi possível ler a base no Vercel Blob. Confira se o Blob está conectado ao projeto e faça um redeploy."
            ) from error
        payload = json.loads(gzip.decompress(compressed).decode("utf-8"))
        _CURRENT_PAYLOAD_CACHE = payload
        _CURRENT_PAYLOAD_CACHE_AT = now
        _CURRENT_PAYLOAD_LOCAL_MTIME_NS = None
        return payload

    if _is_vercel_runtime():
        return None

    if not CURRENT_DATA.is_file():
        return None
    with gzip.open(CURRENT_DATA, "rt", encoding="utf-8") as source:
        payload = json.load(source)
    _CURRENT_PAYLOAD_CACHE = payload
    _CURRENT_PAYLOAD_CACHE_AT = now
    _CURRENT_PAYLOAD_LOCAL_MTIME_NS = CURRENT_DATA.stat().st_mtime_ns
    return payload


async def write_current_payload(payload: dict) -> None:
    global _CURRENT_PAYLOAD_CACHE, _CURRENT_PAYLOAD_CACHE_AT, _CURRENT_PAYLOAD_LOCAL_MTIME_NS
    compressed = gzip.compress(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=9,
    )
    if BLOB_TOKEN:
        from vercel.blob import AsyncBlobClient

        try:
            async with AsyncBlobClient(token=BLOB_TOKEN) as blob_client:
                await blob_client.put(
                    BLOB_PATH,
                    compressed,
                    access="private",
                    content_type="application/gzip",
                    overwrite=True,
                    cache_control_max_age=60,
                )
        except Exception as error:
            _report_blob_error("write", error)
            raise CloudStorageError(
                "O Vercel Blob recusou a gravação. Reconecte o Blob ao projeto e faça um redeploy sem cache."
            ) from error
        _CURRENT_PAYLOAD_CACHE = payload
        _CURRENT_PAYLOAD_CACHE_AT = time.monotonic()
        _CURRENT_PAYLOAD_LOCAL_MTIME_NS = None
        return

    if _is_vercel_runtime():
        raise CloudStorageError(
            "Vercel Blob não configurado neste deployment. Conecte o Blob ao projeto e faça um redeploy sem cache."
        )

    DATA_DIR.mkdir(exist_ok=True)
    temporary = DATA_DIR / "current.next.json.gz"
    temporary.write_bytes(compressed)
    temporary.replace(CURRENT_DATA)
    _CURRENT_PAYLOAD_CACHE = payload
    _CURRENT_PAYLOAD_CACHE_AT = time.monotonic()
    _CURRENT_PAYLOAD_LOCAL_MTIME_NS = CURRENT_DATA.stat().st_mtime_ns


def _default_notification_settings() -> dict:
    recipients = [
        value.strip().lower()
        for value in os.getenv("NOTIFY_EMAIL_TO", "").split(",")
        if value.strip()
    ]
    return {
        "autoEnabled": os.getenv("AUTO_NOTIFY_ON_UPLOAD", "").strip().lower() in {"1", "true", "yes", "sim"},
        "emailRecipients": list(dict.fromkeys(recipients)),
        "senderEmail": os.getenv("SMTP_FROM", "").strip().lower(),
        "senderName": "Ital in House",
        "messageTemplate": "",
    }


async def read_notification_settings() -> dict:
    defaults = _default_notification_settings()
    if BLOB_TOKEN:
        from vercel.blob import AsyncBlobClient, BlobNotFoundError

        try:
            async with AsyncBlobClient(token=BLOB_TOKEN) as blob_client:
                result = await blob_client.get(NOTIFICATION_SETTINGS_BLOB_PATH, access="private")
                raw = await _blob_result_bytes(result)
                if raw is None:
                    return defaults
        except BlobNotFoundError:
            # Primeiro uso: o arquivo de preferências ainda será criado pelo administrador.
            return defaults
        saved = json.loads(raw.decode("utf-8"))
        return {**defaults, **saved}

    if not NOTIFICATION_SETTINGS_PATH.is_file():
        return defaults
    return {**defaults, **json.loads(NOTIFICATION_SETTINGS_PATH.read_text(encoding="utf-8"))}


async def write_notification_settings(settings: dict) -> None:
    serialized = json.dumps(settings, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if BLOB_TOKEN:
        from vercel.blob import AsyncBlobClient

        async with AsyncBlobClient(token=BLOB_TOKEN) as blob_client:
            await blob_client.put(
                NOTIFICATION_SETTINGS_BLOB_PATH,
                serialized,
                access="private",
                content_type="application/json",
                overwrite=True,
                cache_control_max_age=0,
            )
        return

    DATA_DIR.mkdir(exist_ok=True)
    temporary = DATA_DIR / "notification-settings.next.json"
    temporary.write_bytes(serialized)
    temporary.replace(NOTIFICATION_SETTINGS_PATH)


def _default_feedback_settings() -> dict:
    return {"sheetCsvUrl": ""}


async def read_feedback_settings() -> dict:
    defaults = _default_feedback_settings()
    if BLOB_TOKEN:
        from vercel.blob import AsyncBlobClient, BlobNotFoundError

        try:
            async with AsyncBlobClient(token=BLOB_TOKEN) as blob_client:
                result = await blob_client.get(FEEDBACK_SETTINGS_BLOB_PATH, access="private")
                raw = await _blob_result_bytes(result)
                if raw is None:
                    return defaults
        except BlobNotFoundError:
            # Primeiro uso: o admin ainda não salvou o link da planilha.
            return defaults
        saved = json.loads(raw.decode("utf-8"))
        return {**defaults, **saved}

    if not FEEDBACK_SETTINGS_PATH.is_file():
        return defaults
    return {**defaults, **json.loads(FEEDBACK_SETTINGS_PATH.read_text(encoding="utf-8"))}


async def write_feedback_settings(settings: dict) -> None:
    serialized = json.dumps(settings, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if BLOB_TOKEN:
        from vercel.blob import AsyncBlobClient

        async with AsyncBlobClient(token=BLOB_TOKEN) as blob_client:
            await blob_client.put(
                FEEDBACK_SETTINGS_BLOB_PATH,
                serialized,
                access="private",
                content_type="application/json",
                overwrite=True,
                cache_control_max_age=0,
            )
        return

    DATA_DIR.mkdir(exist_ok=True)
    temporary = DATA_DIR / "feedback-settings.next.json"
    temporary.write_bytes(serialized)
    temporary.replace(FEEDBACK_SETTINGS_PATH)


def price_override_key(store: str, item: str) -> str:
    return f"{' '.join(store.strip().split()).lower()}||{' '.join(item.strip().split()).lower()}"


async def read_price_overrides() -> dict:
    """Preços definidos manualmente por franqueados/admins para itens sem
    preço cadastrado na planilha. Fica num arquivo separado do upload
    principal de propósito: assim uma nova carga de XLSX pelo admin nunca
    apaga os ajustes que a franquia já fez."""
    if BLOB_TOKEN:
        from vercel.blob import AsyncBlobClient, BlobNotFoundError

        try:
            async with AsyncBlobClient(token=BLOB_TOKEN) as blob_client:
                result = await blob_client.get(PRICE_OVERRIDES_BLOB_PATH, access="private")
                raw = await _blob_result_bytes(result)
                if raw is None:
                    return {}
        except BlobNotFoundError:
            return {}
        return json.loads(raw.decode("utf-8"))

    if not PRICE_OVERRIDES_PATH.is_file():
        return {}
    return json.loads(PRICE_OVERRIDES_PATH.read_text(encoding="utf-8"))


async def write_price_overrides(overrides: dict) -> None:
    serialized = json.dumps(overrides, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if BLOB_TOKEN:
        from vercel.blob import AsyncBlobClient

        async with AsyncBlobClient(token=BLOB_TOKEN) as blob_client:
            await blob_client.put(
                PRICE_OVERRIDES_BLOB_PATH,
                serialized,
                access="private",
                content_type="application/json",
                overwrite=True,
                cache_control_max_age=0,
            )
        return

    DATA_DIR.mkdir(exist_ok=True)
    temporary = DATA_DIR / "price-overrides.next.json"
    temporary.write_bytes(serialized)
    temporary.replace(PRICE_OVERRIDES_PATH)


def _notification_context(payload: dict, settings: dict | None = None) -> dict[str, str]:
    rows = payload.get("rows") or []
    metadata = next((row for row in rows if row.get("networkHistory") or row.get("networkSummary")), {})
    dates = [
        *(entry.get("date") for entry in metadata.get("networkHistory", [])),
        *(row.get("dia") for row in rows),
    ]
    date = max((str(value)[:10] for value in dates if value), default=datetime.now().date().isoformat())
    now = datetime.now(ZoneInfo("America/Sao_Paulo"))
    shift = "Almoço" if now.hour < 17 else "Jantar"
    greeting = "Boa tarde" if shift == "Almoço" else "Boa noite"
    formatted_date = datetime.fromisoformat(date).strftime("%d/%m/%Y")
    dashboard_url = os.getenv("DASHBOARD_PUBLIC_URL", "").strip()
    portal_password = os.getenv("DASHBOARD_PORTAL_PASSWORD", "").strip()

    # Se o admin salvou um modelo próprio (ver /api/notifications/settings),
    # só trocamos os pedaços automáticos ({saudacao}/{turno}/{data}/{link}/{senha})
    # e mantemos o resto do texto exatamente como ele escreveu.
    template = ((settings or {}).get("messageTemplate") or "").strip()
    if template:
        message = (
            template
            .replace("{saudacao}", greeting)
            .replace("{turno}", shift)
            .replace("{data}", formatted_date)
            .replace("{link}", dashboard_url)
            .replace("{senha}", portal_password)
        )
    else:
        message = (
            f"{greeting}! O Dashboard de Itens Pausados foi atualizado com os dados de "
            f"{shift}, dia {formatted_date}."
        )
        message += f"\n\nAcesse o portal: {dashboard_url}" if dashboard_url else "\n\nAcesse o portal."
        if portal_password:
            message += f"\nSenha: {portal_password}"
        message += (
            "\n\nAqui você consegue consultar itens ativos, pausados, o ranking da rede e outras métricas."
        )
    return {
        "date": date,
        "formattedDate": formatted_date,
        "shift": shift,
        "greeting": greeting,
        "message": message,
        "subject": f"Dashboard atualizado – {shift} – {formatted_date}",
    }


def _send_emails(subject: str, body: str, recipients: list[str], sender_email: str, sender_name: str) -> int:
    if not recipients:
        return 0
    required = ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"]
    if not all(os.getenv(key, "").strip() for key in required):
        raise RuntimeError(
            "Servidor de e-mail não configurado. Preencha SMTP_HOST, SMTP_USER e SMTP_PASSWORD "
            "nas variáveis de ambiente."
        )
    if not sender_email:
        raise RuntimeError("Informe o e-mail remetente na página Avisos.")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{sender_name} <{sender_email}>" if sender_name else sender_email
    message["To"] = sender_email
    message["Bcc"] = ", ".join(recipients)
    message.set_content(body)
    host = os.environ["SMTP_HOST"].strip()
    username = os.environ["SMTP_USER"].strip()
    password = os.environ["SMTP_PASSWORD"].strip()
    if host.lower() in {"smtp.gmail.com", "smtp.googlemail.com"}:
        password = password.replace(" ", "")
    port = int(os.getenv("SMTP_PORT", "465"))
    security = os.getenv("SMTP_SECURITY", "ssl" if port == 465 else "starttls").strip().lower()
    try:
        if security == "starttls":
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
                smtp.login(username, password)
                smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as error:
        provider_hint = (
            " Para Gmail, use o e-mail completo em SMTP_USER e uma Senha de app de 16 caracteres; "
            "a senha normal da conta não funciona."
            if host.lower() in {"smtp.gmail.com", "smtp.googlemail.com"}
            else " Confira o usuário e a senha SMTP fornecidos pelo seu provedor."
        )
        raise RuntimeError(f"O servidor recusou o usuário ou a senha SMTP.{provider_hint}") from error
    return len(recipients)


async def _send_whatsapp(body: str) -> int:
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    recipients = [value.strip() for value in os.getenv("WHATSAPP_TO", "").split(",") if value.strip()]
    if not recipients:
        return 0
    if not token or not phone_id:
        raise RuntimeError("WhatsApp Cloud API incompleta.")
    async with httpx.AsyncClient(timeout=20) as client:
        for recipient in recipients:
            response = await client.post(
                f"https://graph.facebook.com/v21.0/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": recipient,
                    "type": "text",
                    "text": {"preview_url": False, "body": body},
                },
            )
            response.raise_for_status()
    return len(recipients)


async def send_notifications(payload: dict, message: str | None = None, subject: str | None = None) -> dict:
    settings = await read_notification_settings()
    context = _notification_context(payload, settings)
    body = (message or context["message"]).strip()
    title = (subject or context["subject"]).strip()
    email_count, whatsapp_count = await asyncio.gather(
        asyncio.to_thread(
            _send_emails,
            title,
            body,
            settings["emailRecipients"],
            settings["senderEmail"],
            settings["senderName"],
        ),
        _send_whatsapp(body),
    )
    LAST_NOTIFICATION.update({
        "status": "sent",
        "sentAt": datetime.now(timezone.utc).isoformat(),
        "emailCount": email_count,
        "whatsappCount": whatsapp_count,
        **context,
    })
    return dict(LAST_NOTIFICATION)


async def maybe_send_notifications(payload: dict) -> dict:
    try:
        settings = await read_notification_settings()
        if not settings["autoEnabled"]:
            return {"status": "disabled"}
        return await send_notifications(payload)
    except Exception as error:
        _report_blob_error("notification", error)
        LAST_NOTIFICATION.update({
            "status": "error",
            "sentAt": datetime.now(timezone.utc).isoformat(),
            "error": str(error),
        })
        return dict(LAST_NOTIFICATION)


@app.post("/api/session")
async def login_session(credentials: LoginRequest, request: Request, response: Response) -> dict:
    attempt_key = _check_login_rate(request)
    role = _password_role(credentials.password)
    if role is None:
        LOGIN_ATTEMPTS.setdefault(attempt_key, []).append(time.time())
        raise HTTPException(status_code=401, detail="Senha incorreta.")
    LOGIN_ATTEMPTS.pop(attempt_key, None)
    response.delete_cookie(IDENTITY_COOKIE, path="/", samesite="strict")
    response.delete_cookie(FRANCHISE_CONTEXT_COOKIE, path="/", samesite="strict")
    response.set_cookie(
        SESSION_COOKIE,
        _create_session(role),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=os.getenv("NODE_ENV") == "production" or bool(os.getenv("VERCEL")),
        samesite="strict",
        path="/",
    )
    return {"authenticated": True, "role": role, "identified": role == "admin"}


@app.get("/api/session")
async def session_status(request: Request) -> dict:
    role = require_session(request)
    identity = _get_identity(request) if role == "franchise" else None
    return {
        "authenticated": True,
        "role": role,
        "identified": role == "admin" or identity is not None,
        "identity": {"name": identity["name"], "email": identity["email"]} if identity else None,
    }


@app.delete("/api/session")
async def logout_session(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="strict")
    response.delete_cookie(POTENTIAL_COOKIE, path="/", samesite="strict")
    response.delete_cookie(IDENTITY_COOKIE, path="/", samesite="strict")
    response.delete_cookie(FRANCHISE_CONTEXT_COOKIE, path="/", samesite="strict")
    return {"authenticated": False}


@app.post("/api/access/identify")
async def identify_franchise(action: FranchiseIdentityRequest, request: Request, response: Response) -> dict:
    if require_session(request) != "franchise":
        raise HTTPException(status_code=403, detail="Identificação disponível somente para franqueados.")
    name = " ".join(action.name.strip().split())
    email = action.email.strip().lower()
    if len(name) < 2 or not EMAIL_PATTERN.match(email):
        raise HTTPException(status_code=400, detail="Informe nome e e-mail válidos.")
    settings = await read_access_settings()
    email_domain = email.rsplit("@", 1)[-1]
    if email_domain not in settings["allowedDomains"]:
        raise HTTPException(
            status_code=403,
            detail=f"E-mail @{email_domain} não autorizado. Solicite a liberação ao administrador.",
        )
    identity = {"name": name, "email": email}
    audit_recorded = await _record_access_event(_access_event(request, identity, "login"))
    response.set_cookie(
        IDENTITY_COOKIE,
        _create_identity(name, email),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=os.getenv("NODE_ENV") == "production" or bool(os.getenv("VERCEL")),
        samesite="strict",
        path="/",
    )
    return {"identified": True, "identity": identity, "auditRecorded": audit_recorded}


@app.post("/api/access/context")
async def register_franchise_context(action: FranchiseContextRequest, request: Request, response: Response) -> dict:
    if require_session(request) != "franchise":
        raise HTTPException(status_code=403, detail="Registro disponível somente para franqueados.")
    identity = _get_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="Identifique-se antes de selecionar a unidade.")
    brand_id = action.brandId.strip()
    store = " ".join(action.store.strip().split())
    audit_recorded = await _record_access_event(_access_event(
        request,
        identity,
        "unit_selected",
        brandId=brand_id,
        store=store,
    ))
    response.set_cookie(
        FRANCHISE_CONTEXT_COOKIE,
        _create_franchise_context(brand_id, store),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=os.getenv("NODE_ENV") == "production" or bool(os.getenv("VERCEL")),
        samesite="strict",
        path="/",
    )
    return {"recorded": audit_recorded}


@app.get("/api/access-logs")
async def access_logs(request: Request, limit: int = 250) -> dict:
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem consultar os acessos.")
    safe_limit = max(1, min(limit, 500))
    events = await _read_access_events(safe_limit)
    return {"events": events, "total": len(events)}


@app.get("/api/access-settings")
async def access_settings(request: Request) -> dict:
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem consultar esta configuração.")
    return await read_access_settings()


@app.put("/api/access-settings")
async def update_access_settings(action: AccessSettingsRequest, request: Request) -> dict:
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem alterar esta configuração.")
    settings = {"allowedDomains": _normalize_domains(action.allowedDomains)}
    await write_access_settings(settings)
    return settings


@app.delete("/api/access-logs")
async def clear_access_logs(request: Request) -> dict:
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem apagar os acessos.")
    return {"deleted": await delete_access_events()}


@app.get("/api/price-overrides")
async def get_price_overrides(request: Request) -> dict:
    # Admin e franqueado podem ler — o admin precisa ver o ajuste feito pela
    # franquia, e a franquia precisa ver o que já cadastrou.
    role = require_session(request)
    overrides = await read_price_overrides()
    if role == "franchise":
        context = _get_franchise_context(request)
        if context is None:
            return {"overrides": {}}
        target = _store_key(context["store"])
        overrides = {
            key: value for key, value in overrides.items()
            if _store_key(value.get("store")) == target
        }
    return {"overrides": overrides}


@app.post("/api/price-overrides")
async def set_price_override(action: PriceOverrideRequest, request: Request) -> dict:
    role = require_session(request)
    store = " ".join(action.store.strip().split())
    item = " ".join(action.item.strip().split())
    if not store or not item:
        raise HTTPException(status_code=400, detail="Informe a unidade e o item.")
    if role == "franchise":
        context = _get_franchise_context(request)
        if context is None or _store_key(context["store"]) != _store_key(store):
            raise HTTPException(status_code=403, detail="Você só pode ajustar preços da unidade selecionada.")
    identity = _get_identity(request) if role == "franchise" else None
    key = price_override_key(store, item)
    overrides = await read_price_overrides()
    overrides[key] = {
        "store": store,
        "item": item,
        "categoria": action.categoria.strip(),
        "price": round(action.price, 2),
        "setByRole": role,
        "setByName": identity["name"] if identity else None,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    await write_price_overrides(overrides)
    return {"success": True, "key": key, "override": overrides[key]}


@app.get("/api/feedback/settings")
async def feedback_settings_status(request: Request) -> dict:
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem consultar essa configuração.")
    settings = await read_feedback_settings()
    env_url = os.getenv("FEEDBACK_SHEET_CSV_URL", "").strip()
    return {
        "sheetCsvUrl": settings.get("sheetCsvUrl", ""),
        "envConfigured": bool(env_url),
    }


@app.put("/api/feedback/settings")
async def update_feedback_settings(action: FeedbackSettingsRequest, request: Request) -> dict:
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem configurar essa opção.")
    url = action.sheetCsvUrl.strip()
    if url and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Use um link https:// válido.")
    if url and "output=csv" not in url and "/pub" not in url:
        raise HTTPException(
            status_code=400,
            detail="Use o link de exportação CSV da planilha (com output=csv ou /pub), não o link de edição do Google Sheets.",
        )
    await write_feedback_settings({"sheetCsvUrl": url})
    return {"success": True, "sheetCsvUrl": url}


@app.get("/api/feedback", response_class=PlainTextResponse)
async def feedback_responses(request: Request) -> PlainTextResponse:
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem consultar feedbacks.")
    sheet_url = os.getenv("FEEDBACK_SHEET_CSV_URL", "").strip()
    if not sheet_url:
        sheet_url = (await read_feedback_settings()).get("sheetCsvUrl", "").strip()
    if not sheet_url:
        raise HTTPException(status_code=503, detail="Planilha de feedback ainda não configurada.")
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(sheet_url)
            response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail="Não foi possível consultar o Google Sheets.") from error
    content_type = response.headers.get("content-type", "").lower()
    content = response.text.lstrip()
    if "text/html" in content_type or content.lower().startswith(("<!doctype html", "<html")):
        raise HTTPException(
            status_code=422,
            detail="A URL configurada retornou HTML. Use a URL CSV publicada da planilha de respostas, não o link do formulário ou do dashboard.",
        )
    return PlainTextResponse(response.text, media_type="text/csv; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.get("/api/potential/session")
async def potential_session_status(request: Request) -> dict:
    role = require_session(request)
    return {"authorized": role == "admin" or _has_potential_access(request)}


@app.post("/api/potential/session")
async def unlock_potential(action: PotentialAccessRequest, request: Request, response: Response) -> dict:
    role = require_session(request)
    if role == "admin":
        return {"authorized": True}
    configured_hash = os.getenv("FRANCHISE_POTENTIAL_PASSWORD_HASH", "").strip()
    if not configured_hash:
        raise HTTPException(status_code=503, detail="Senha adicional do Potencial não configurada.")
    supplied_hash = hashlib.sha256(action.password.strip().encode("utf-8")).hexdigest()
    if not hmac.compare_digest(supplied_hash, configured_hash):
        raise HTTPException(status_code=401, detail="Senha do Potencial incorreta.")
    response.set_cookie(
        POTENTIAL_COOKIE,
        _create_potential_access(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=os.getenv("NODE_ENV") == "production" or bool(os.getenv("VERCEL")),
        samesite="strict",
        path="/",
    )
    return {"authorized": True}


@app.get("/api/data")
async def load_saved_data(request: Request) -> dict:
    role = require_session(request)
    payload = await read_current_payload()
    if payload is None:
        return {"hasData": False, "rows": []}
    response = {"hasData": True, **payload}
    if role != "franchise":
        return response
    context = _get_franchise_context(request)
    if context is None:
        return redact_paused_revenue(response)
    return filter_payload_for_store(response, context["store"])


@app.post("/api/data/upload")
async def upload_compressed_data(request: Request) -> dict:
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem atualizar a base.")
    compressed = await request.body()
    if not compressed or len(compressed) > 4_000_000:
        raise HTTPException(status_code=413, detail="Base comprimida excede o limite seguro de 4 MB.")
    try:
        payload = json.loads(gzip.decompress(compressed).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="Arquivo de dados comprimido inválido.") from error
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != payload.get("totalRows"):
        raise HTTPException(status_code=400, detail="Quantidade de registros inconsistente.")
    try:
        await write_current_payload(payload)
    except CloudStorageError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    notification = await maybe_send_notifications(payload)
    return {"success": True, "totalRows": len(rows), "notification": notification}


def _find_matching_run(runs: list[dict], triggered_at: datetime, ref: str) -> dict | None:
    """Entre os runs recentes do workflow, acha o que corresponde ao nosso
    disparo — melhor esforço, já que workflow_dispatch responde 204 sem
    devolver run_id (não existe, nessa arquitetura, um identificador forte
    pra correlacionar; o workflow atual não tem inputs customizados que
    poderiam carregar um token único). Critério: event=workflow_dispatch,
    mesma branch/ref configurada, e created_at >= triggered_at — dentre os
    que sobram, o mais antigo (o run criado logo depois do nosso POST).
    Risco conhecido e documentado: dois disparos manuais (nosso botão e/ou
    a aba Actions do GitHub) muito próximos no tempo podem, em tese, ser
    confundidos — não há como diferenciar sem um input customizado no
    workflow."""
    candidates: list[tuple[datetime, dict]] = []
    for run in runs:
        if run.get("event") != "workflow_dispatch":
            continue
        if run.get("head_branch") != ref:
            continue
        created_raw = run.get("created_at")
        if not created_raw:
            continue
        try:
            created_at = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if created_at < triggered_at:
            continue
        candidates.append((created_at, run))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


def _map_run_status_to_outcome(
    run: dict | None,
    uploaded_at_at_trigger: str | None,
    current_uploaded_at: str | None,
) -> str:
    """Traduz o status do run do GitHub Actions pro vocabulário do frontend.
    Nunca inventa "updated"/"no_new_data" sem um run 'completed' de verdade —
    enquanto o run não é encontrado (ainda não apareceu na listagem) ou está
    queued/waiting/pending, é 'requested'; em execução é 'running'; só depois
    de 'completed' é que olhamos a conclusão e o uploadedAt pra decidir entre
    'failed', 'updated' e 'no_new_data'."""
    if run is None:
        return "requested"
    status = str(run.get("status") or "").lower()
    if status in ("queued", "waiting", "pending", "requested"):
        return "requested"
    if status == "in_progress":
        return "running"
    if status == "completed":
        conclusion = str(run.get("conclusion") or "").lower()
        if conclusion != "success":
            return "failed"
        if current_uploaded_at and current_uploaded_at != uploaded_at_at_trigger:
            return "updated"
        return "no_new_data"
    # Status que o GitHub venha a introduzir e que não conhecemos ainda —
    # tratamos como "ainda em andamento" em vez de arriscar uma conclusão
    # errada.
    return "requested"


async def _fetch_recent_workflow_runs(http_client: httpx.AsyncClient) -> list[dict]:
    response = await http_client.get(
        f"https://api.github.com/repos/{GITHUB_SYNC_REPO}/actions/workflows/{GITHUB_SYNC_WORKFLOW}/runs",
        headers={
            "Authorization": f"Bearer {GITHUB_SYNC_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        params={"event": "workflow_dispatch", "branch": GITHUB_SYNC_REF, "per_page": 10},
    )
    response.raise_for_status()
    return response.json().get("workflow_runs", [])


@app.post("/api/data/sync-now")
async def trigger_sync_now(request: Request) -> dict:
    """Dispara manualmente o workflow_dispatch do sync-postgres.yml (mesmo
    workflow do cron automático de 15:20/20:20 BRT). Só admin, com cooldown
    para não deixar clique duplo/repetido disparar várias execuções ao mesmo
    tempo. O GitHub só confirma que ACEITOU o disparo (204) — não que o sync
    já terminou; guardamos triggeredAt + o uploadedAt de agora (antes do
    disparo) pra GET /api/data/sync-status conseguir acompanhar o run real
    depois (ver _find_matching_run / _map_run_status_to_outcome)."""
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem disparar a sincronização.")
    if not GITHUB_SYNC_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="GITHUB_SYNC_TOKEN não configurado no backend — configure a variável de ambiente para habilitar este botão.",
        )
    now = time.time()
    if SYNC_TRIGGER_STATE["inFlight"]:
        raise HTTPException(status_code=429, detail="Já existe uma solicitação de sincronização em andamento.")
    elapsed = now - SYNC_TRIGGER_STATE["lastTriggeredAt"]
    if elapsed < SYNC_TRIGGER_COOLDOWN_SECONDS:
        wait = int(SYNC_TRIGGER_COOLDOWN_SECONDS - elapsed) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Aguarde {wait}s antes de disparar a sincronização de novo.",
            headers={"Retry-After": str(wait)},
        )

    # Carimbo ANTES do POST de dispatch — representa o estado "como estava"
    # no momento do clique, pra depois sabermos se um run 'completed com
    # sucesso' de fato trouxe dado novo ou não.
    triggered_at_dt = datetime.now(timezone.utc)
    uploaded_at_before = (await read_current_payload() or {}).get("uploadedAt")

    SYNC_TRIGGER_STATE["inFlight"] = True
    try:
        async with httpx.AsyncClient(timeout=20) as http_client:
            response = await http_client.post(
                f"https://api.github.com/repos/{GITHUB_SYNC_REPO}/actions/workflows/{GITHUB_SYNC_WORKFLOW}/dispatches",
                headers={
                    "Authorization": f"Bearer {GITHUB_SYNC_TOKEN}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"ref": GITHUB_SYNC_REF},
            )
        if response.status_code not in (201, 204):
            raise HTTPException(
                status_code=502,
                detail=f"O GitHub recusou o disparo do workflow (status {response.status_code}).",
            )
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail="Não foi possível contatar o GitHub Actions.") from error
    finally:
        SYNC_TRIGGER_STATE["inFlight"] = False

    # O 204 do dispatch só significa "o GitHub aceitou e colocou na fila" —
    # por isso guardamos como 'requested', nunca como concluído.
    SYNC_TRIGGER_STATE["lastTriggeredAt"] = now
    SYNC_TRIGGER_STATE["triggeredAtIso"] = triggered_at_dt.isoformat()
    SYNC_TRIGGER_STATE["uploadedAtAtTrigger"] = uploaded_at_before
    return {
        "success": True,
        "triggeredAt": SYNC_TRIGGER_STATE["triggeredAtIso"],
        "cooldownSeconds": SYNC_TRIGGER_COOLDOWN_SECONDS,
    }


@app.get("/api/data/sync-status")
async def sync_trigger_status(request: Request) -> dict:
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem consultar o status.")
    elapsed = time.time() - SYNC_TRIGGER_STATE["lastTriggeredAt"] if SYNC_TRIGGER_STATE["lastTriggeredAt"] else None
    cooldown_remaining = (
        max(0, int(SYNC_TRIGGER_COOLDOWN_SECONDS - elapsed)) if elapsed is not None else 0
    )
    base = {
        "configured": bool(GITHUB_SYNC_TOKEN),
        "inFlight": SYNC_TRIGGER_STATE["inFlight"],
        "cooldownRemaining": cooldown_remaining,
        "lastTriggeredAt": (
            datetime.fromtimestamp(SYNC_TRIGGER_STATE["lastTriggeredAt"], tz=timezone.utc).isoformat()
            if SYNC_TRIGGER_STATE["lastTriggeredAt"] else None
        ),
    }

    triggered_at_iso = SYNC_TRIGGER_STATE.get("triggeredAtIso")
    if not triggered_at_iso:
        # Nunca disparado (ou processo reiniciado) — não há nada pra
        # acompanhar; outcome None é o estado "idle" pro frontend.
        return {**base, "outcome": None, "runId": None, "runUrl": None}

    try:
        triggered_at = datetime.fromisoformat(triggered_at_iso)
    except ValueError:
        return {**base, "outcome": None, "runId": None, "runUrl": None}

    if not GITHUB_SYNC_TOKEN:
        return {**base, "outcome": "requested", "runId": None, "runUrl": None}

    try:
        async with httpx.AsyncClient(timeout=20) as http_client:
            runs = await _fetch_recent_workflow_runs(http_client)
    except httpx.HTTPError:
        # Não conseguimos consultar o GitHub agora — não inventamos
        # conclusão nenhuma (nem sucesso, nem no_new_data): devolvemos um
        # estado indeterminado explícito, pro frontend não travar
        # silenciosamente nem assumir algo que não confirmamos.
        return {**base, "outcome": "unknown", "runId": None, "runUrl": None}

    matching_run = _find_matching_run(runs, triggered_at, GITHUB_SYNC_REF)
    current_uploaded_at = (await read_current_payload() or {}).get("uploadedAt")
    outcome = _map_run_status_to_outcome(
        matching_run, SYNC_TRIGGER_STATE.get("uploadedAtAtTrigger"), current_uploaded_at,
    )
    return {
        **base,
        "outcome": outcome,
        "runId": matching_run.get("id") if matching_run else None,
        "runUrl": matching_run.get("html_url") if matching_run else None,
    }


@app.post("/api/data")
async def save_data(action: DataAction, request: Request) -> dict:
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem atualizar a base.")
    DATA_DIR.mkdir(exist_ok=True)
    STAGING_DIR.mkdir(exist_ok=True)

    if action.action == "start":
        upload_id = secrets.token_hex(12)
        (STAGING_DIR / upload_id).mkdir()
        return {"success": True, "uploadId": upload_id}

    if not action.uploadId or not re.fullmatch(r"[0-9a-f]{24}", action.uploadId):
        raise HTTPException(status_code=400, detail="Upload inválido.")
    upload_dir = STAGING_DIR / action.uploadId
    if not upload_dir.is_dir():
        raise HTTPException(status_code=404, detail="Upload não encontrado.")

    if action.action == "chunk":
        if action.chunkIndex is None or action.chunkIndex < 0 or action.rows is None:
            raise HTTPException(status_code=400, detail="Chunk inválido.")
        target = upload_dir / f"{action.chunkIndex:05d}.json.gz"
        with gzip.open(target, "wt", encoding="utf-8") as output:
            json.dump(action.rows, output, ensure_ascii=False, separators=(",", ":"))
        return {"success": True}

    if action.action == "complete":
        rows: list[dict] = []
        for chunk in sorted(upload_dir.glob("*.json.gz")):
            with gzip.open(chunk, "rt", encoding="utf-8") as source:
                rows.extend(json.load(source))
        if action.totalRows is not None and len(rows) != action.totalRows:
            raise HTTPException(status_code=400, detail="Quantidade de registros inconsistente.")
        payload = {"rows": rows, "totalRows": len(rows), "uploadedAt": action.uploadedAt}
        try:
            await write_current_payload(payload)
        except CloudStorageError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        notification = await maybe_send_notifications(payload)
        shutil.rmtree(upload_dir)
        return {"success": True, "notification": notification}

    raise HTTPException(status_code=400, detail="Ação inválida.")


@app.delete("/api/data")
async def clear_saved_data(request: Request) -> dict:
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem limpar a base.")
    if BLOB_TOKEN:
        from vercel.blob import AsyncBlobClient

        async with AsyncBlobClient(token=BLOB_TOKEN) as blob_client:
            await blob_client.delete(BLOB_PATH)
    if CURRENT_DATA.exists():
        CURRENT_DATA.unlink()
    return {"success": True}


@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "runtime": "python",
        "blobConfigured": bool(BLOB_TOKEN),
        "blobTokenSource": BLOB_TOKEN_SOURCE,
        "storageMode": "vercel-blob" if BLOB_TOKEN else ("unavailable" if _is_vercel_runtime() else "local-disk"),
    }


@app.get("/api/notifications/status")
async def notification_status(request: Request) -> dict:
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem consultar avisos.")
    payload = await read_current_payload() or {"rows": []}
    settings = await read_notification_settings()
    return {
        **LAST_NOTIFICATION,
        "autoEnabled": settings["autoEnabled"],
        "emailConfigured": bool(settings["emailRecipients"]),
        "emailRecipients": settings["emailRecipients"],
        "senderEmail": settings["senderEmail"],
        "senderName": settings["senderName"],
        "messageTemplate": settings["messageTemplate"],
        "smtpConfigured": all(os.getenv(key, "").strip() for key in ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"]),
        "whatsappConfigured": bool(os.getenv("WHATSAPP_TO", "").strip()),
        "preview": _notification_context(payload, settings),
    }


@app.put("/api/notifications/settings")
async def notification_settings(action: NotificationSettingsRequest, request: Request) -> dict:
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem configurar avisos.")
    recipients = list(dict.fromkeys(
        value.strip().lower()
        for value in action.emailRecipients
        if value.strip()
    ))
    if len(recipients) > 1000:
        raise HTTPException(status_code=400, detail="Limite de 1.000 destinatários por configuração.")
    invalid = [value for value in recipients if not EMAIL_PATTERN.fullmatch(value)]
    if invalid:
        raise HTTPException(status_code=400, detail=f"E-mail inválido: {invalid[0]}")
    sender_email = action.senderEmail.strip().lower()
    if sender_email and not EMAIL_PATTERN.fullmatch(sender_email):
        raise HTTPException(status_code=400, detail="E-mail remetente inválido.")
    sender_name = action.senderName.strip()[:80] or "Ital in House"
    message_template = action.messageTemplate.strip()
    if len(message_template) > 4000:
        raise HTTPException(status_code=400, detail="Modelo de mensagem muito longo (máx. 4.000 caracteres).")
    settings = {
        "autoEnabled": action.autoEnabled,
        "emailRecipients": recipients,
        "senderEmail": sender_email,
        "senderName": sender_name,
        "messageTemplate": message_template,
    }
    await write_notification_settings(settings)
    return {
        "success": True,
        **settings,
        "emailConfigured": bool(recipients),
    }


@app.post("/api/notifications/test")
async def notification_test(action: NotificationTestRequest, request: Request) -> dict:
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem testar avisos.")
    recipient = action.recipient.strip().lower()
    if not EMAIL_PATTERN.fullmatch(recipient):
        raise HTTPException(status_code=400, detail="Informe um destinatário de teste válido.")
    settings = await read_notification_settings()
    try:
        count = await asyncio.to_thread(
            _send_emails,
            "Teste de envio – Dashboard Ital in House",
            "Este é um e-mail de teste do Dashboard de Itens Pausados.\n\n"
            "Se você recebeu esta mensagem, a configuração de envio está funcionando.",
            [recipient],
            settings["senderEmail"],
            settings["senderName"],
        )
        return {"success": True, "emailCount": count, "recipient": recipient}
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except smtplib.SMTPAuthenticationError as error:
        raise HTTPException(status_code=502, detail="O servidor recusou o usuário ou a senha SMTP.") from error
    except (OSError, smtplib.SMTPException) as error:
        raise HTTPException(
            status_code=502,
            detail="Não foi possível conectar ao servidor SMTP. Confira host, porta e tipo de segurança.",
        ) from error


@app.post("/api/notifications/send")
async def notification_send(action: NotificationRequest, request: Request) -> dict:
    if require_session(request) != "admin":
        raise HTTPException(status_code=403, detail="Apenas administradores podem enviar avisos.")
    payload = await read_current_payload()
    if payload is None:
        raise HTTPException(status_code=404, detail="Nenhuma base publicada.")
    try:
        return await send_notifications(payload, action.message, action.subject)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (OSError, smtplib.SMTPException, httpx.HTTPError) as error:
        raise HTTPException(status_code=502, detail="Falha no provedor de envio.") from error


@app.get("/api/ifood/auth")
@app.post("/api/ifood/auth")
async def ifood_auth() -> dict:
    try:
        token, source = await client().get_access_token()
        merchants = await client().list_merchants()
        selected = os.getenv("IFOOD_TEST_MERCHANT_ID", "").strip()
        return {
            "success": True,
            "connected": True,
            "expiresIn": token.expires_in,
            "expiresAt": datetime.fromtimestamp(token.expires_at, tz=timezone.utc).isoformat(),
            "tokenSource": source,
            "authMode": client().auth_mode,
            "merchants": merchants,
            "selectedMerchantId": selected or (merchants[0]["id"] if merchants else None),
        }
    except IFoodAPIError as error:
        raise api_error(error) from error


@app.post("/api/ifood/sync")
async def ifood_sync(request: SyncRequest) -> dict:
    merchant_id = request.merchantId.strip()
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        merchant_id,
        re.IGNORECASE,
    ):
        raise HTTPException(status_code=400, detail="Selecione uma loja iFood válida.")

    try:
        merchants = await client().list_merchants()
        merchant = next((entry for entry in merchants if entry["id"] == merchant_id), None)
        if merchant is None:
            raise HTTPException(status_code=403, detail="A credencial atual não possui acesso a essa loja.")

        categories = await client().list_categories(merchant_id)
        items = normalize_categories(categories, merchant["name"])
        paused = sum(item["status"] == "Pausado" for item in items)
        return {
            "success": True,
            "merchant": merchant,
            "totalItems": len(items),
            "activeItems": len(items) - paused,
            "pausedItems": paused,
            "categories": len(categories),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "csv": items_to_csv(items),
        }
    except IFoodAPIError as error:
        raise api_error(error) from error


@app.get("/{path:path}", include_in_schema=False)
async def frontend(path: str):
    requested = (DIST / path).resolve()
    if path and requested.is_relative_to(DIST.resolve()) and requested.is_file():
        return FileResponse(requested)
    index = DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    raise HTTPException(status_code=503, detail="Frontend não compilado. Execute npm run build.")
