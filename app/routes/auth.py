import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import auth, db, metrics as m
from app.config import settings
from app.templates_env import templates


def _base_url(request: Request) -> str:
    if settings.base_url:
        return settings.base_url.rstrip("/")
    return str(request.base_url).rstrip("/")

router = APIRouter()


@router.get("/login")
async def login_page(request: Request, status: str = ""):
    client_id = db.get_setting("github_client_id")
    github_enabled = db.get_setting("github_oauth_enabled") == "true" and bool(client_id)
    return templates.TemplateResponse(request, "login.html", {
        "status": status,
        "github_enabled": github_enabled,
    })


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    user = db.get_user_by_username(username)
    if not user or not db.verify_password(password, user["password_hash"] or ""):
        m.LOGIN_ATTEMPTS_TOTAL.labels(result="failure").inc()
        return RedirectResponse("/login?status=invalid", status_code=302)
    m.LOGIN_ATTEMPTS_TOTAL.labels(result="success").inc()
    token = db.create_session(user["id"])
    request.session["token"] = token
    return RedirectResponse("/", status_code=302)


@router.get("/auth/github")
async def github_redirect(request: Request):
    client_id = db.get_setting("github_client_id")
    if not client_id or db.get_setting("github_oauth_enabled") != "true":
        return RedirectResponse("/login", status_code=302)
    redirect_uri = _base_url(request) + "/auth/callback"
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    return RedirectResponse(
        auth.build_github_auth_url(client_id, redirect_uri, state), status_code=302
    )


@router.get("/auth/callback")
async def github_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return RedirectResponse("/login?status=error", status_code=302)

    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or not secrets.compare_digest(state, expected_state):
        return RedirectResponse("/login?status=error", status_code=302)

    client_id = db.get_setting("github_client_id")
    client_secret = db.get_setting("github_client_secret")
    redirect_uri = _base_url(request) + "/auth/callback"

    access_token = await auth.exchange_github_code(code, client_id, client_secret, redirect_uri)
    if not access_token:
        return RedirectResponse("/login?status=error", status_code=302)

    profile = await auth.get_github_profile(access_token)
    if not profile:
        return RedirectResponse("/login?status=error", status_code=302)

    user = db.upsert_github_user(profile["id"], profile["login"], profile["avatar_url"])

    if user["role"] in ("pending", "revoked"):
        return RedirectResponse(f"/login?status={user['role']}", status_code=302)

    token = db.create_session(user["id"])
    request.session["token"] = token
    return RedirectResponse("/", status_code=302)


@router.post("/auth/logout")
async def logout(request: Request):
    token = request.session.get("token", "")
    if token:
        db.delete_session(token)
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
