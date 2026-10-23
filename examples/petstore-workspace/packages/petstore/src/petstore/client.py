"""The hand-written half of the SDK, on top of the generated `_generated` package.

`_generated/` comes from `openapi.json` via `nanosdk generate`: the models, a parameter
model per operation, every route, and `PetstoreBase` / `AsyncPetstoreBase` clients.
This module adds only what the spec can't express: auth, retries, the error model, and
a fix where the live server is stricter than the spec.
"""

from typing import Annotated

import httpx
from pydantic import BaseModel

import nanosdk
from nanosdk.auth import ApiKey

from ._generated.client import DEFAULT_BASE_URL, AsyncPetstoreBase, PetstoreBase
from ._generated.models import ApiResponse, Pet, PetStatus

__all__ = ["DEFAULT_BASE_URL", "AsyncPetstore", "Petstore"]


class UpdatePetForm(BaseModel):
    """`POST /pet/{petId}` parameters, as the live server actually accepts them."""

    pet_id: int
    # The spec marks `name` optional, but the live server answers 400 "No Name
    # provided" without it. So this operation is skipped by the generator (see
    # `skip` in pyproject.toml) and declared here instead.
    name: Annotated[str, nanosdk.Query()]
    status: Annotated[PetStatus | None, nanosdk.Query()] = None


class HandWritten(nanosdk.Routes):
    """Operations the generator skips because the spec gets them wrong."""

    update_pet_form = nanosdk.post(
        "/pet/{pet_id}", request=UpdatePetForm, response=Pet, exclude_unset=True
    )


DEFAULT_RETRY = nanosdk.Retry()


class Petstore(HandWritten, PetstoreBase):
    """Sync Petstore client. `api_key` authorises the store/inventory endpoints."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_key: str | None = "special-key",
        retry: nanosdk.Retry | None = DEFAULT_RETRY,
        transport: httpx.BaseTransport | None = None,  # tests pass httpx.MockTransport
    ) -> None:
        super().__init__(
            base_url,
            auth=ApiKey("api_key", api_key) if api_key else None,
            error_model=ApiResponse,
            retry=retry,
            transport=transport,
        )


class AsyncPetstore(HandWritten, AsyncPetstoreBase):
    """Async twin of `Petstore`."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_key: str | None = "special-key",
        retry: nanosdk.Retry | None = DEFAULT_RETRY,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            base_url,
            auth=ApiKey("api_key", api_key) if api_key else None,
            error_model=ApiResponse,
            retry=retry,
            transport=transport,
        )
