import httpx

_GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_USER_URL = "https://api.github.com/user"


def build_github_auth_url(client_id: str, redirect_uri: str, state: str = "") -> str:
    url = (
        f"{_GITHUB_AUTH_URL}?client_id={client_id}"
        f"&scope=read:user&redirect_uri={redirect_uri}"
    )
    if state:
        url += f"&state={state}"
    return url


async def exchange_github_code(
    code: str, client_id: str, client_secret: str, redirect_uri: str
) -> str | None:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _GITHUB_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("access_token")


async def get_github_profile(access_token: str) -> dict | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            _GITHUB_USER_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return {
            "id": data["id"],
            "login": data["login"],
            "avatar_url": data.get("avatar_url", ""),
        }
