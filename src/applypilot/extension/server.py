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
- GET /profile/full is a considered relaxation of that last point — it
  returns real profile values, for the extension's own profile editor.
  Still 127.0.0.1-only and token-gated, and the secret denylist still
  applies: personal.password never leaves this service via any route.
  POST /profile merges rather than overwrites on the secret paths, so a
  client that only ever saw the stripped /profile/full response can never
  wipe a secret it was never shown.
- This service never submits, navigates, or writes to the pipeline's DB.
  It answers "what goes in this field" and nothing else.
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import shutil
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from applypilot import profiles as profiles_mod
from applypilot.extension import (answer_memory, app_log, cover_letter, job_context, llm_util, resolve,
                                  resume_import, schema)
from applypilot.extension import settings as ext_settings

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
    # Workday widget driver the content script bound this field to
    # ("wd-dropdown" | "wd-prompt" | "wd-date-my" | "wd-date-y"), or "" for a
    # plain input/select. Optional so a scanner build that hasn't shipped
    # this yet keeps working unchanged -- see schema.FieldDescriptor.widget.
    widget: str = ""


class ResolveRequest(BaseModel):
    url: str = ""
    fields: list[FieldIn] = Field(default_factory=list)


class ProfileCreateIn(BaseModel):
    id: str


class LearnItem(BaseModel):
    question: str = ""
    answer: str = ""


class LearnIn(BaseModel):
    """POST /answers/learn body: answers the applicant typed themselves into
    questions a fill left for them, sent on an explicit click."""
    items: list[LearnItem] = []


class ForgetIn(BaseModel):
    question: str = ""


class LogIn(BaseModel):
    """POST /log body: one fill of one page — counts only, never values."""
    url: str = ""
    title: str = ""
    company: str = ""
    counts: dict[str, int] = {}


class LogStatusIn(BaseModel):
    status: str = ""


class CoverLetterIn(BaseModel):
    """POST /cover-letter body: the page's URL plus any frame URLs (an
    embedded Greenhouse form's iframe names the posting), and optionally the
    page's visible text as a last-resort job description."""
    urls: list[str] = []
    page_text: str = ""


class SettingsIn(BaseModel):
    """POST /settings body -- every field optional, a partial update over
    whatever is already persisted. Unset fields are left untouched."""
    answers_enabled: bool | None = None
    drafts_enabled: bool | None = None
    max_drafts: int | None = None


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
# Profile management helpers (GET /profile/full, POST /profile, /profiles*)
#
# These are pure/module-level: they take a root or a profile dict as an
# argument rather than closing over app state, so they are trivially unit-
# testable and reusable across the several profile endpoints below.
# ---------------------------------------------------------------------------

_MISSING = object()  # sentinel: "this dotted path is absent", distinct from None/""


def _dotted_get(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _dotted_set(d: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _strip_secret_values(profile: dict, prefix: str = "") -> dict:
    """Deep-copy `profile` omitting any key whose dotted path is on the
    secret denylist (resolve.SECRET_PROFILE_PATHS) — e.g. personal.password.
    Unlike GET /profile (key names only), /profile/full returns real values,
    so this is the one thing standing between that relaxation and a leaked
    credential. The key is omitted entirely, not blanked, so a client can
    never mistake an empty string for "no secret set"."""
    out: dict = {}
    for k, v in (profile or {}).items():
        path = f"{prefix}.{k}" if prefix else k
        if resolve.is_secret_path(path):
            continue
        if isinstance(v, dict):
            out[k] = _strip_secret_values(v, path)
        else:
            out[k] = v
    return out


def _merge_preserving_secrets(new_profile: dict, existing_profile: dict) -> dict:
    """POST /profile's client only ever saw a secret-stripped profile (from
    GET /profile/full), so an absent secret path in its payload means "I
    never had this", not "delete it". For every secret path missing from
    new_profile, carry the value forward from what is already on disk. A
    path the client DID send (even "") is left alone — this only stops the
    silent-wipe-on-round-trip bug, it doesn't block an intentional change."""
    merged = copy.deepcopy(new_profile) if isinstance(new_profile, dict) else {}
    for path in resolve.SECRET_PROFILE_PATHS:
        if _dotted_get(merged, path) is _MISSING:
            existing_val = _dotted_get(existing_profile or {}, path)
            if existing_val is not _MISSING:
                _dotted_set(merged, path, existing_val)
    return merged


def _current_profile_path(root: Path) -> Path:
    """Where the currently-active profile's profile.json lives, handling
    both the legacy flat layout and profiles/<id>/. Raises 409 only in the
    genuinely ambiguous multi-profile/no-active case — which the running
    service normally never reaches (bind_profile refuses to start it), but
    can reach once profiles are created/switched live via this API."""
    root = Path(root)
    if profiles_mod.is_legacy_layout(root):
        return root / "profile.json"
    pid = profiles_mod.get_active(root)
    if not pid:
        available = profiles_mod.list_profiles(root)
        if len(available) == 1:
            pid = available[0]
    if not pid:
        raise HTTPException(status_code=409, detail="no active profile selected — activate one first")
    return profiles_mod.profile_dir(root, pid) / "profile.json"


def _read_profile_or_empty(path: Path) -> dict:
    """Fail-safe read for the profile-management endpoints: a missing or
    corrupt profile.json reads as {} rather than raising, so the operator's
    very first profile can be created through the extension with nothing on
    disk yet. (GET /profile and POST /resolve keep their stricter, existing
    behaviour via _load_profile below.)"""
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:      # noqa: BLE001 — fail-safe, mirrors webui/settings.py
        return {}


def _validate_profile_body(body) -> None:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="profile must be a JSON object")
    for key in ("work_history", "education"):
        val = body.get(key)
        if val is None:
            continue
        if not isinstance(val, list) or not all(isinstance(x, dict) for x in val):
            raise HTTPException(status_code=422, detail=f"{key} must be a list of objects")
    for key in ("personal", "work_authorization", "compensation", "experience", "eeo_voluntary"):
        val = body.get(key)
        if val is not None and not isinstance(val, dict):
            raise HTTPException(status_code=422, detail=f"{key} must be an object")


def _backup_and_write_profile(path: Path, data: dict) -> None:
    """Atomic write (tempfile + os.replace), backing up any existing file
    first. Mirrors webui/settings.py's save_settings pattern plus the backup
    this task additionally requires."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _display_name(pdir: Path) -> str:
    """Best-effort label for the profile switcher; an unreadable profile.json
    must never raise — GET /profiles has to keep working even for a profile
    someone else broke by hand."""
    try:
        data = json.loads((Path(pdir) / "profile.json").read_text(encoding="utf-8"))
        name = (data.get("personal") or {}).get("full_name")
        return name or data.get("profile_id") or ""
    except Exception:      # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    app_dir: Path | None = None,
    profile: dict | None = None,
    host: str = "127.0.0.1",
    root: Path | None = None,
) -> FastAPI:
    """Build the extension service app.

    ``app_dir`` is where the token is persisted (config.APP_DIR by
    default). ``profile`` lets tests (and, if ever needed, callers) inject
    a profile dict directly instead of reading profile.json from disk, and
    when set it is what /resolve, /profile and /profile/full serve — POST
    /profile and the /profiles* management endpoints still read/write real
    files under ``root`` regardless, so tests exercising those must not rely
    on ``profile`` injection.

    ``root`` is the data ROOT holding profiles/ and active_profile
    (profiles.data_root() by default — the same root config.ROOT resolves,
    independent of whichever single profile this process happens to be
    bound to). Resolving it fresh on every request, rather than once at
    startup like config.APP_DIR, is what lets POST /profiles/{id}/activate
    take effect immediately without restarting the service.
    """
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError(f"refusing to bind the extension service to non-local host: {host!r}")

    if app_dir is None:
        from applypilot import config as _config

        app_dir = _config.APP_DIR
    app_dir = Path(app_dir)

    if root is None:
        root = profiles_mod.data_root()
    root = Path(root)

    token = get_or_create_token(app_dir)

    if profile is not None:
        def _load_profile() -> dict:
            return profile
    else:
        def _load_profile() -> dict:
            path = _current_profile_path(root)
            if not path.exists():
                raise FileNotFoundError(
                    f"Profile not found at {path}. Run `applypilot init` first."
                )
            return json.loads(path.read_text(encoding="utf-8"))

    def _load_full_profile() -> dict:
        """Values (not just keys), for /profile/full — same source as
        _load_profile but fails safe to {} instead of raising, so a
        brand-new install with no profile.json yet can still open the
        extension's profile editor and create one."""
        if profile is not None:
            return profile
        return _read_profile_or_empty(_current_profile_path(root))

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
        llm_ok, llm_provider = llm_util.llm_available()
        return {
            "status": "ok",
            "tiers_available": resolve.tiers_available(app_dir=app_dir),
            "llm_available": llm_ok,
            "llm_provider": llm_provider,
        }

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
                widget=f.widget,
            )
            for f in body.fields
        ]
        # The answer bank is personal: read the ACTIVE profile's own file, never
        # another person's. Resolved per request so switching profiles takes
        # effect immediately.
        try:
            bank = _current_profile_path(root).parent / "answer_bank.json"
        except HTTPException:
            bank = None
        plan = resolve.resolve_fields(fields, _load_profile(), app_dir=app_dir,
                                      url=body.url, bank_path=bank)
        return plan.to_dict()

    # -----------------------------------------------------------------
    # Tier settings: answer bank / draft / max-drafts, settable from the
    # extension instead of environment variables. Env vars, when set, still
    # override -- see applypilot.extension.settings.effective_settings().
    # -----------------------------------------------------------------

    def _settings_response() -> dict:
        effective = ext_settings.effective_settings(app_dir)
        return {
            "answers_enabled": effective["answers_enabled"],
            "drafts_enabled": effective["drafts_enabled"],
            "max_drafts": effective["max_drafts"],
            # Which (if any) of the three are currently pinned by an env
            # var -- null means "not pinned, the persisted value above is
            # editable from here".
            "env_overrides": {
                "answers_enabled": ext_settings.env_override_answers(),
                "drafts_enabled": ext_settings.env_override_drafts(),
                "max_drafts": ext_settings.env_override_max_drafts(),
            },
        }

    @app.get("/settings")
    def get_settings(_: None = Depends(_require_token)) -> dict:
        return _settings_response()

    @app.post("/settings")
    def update_settings(body: SettingsIn, _: None = Depends(_require_token)) -> dict:
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            ext_settings.save_settings(app_dir, updates)
        except ext_settings.InvalidSettings as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _settings_response()

    # -----------------------------------------------------------------
    # Answer memory: the applicant's own answers to questions a fill left
    # for them, saved on an explicit click and reused by the answer-bank
    # tier next time. Always the ACTIVE profile's own bank file.
    # -----------------------------------------------------------------

    def _bank_path() -> Path:
        return _current_profile_path(root).parent / "answer_bank.json"

    @app.get("/answers")
    def list_saved_answers(_: None = Depends(_require_token)) -> dict:
        return {"answers": answer_memory.list_answers(_bank_path())}

    @app.post("/answers/learn")
    def learn_answers(body: LearnIn, _: None = Depends(_require_token)) -> dict:
        return answer_memory.learn(_bank_path(), [i.model_dump() for i in body.items])

    @app.post("/answers/forget")
    def forget_answer(body: ForgetIn, _: None = Depends(_require_token)) -> dict:
        return {"forgotten": answer_memory.forget(_bank_path(), body.question)}

    # -----------------------------------------------------------------
    # Cover letter: an explicit, user-clicked draft for the page being
    # filled. Job context from the operator's jobs DB, else the ATS's
    # public posting API, else the page text. Never auto-attached.
    # -----------------------------------------------------------------

    # -----------------------------------------------------------------
    # Application log: which pages the Copilot filled, when, and how it
    # went (counts only). Status is the applicant's to set.
    # -----------------------------------------------------------------

    def _log_path() -> Path:
        return _current_profile_path(root).parent / app_log.LOG_NAME

    @app.post("/log")
    def log_fill(body: LogIn, _: None = Depends(_require_token)) -> dict:
        if not body.url.startswith(("http://", "https://")):
            raise HTTPException(status_code=422, detail="url must be an http(s) page")
        title, company = body.title, body.company
        if not (title and company):
            parsed = job_context.parse_ats_url(body.url) or {}
            company = company or parsed.get("slug", "")
        return app_log.record(_log_path(), body.url, title=title, company=company, counts=body.counts)

    @app.get("/log")
    def list_log(_: None = Depends(_require_token)) -> dict:
        return {"entries": app_log.entries(_log_path())}

    @app.post("/log/{entry_id}/status")
    def log_status(entry_id: str, body: LogStatusIn, _: None = Depends(_require_token)) -> dict:
        try:
            found = app_log.set_status(_log_path(), entry_id, body.status)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not found:
            raise HTTPException(status_code=404, detail="no such log entry")
        return {"ok": True}

    @app.get("/log.csv")
    def log_csv(_: None = Depends(_require_token)) -> Response:
        return Response(content=app_log.to_csv(_log_path()), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="applications.csv"'})

    @app.post("/cover-letter")
    def cover_letter_endpoint(body: CoverLetterIn, _: None = Depends(_require_token)) -> dict:
        ok, provider = llm_util.llm_available()
        if not ok:
            raise HTTPException(status_code=503, detail="no language model available for drafting")
        profile = _load_profile()
        job = job_context.job_context([u for u in body.urls if u][:6], page_text=body.page_text[:20000],
                                      db_path=app_dir / "applypilot.db")
        try:
            resume_txt = (_current_profile_path(root).parent / "resume.txt").read_text(encoding="utf-8")
        except Exception:
            resume_txt = ""

        def chat(messages: list[dict]) -> str:
            return llm_util.get_llm_client().chat(messages, max_tokens=1024, temperature=0.7)

        try:
            out = cover_letter.draft_cover_letter(profile, job or {}, resume_txt, chat)
        except cover_letter.CoverLetterError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — never a stack trace to the extension
            raise HTTPException(status_code=502, detail=f"drafting failed: {str(exc)[:200]}") from exc
        return {"text": out["text"], "warnings": out["warnings"], "draft": True, "provider": provider,
                "job": {"title": job.get("title", ""), "company": job.get("company", ""),
                        "source": job.get("source", "")}}

    @app.get("/profile")
    def profile_keys(_: None = Depends(_require_token)) -> dict:
        prof = _load_profile()
        keys = sorted(k for k in _dotted_keys(prof) if not resolve.is_secret_path(k))
        return {"keys": keys}

    @app.get("/profile/full")
    def profile_full(_: None = Depends(_require_token)) -> dict:
        """Current profile VALUES, secret paths stripped. A considered
        relaxation of /profile (key names only): this service is
        127.0.0.1-only, token-gated, and the data is the operator's own —
        but personal.password (and anything else on the denylist) must
        still never leave the service, so _strip_secret_values is the one
        thing standing between this endpoint and a leaked credential."""
        return _strip_secret_values(_load_full_profile())

    @app.get("/profile/counts")
    def profile_counts(_: None = Depends(_require_token)) -> dict:
        """{work_history: N, education: M} for the ACTIVE profile -- lets
        the extension know how many repeating blocks to create before
        filling a Workday-style form that only renders "Work Experience 1"
        plus an "Add Another" button. Counts only, never values. Resolved
        via _load_full_profile (same active-profile resolution as
        /profile/full, i.e. _current_profile_path(root)) so a profile
        switch takes effect immediately, and fails safe to 0 for a missing
        or malformed section rather than raising."""
        prof = _load_full_profile()
        work = prof.get("work_history")
        edu = prof.get("education")
        return {
            "work_history": len(work) if isinstance(work, list) else 0,
            "education": len(edu) if isinstance(edu, list) else 0,
        }

    @app.post("/profile")
    def write_profile(body: dict, _: None = Depends(_require_token)) -> dict:
        """Write the profile: validated, merged so an unseen secret can
        never be wiped by a round-trip, backed up, and written atomically."""
        _validate_profile_body(body)
        path = _current_profile_path(root)
        existing = _read_profile_or_empty(path)
        merged = _merge_preserving_secrets(body, existing)
        _backup_and_write_profile(path, merged)
        return {"ok": True}

    @app.get("/profiles")
    def list_profiles_endpoint(_: None = Depends(_require_token)) -> dict:
        """{profiles: [{id, name, active}], legacy: bool}. An un-migrated
        install (flat profile.json, no profiles/ dir) reports legacy=True
        and an empty list — the options page hides the switcher in that
        case rather than pretending multi-profile support exists."""
        if profiles_mod.is_legacy_layout(root):
            return {"profiles": [], "legacy": True}
        ids = profiles_mod.list_profiles(root)
        active = profiles_mod.get_active(root)
        if not active and len(ids) == 1:
            active = ids[0]
        return {
            "profiles": [
                {"id": pid, "name": _display_name(profiles_mod.profile_dir(root, pid)), "active": pid == active}
                for pid in ids
            ],
            "legacy": False,
        }

    @app.post("/profiles/{pid}/activate")
    def activate_profile(pid: str, _: None = Depends(_require_token)) -> dict:
        if not profiles_mod.is_valid_id(pid):
            raise HTTPException(status_code=422, detail="invalid profile id")
        if profiles_mod.is_legacy_layout(root):
            raise HTTPException(status_code=409, detail="legacy single-profile install — nothing to activate")
        if pid not in profiles_mod.list_profiles(root):
            raise HTTPException(status_code=404, detail=f"unknown profile: {pid}")
        profiles_mod.set_active(root, pid)
        return {"active": pid}

    @app.post("/profiles")
    def create_profile(body: ProfileCreateIn, _: None = Depends(_require_token)) -> dict:
        """Create a new empty profile `{id}`. Refused in legacy layout —
        the options page hides this affordance there too, since a legacy
        install's flat profile.json always wins profile resolution
        regardless of any profiles/ dir created underneath it."""
        pid = body.id
        if not profiles_mod.is_valid_id(pid):
            raise HTTPException(
                status_code=422,
                detail="invalid profile id (lowercase letters, digits, dash, underscore; max 64)",
            )
        if profiles_mod.is_legacy_layout(root):
            raise HTTPException(
                status_code=409,
                detail="legacy single-profile install — run `applypilot profile migrate` first",
            )
        pdir = profiles_mod.profile_dir(root, pid)
        if pdir.exists():
            raise HTTPException(status_code=409, detail=f"profile already exists: {pid}")
        pdir.mkdir(parents=True)
        (pdir / "profile.json").write_text(
            json.dumps({"profile_id": pid}, indent=2), encoding="utf-8"
        )
        if len(profiles_mod.list_profiles(root)) == 1:
            profiles_mod.set_active(root, pid)
        return {"id": pid, "created": True}

    # -----------------------------------------------------------------
    # Résumé import (Copilot v3, section A): upload -> draft profile.
    # Never writes profile.json -- POST /profile above is the only route
    # that persists one. See applypilot.extension.resume_import for the
    # extraction/parsing/canary-stripping pipeline.
    # -----------------------------------------------------------------

    @app.post("/profile/import-resume")
    async def import_resume_endpoint(
        file: UploadFile = File(...),
        allow_identity_change: bool = Form(False),
        _: None = Depends(_require_token),
    ) -> dict:
        """Multipart upload (field name "file"), .pdf/.docx/.txt. Extracts
        text, runs the deterministic + LLM passes, saves the résumé (and a
        resume.txt rendering) into the active profile's directory, and
        returns a DRAFT profile merged over the current one for the
        operator to review -- this endpoint itself never saves it."""
        try:
            # Read one byte past the cap so an oversized upload is caught
            # without ever buffering the whole (potentially huge) file.
            data = await file.read(resume_import.MAX_UPLOAD_BYTES + 1)
        except Exception as exc:  # noqa: BLE001 -- never a stack trace to the extension
            raise HTTPException(status_code=400, detail=f"could not read upload: {exc}") from exc

        if len(data) > resume_import.MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"resume file is too large -- max "
                    f"{resume_import.MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
                ),
            )

        path = _current_profile_path(root)
        existing = _read_profile_or_empty(path)

        try:
            result = resume_import.import_resume(
                filename=file.filename or "",
                data=data,
                existing_profile=existing,
                profile_dir=path.parent,
                allow_identity_change=allow_identity_change,
            )
        except resume_import.IdentityMismatch as exc:
            # 409 with a structured body so the extension can offer the two
            # honest choices (switch/create a profile, or replace knowingly).
            raise HTTPException(status_code=409, detail={
                "code": "identity_mismatch",
                "resume_name": exc.resume_name,
                "profile_name": exc.profile_name,
                "message": str(exc),
            }) from exc
        except resume_import.ResumeImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 -- fail soft, never leak a stack trace
            raise HTTPException(status_code=400, detail=f"could not process resume: {exc}") from exc

        return {
            "profile": result.draft_profile,
            "provenance": result.provenance,
            "warnings": result.warnings,
            "resume": {
                "filename": result.saved_filename,
                "content_type": result.content_type,
                "size": len(data),
            },
        }

    @app.get("/resume/info")
    def resume_info(_: None = Depends(_require_token)) -> dict:
        """Metadata only (filename/content_type/size/mtime) -- lets the
        extension check whether a résumé is on file without downloading it.
        GET /resume itself already answers HEAD requests the same way
        (Starlette adds HEAD automatically for a GET route), so this is a
        JSON-friendly alternative rather than the only option."""
        path = _current_profile_path(root)
        stored = resume_import.find_stored_resume(path.parent)
        if stored is None:
            raise HTTPException(
                status_code=404,
                detail="no resume on file -- upload one via POST /profile/import-resume",
            )
        stat = stored.stat()
        return {
            "filename": stored.name,
            "content_type": resume_import.content_type_for(stored),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        }

    @app.get("/resume")
    def get_resume(_: None = Depends(_require_token)) -> Response:
        """Serve the stored résumé's raw bytes with the right content-type,
        so the extension's content script can attach it to a file input via
        DataTransfer (see the v3 design doc, section B)."""
        path = _current_profile_path(root)
        stored = resume_import.find_stored_resume(path.parent)
        if stored is None:
            raise HTTPException(
                status_code=404,
                detail="no resume on file -- upload one via POST /profile/import-resume",
            )
        data = stored.read_bytes()
        return Response(
            content=data,
            media_type=resume_import.content_type_for(stored),
            headers={"Content-Disposition": f'attachment; filename="{stored.name}"'},
        )

    return app
