"""Every built-in auth, plus a custom one, against a fake API (no network).

Run:  python examples/auth_flows.py
"""

import asyncio
import base64
from collections.abc import Generator
from typing import Any

import httpx
from pydantic import BaseModel

import nanoapi
from nanoapi.auth import ApiKey, Bearer, OAuth2ClientCredentials

# --- fake identity provider + API ---------------------------------------------


class FakeServer:
    def __init__(self) -> None:
        self.tokens_issued = 0
        self.live_tokens: set[str] = set()
        self.log: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.log.append(f"{request.method} {request.url.host}{request.url.path}")
        if request.url.host == "auth.test":
            return self.issue_token(request)
        return httpx.Response(200, json={"caller": self.identify(request)})

    def issue_token(self, request: httpx.Request) -> httpx.Response:
        expected = "Basic " + base64.b64encode(b"my-app:my-secret").decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, json={"error": "invalid_client"})
        self.tokens_issued += 1
        token = f"token-{self.tokens_issued}"
        self.live_tokens.add(token)
        return httpx.Response(200, json={"access_token": token, "expires_in": 3600})

    def identify(self, request: httpx.Request) -> str:
        if key := request.headers.get("x-api-key") or request.url.params.get("api_key"):
            return f"api key {key}"
        if signed := request.headers.get("x-tenant"):
            return f"tenant {signed}"
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth.removeprefix("Bearer ")
            if token.startswith("token-") and token not in self.live_tokens:
                raise _Unauthorized
            return f"bearer {token}"
        if auth.startswith("Basic "):
            return f"basic {base64.b64decode(auth[6:]).decode().split(':')[0]}"
        return "anonymous"


class _Unauthorized(Exception):
    pass


def transport(server: FakeServer) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        try:
            return server(request)
        except _Unauthorized:
            return httpx.Response(401, json={"error": "token expired"})

    return httpx.MockTransport(handler)


# --- the client -------------------------------------------------------------------


class WhoAmI(BaseModel):
    caller: str


class Api(nanoapi.Client):
    whoami = nanoapi.get("/me", response=WhoAmI)


class AsyncApi(nanoapi.AsyncClient):
    whoami = nanoapi.get("/me", response=WhoAmI)


# --- a custom httpx.Auth: anything httpx supports works -----------------------------


class TenantAuth(httpx.Auth):
    """Adds a per-tenant header; write any flow as a generator (see httpx docs)."""

    def __init__(self, tenant: str) -> None:
        self.tenant = tenant

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        request.headers["X-Tenant"] = self.tenant
        yield request


def main() -> None:
    server = FakeServer()

    def client(auth: httpx.Auth | tuple[str, str] | None = None, **kw: Any) -> Api:
        return Api("https://api.test", auth=auth, transport=transport(server), **kw)

    print("none:            ", client().whoami().caller)
    print("basic (tuple):   ", client(("ada", "pw")).whoami().caller)
    print("bearer:          ", client(Bearer("static-token")).whoami().caller)
    print("api key header:  ", client(ApiKey("X-Api-Key", "k-123")).whoami().caller)
    query_key = ApiKey("api_key", "k-456", location="query")
    print("api key query:   ", client(query_key).whoami().caller)
    print("custom httpx.Auth:", client(TenantAuth("acme")).whoami().caller)

    # A callable token is read on every request, e.g. from a vault or a file.
    current = {"token": "rotated-1"}
    api = client(Bearer(lambda: current["token"]))
    first = api.whoami().caller
    current["token"] = "rotated-2"
    print(f"bearer(callable): {first} -> {api.whoami().caller}")

    # Per-call credentials without a new connection pool:
    scoped = client().with_options(headers={"Authorization": "Bearer per-call"})
    print("with_options:    ", scoped.whoami().caller)

    # OAuth 2 client credentials: fetched once, cached, refreshed on 401.
    print("\noauth2 client credentials:")
    oauth = OAuth2ClientCredentials("https://auth.test/oauth/token", "my-app", "my-secret")
    api = client(oauth)
    server.log.clear()
    print("  call 1:", api.whoami().caller)
    print("  call 2:", api.whoami().caller)
    server.live_tokens.clear()  # the provider revokes everything
    print("  after revocation:", api.whoami().caller)
    print("  wire log:", *server.log, sep="\n    ")

    # Bad credentials surface as a normal APIStatusError from the token endpoint.
    bad = client(OAuth2ClientCredentials("https://auth.test/oauth/token", "my-app", "wrong"))
    try:
        bad.whoami()
    except nanoapi.APIStatusError as exc:
        print(f"\nbad secret: {exc.status_code} from {exc.request.url} -> {exc.body}")

    # The same auth objects drive the async client.
    async def run_async() -> str:
        async with AsyncApi("https://api.test", auth=oauth, transport=transport(server)) as aapi:
            return (await aapi.whoami()).caller

    print("async, shared OAuth object:", asyncio.run(run_async()))


if __name__ == "__main__":
    main()
