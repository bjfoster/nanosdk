"""Custom behaviour by subclassing: request signing, response envelopes, domain
exceptions, idempotency-aware retries and per-route ``meta``.

The API is an in-process fake bank, so this runs offline.

Run:  python examples/custom_client.py
"""

import hashlib
import hmac
import json
import time
import uuid
from typing import Annotated, Any

import httpx
from pydantic import BaseModel, Field, ValidationError

import nanosdk

SECRET = b"shared-secret"


def signature(method: str, path: str, body: bytes, timestamp: str) -> str:
    message = b"\n".join([method.encode(), path.encode(), timestamp.encode(), body])
    return hmac.new(SECRET, message, hashlib.sha256).hexdigest()


# --- the fake bank ---------------------------------------------------------------


def ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "data": data})


def fail(status: int, code: str, message: str, **details: Any) -> httpx.Response:
    error = {"code": code, "message": message, "details": details}
    return httpx.Response(status, json={"ok": False, "error": error})


class FakeBank:
    """Checks signatures, wraps everything in {"ok", "data" | "error"}, honours
    Idempotency-Key, and drops the response of the first transfer *after* committing
    it — the classic case where a blind retry would move the money twice."""

    def __init__(self) -> None:
        self.balances = {"acc_alice": 100_00, "acc_bob": 5_00}
        self.receipts: dict[str, dict[str, Any]] = {}
        self.lose_next_response = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path != "/health":
            ts = request.headers.get("x-timestamp", "")
            expected = signature(request.method, path, request.content, ts)
            if not hmac.compare_digest(request.headers.get("x-signature", ""), expected):
                return fail(401, "bad_signature", "request signature mismatch")

        if request.method == "GET" and path == "/health":
            return ok({"status": "green"})
        if request.method == "GET" and path.startswith("/accounts/"):
            account = path.removeprefix("/accounts/")
            if account not in self.balances:
                return fail(404, "not_found", f"no account {account!r}")
            return ok({"id": account, "balance_cents": self.balances[account]})
        if request.method == "POST" and path == "/transfers":
            key = request.headers["idempotency-key"]
            if key in self.receipts:  # a retry of something we already did
                return ok(self.receipts[key])
            body = json.loads(request.content)
            src, dst, amount = body["from_account"], body["to_account"], body["amount_cents"]
            if self.balances[src] < amount:
                return fail(
                    422, "insufficient_funds", "not enough money", balance=self.balances[src]
                )
            self.balances[src] -= amount
            self.balances[dst] += amount
            receipt = {"id": f"tr_{len(self.receipts) + 1}", "amount_cents": amount}
            self.receipts[key] = receipt
            if self.lose_next_response:
                self.lose_next_response = False
                return httpx.Response(503)  # committed, but the client never hears back
            return ok(receipt)
        return fail(404, "not_found", "no such route")


# --- models -----------------------------------------------------------------------


class Health(BaseModel):
    status: str


class AccountId(BaseModel):
    account_id: str


class Account(BaseModel):
    id: str
    balance_cents: int


class Transfer(BaseModel):
    from_account: str
    to_account: str
    amount_cents: int
    # A fresh key per call, sent as the `idempotency-key` header and reused by retries.
    idempotency_key: Annotated[str, nanosdk.Header()] = Field(
        default_factory=lambda: str(uuid.uuid4())
    )


class Receipt(BaseModel):
    id: str
    amount_cents: int


class ApiError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


# --- domain exceptions -----------------------------------------------------------


class BankError(Exception):
    def __init__(self, status: int, error: ApiError) -> None:
        super().__init__(f"{status} {error.code}: {error.message}")
        self.status = status
        self.error = error


class NotFound(BankError):
    pass


class InsufficientFunds(BankError):
    pass


# --- retries that know about idempotency -----------------------------------------


class IdempotentRetry(nanosdk.Retry):
    """The default policy never retries POST. Retry it when it carries an
    Idempotency-Key, since the server will then dedupe it."""

    def should_retry(
        self, attempt: int, request: httpx.Request, response: httpx.Response | None
    ) -> bool:
        if request.method == "POST" and "idempotency-key" in request.headers:
            if attempt >= self.max_retries:
                return False
            return response is None or response.status_code in self.statuses
        return super().should_retry(attempt, request, response)


# --- the client --------------------------------------------------------------------


class Bank(nanosdk.Client):
    health = nanosdk.get("/health", response=Health, meta={"signed": False, "retry": False})
    get_account = nanosdk.get("/accounts/{account_id}", request=AccountId, response=Account)
    transfer = nanosdk.post("/transfers", request=Transfer, response=Receipt)

    def __init__(self, transport: httpx.BaseTransport) -> None:
        super().__init__(
            "https://bank.test", retry=IdempotentRetry(backoff=0.01), transport=transport
        )

    def _prepare_request(
        self, request: httpx.Request, route: nanosdk.Route[Any, Any, Any]
    ) -> httpx.Request:
        """Runs on every attempt, so each retry gets a fresh timestamp + signature."""
        if route.meta.get("signed", True):
            ts = str(int(time.time()))
            request.headers["X-Timestamp"] = ts
            request.headers["X-Signature"] = signature(
                request.method, request.url.path, request.content, ts
            )
        print(f"  -> {request.method} {request.url.path}")
        return request

    def _process_response(
        self, response: httpx.Response, route: nanosdk.Route[Any, Any, Any]
    ) -> httpx.Response:
        """Unwrap {"ok": true, "data": X} so response models can describe just X."""
        print(f"  <- {response.status_code} ({route.name})")
        if response.is_success:
            data = response.json()["data"]
            return httpx.Response(response.status_code, json=data, request=response.request)
        return response

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Turn error envelopes into domain exceptions; anything else -> APIStatusError."""
        if response.is_success:
            return
        try:
            error = ApiError.model_validate(response.json()["error"])
        except (ValueError, KeyError, TypeError, ValidationError):
            super()._raise_for_status(response)
            return
        exc_type = {404: NotFound, 422: InsufficientFunds}.get(response.status_code, BankError)
        raise exc_type(response.status_code, error)

    def _retry_policy(self, route: nanosdk.Route[Any, Any, Any]) -> nanosdk.Retry | None:
        return None if route.meta.get("retry") is False else super()._retry_policy(route)


def main() -> None:
    server = FakeBank()
    bank = Bank(httpx.MockTransport(server))

    print("health (unsigned, never retried):")
    print("  ", bank.health())

    print("\ntransfer $30 alice -> bob (first response is lost, then retried):")
    receipt = bank.transfer(from_account="acc_alice", to_account="acc_bob", amount_cents=30_00)
    print("  ", receipt)
    alice = bank.get_account(account_id="acc_alice")
    print(f"  alice now has ${alice.balance_cents / 100:.2f}: charged once, not twice")

    print("\ntransfer $500 bob -> alice:")
    try:
        bank.transfer(from_account="acc_bob", to_account="acc_alice", amount_cents=500_00)
    except InsufficientFunds as exc:
        print(f"   InsufficientFunds: {exc} (balance {exc.error.details['balance']})")

    print("\nunknown account:")
    try:
        bank.get_account(account_id="acc_mallory")
    except NotFound as exc:
        print(f"   NotFound: {exc}")

    print("\nthe same route through a plain client (no signing hook):")
    plain = nanosdk.Client("https://bank.test", transport=httpx.MockTransport(server))
    try:
        Bank.get_account.bind(plain)(account_id="acc_alice")
    except nanosdk.APIStatusError as exc:
        print(f"   APIStatusError {exc.status_code}: {exc.body['error']['code']}")


if __name__ == "__main__":
    main()
