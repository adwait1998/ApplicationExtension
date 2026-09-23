"""Local FastAPI service for the ApplyPilot Copilot Chrome extension.

Safety contract (deliberate, mirrors webui/server.py's header comment):
- Binds 127.0.0.1 only — create_app() refuses any other host outright.
- Every request must carry the token printed on startup (X-ApplyPilot-Token).
  Generated once, persisted under config.APP_DIR, never logged after
  startup.
- CORS is restricted to chrome-extension:// origins.
- POST /resolve returns values only for the fields the caller actually
  submitted — never the whole profile. GET /profile (if used) returns key
  names only, never values, and never the secret-denylisted keys.
- This service never submits, navigates, or writes to the pipeline's DB.
  It answers "what goes in this field" and nothing else.
"""
from __future__ import annotations

import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from applypilot.extension import resolve, schema

TOKEN_FILENAME = "extension_token.txt"


# ---------------------------------------------------------------------------
# Token: generated on first run, persisted, required on every request
# ---------------------------------------------------------------------------


def token_path(app_dir: Path) -> Path:
    return Path(app_dir) / TOKEN_FILENAME


def get_or_create_token(app_dir: Path) -> str:
    path = token_path(app_dir)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    return token


# ---------------------------------------------------------------------------
# Request models (pydantic, parsed into the plain schema.FieldDescriptor
# dataclasses that the resolution ladder actually operates on)
# ---------------------------------------------------------------------------


class FieldIn(BaseModel):
    id: str
    selector: str = ""
    tag: str = ""
    type: str = ""
    name: str = ""
    autocomplete: str = ""
    label: str = ""
    placeholder: str = ""
    required: bool = False
    options: list[str] = Field(default_factory=list)
    # Repeating-section context for tier 3 (structured). Optional so a
    # scanner build that hasn't shipped this yet keeps working unchanged.
    section: str = ""
    section_index: int | None = None


class ResolveRequest(BaseModel):
    url: str = ""
    fields: list[FieldIn] = Field(default_factory=list)


def _dotted_keys(d: dict, prefix: str = "") -> list[str]:
    keys: list[str] = []
    for k, v in (d or {}).items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            keys.extend(_dotted_keys(v, path))
        else:
            keys.append(path)
    return keys


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    app_dir: Path | None = None,
    profile: dict | None = None,
    host: str = "127.0.0.1",
) -> FastAPI:
    """Build the extension service app.

    ``app_dir`` is where the token is persisted (config.APP_DIR by
    default). ``profile`` lets tests (and, if ever needed, callers) inject
    a profile dict directly instead of reading profile.json from disk; when
    omitted the profile is loaded fresh via config.load_profile() on every
    /resolve call, so edits to profile.json take effect without a restart.
    """
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError(f"refusing to bind the extension service to non-local host: {host!r}")

    if app_dir is None:
        from applypilot import config as _config

        app_dir = _config.APP_DIR
    app_dir = Path(app_dir)

    token = get_or_create_token(app_dir)

    if profile is not None:
        def _load_profile() -> dict:
            return profile
    else:
        def _load_profile() -> dict:
            from applypilot import config as _config

            return _config.load_profile()

    def _warm_laya() -> None:
        """Build the Laya checkpoint in the background at startup.

        Loading it costs ~28s. Deferring that to the first ambiguous field
        would dump the whole cliff onto whichever form the operator happens to
        open first, which reads as "the extension hung". Warming here means it
        is usually ready before the first click, and the ladder degrades to the
        deterministic tier in the meantime rather than waiting on it.

        Daemon thread, fully swallowed: a warmup failure must never stop the
        service from serving the tiers that do work.
        """
        backend = resolve.get_backend()
        if backend is None or not hasattr(backend, "warmup"):
            return

        def _run() -> None:
            try:
                backend.warmup()
            except Exception:       # noqa: BLE001 — optional tier, never fatal
                pass

        threading.Thread(target=_run, name="laya-warmup", daemon=True).start()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        _warm_laya()
        yield

    app = FastAPI(title="ApplyPilot Copilot", docs_url=None, redoc_url=None,
                  lifespan=_lifespan)
    app.state.token = token

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^chrome-extension://.*$",
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    def _require_token(x_applypilot_token: str | None = Header(default=None)) -> None:
        if not x_applypilot_token or not secrets.compare_digest(x_applypilot_token, app.state.token):
            raise HTTPException(status_code=401, detail="missing or invalid token")

    @app.get("/health")
    def health(_: None = Depends(_require_token)) -> dict:
        return {"status": "ok", "tiers_available": resolve.tiers_available()}

    @app.post("/resolve")
    def resolve_endpoint(body: ResolveRequest, _: None = Depends(_require_token)) -> dict:
        fields = [
            schema.FieldDescriptor(
                id=f.id,
                selector=f.selector,
                tag=f.tag,
                type=f.type,
                name=f.name,
                autocomplete=f.autocomplete,
                label=f.label,
                placeholder=f.placeholder,
                required=f.required,
                options=list(f.options),
                section=f.section,
                section_index=f.section_index,
            )
            for f in body.fields
        ]
        plan = resolve.resolve_fields(fields, _load_profile())
        return plan.to_dict()

    @app.get("/profile")
    def profile_keys(_: None = Depends(_require_token)) -> dict:
        prof = _load_profile()
        keys = sorted(k for k in _dotted_keys(prof) if not resolve.is_secret_path(k))
        return {"keys": keys}

    return app
