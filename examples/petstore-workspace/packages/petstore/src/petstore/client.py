"""The hand-written half of the SDK: routes over the generated models.

``models.py`` (generated) describes the *bodies*: Pet, Order, User... This file adds
the small request models for path/query/header parameters, which datamodel-codegen
doesn't generate, and wires each OpenAPI operation to a nanoapi route.
"""

from typing import Annotated

import httpx
from pydantic import BaseModel

import nanoapi
from nanoapi.auth import ApiKey

from .models import ApiResponse, Order, Pet, PetStatus, User

DEFAULT_BASE_URL = "https://petstore3.swagger.io/api/v3"

# --- operation parameters -----------------------------------------------------
# Path templates use snake_case placeholders ({pet_id}); only the URL that
# results has to match the spec's /pet/{petId}.


class PetId(BaseModel):
    pet_id: int


class DeletePet(BaseModel):
    pet_id: int
    api_key: Annotated[str | None, nanoapi.Header()] = None


class FindByStatus(BaseModel):
    status: PetStatus = PetStatus.AVAILABLE


class FindByTags(BaseModel):
    tags: list[str]  # repeated: ?tags=a&tags=b


class UpdatePetForm(BaseModel):
    """POST /pet/{petId} takes its fields as query parameters, not a JSON body, so they
    need Query(): on POST, unmarked fields would otherwise go into the body."""

    pet_id: int
    # The spec marks `name` optional, but the live server answers 400 "No Name
    # provided" without it. Hand-written request models are where you encode that.
    name: Annotated[str, nanoapi.Query()]
    status: Annotated[PetStatus | None, nanoapi.Query()] = None


class OrderId(BaseModel):
    order_id: int


class Username(BaseModel):
    username: str


class Login(BaseModel):
    username: str
    password: str


# --- routes, grouped like the spec's tags ----------------------------------------


class PetRoutes(nanoapi.Routes, prefix="/pet"):
    # Generated models are the request: their fields become the JSON body.
    # exclude_unset=True leaves out optional fields you didn't pass (no "id": null).
    add_pet = nanoapi.post("", request=Pet, response=Pet, exclude_unset=True)
    update_pet = nanoapi.put("", request=Pet, response=Pet, exclude_unset=True)
    get_pet = nanoapi.get("/{pet_id}", request=PetId, response=Pet)
    update_pet_form = nanoapi.post("/{pet_id}", request=UpdatePetForm, response=Pet)
    delete_pet = nanoapi.delete("/{pet_id}", request=DeletePet, response=str)
    find_by_status = nanoapi.get("/findByStatus", request=FindByStatus, response=list[Pet])
    find_by_tags = nanoapi.get("/findByTags", request=FindByTags, response=list[Pet])


class StoreRoutes(nanoapi.Routes, prefix="/store"):
    inventory = nanoapi.get("/inventory", response=dict[str, int])
    place_order = nanoapi.post("/order", request=Order, response=Order, exclude_unset=True)
    get_order = nanoapi.get("/order/{order_id}", request=OrderId, response=Order)
    delete_order = nanoapi.delete("/order/{order_id}", request=OrderId)


class UserRoutes(nanoapi.Routes, prefix="/user"):
    create_user = nanoapi.post("", request=User, response=User, exclude_unset=True)
    get_user = nanoapi.get("/{username}", request=Username, response=User)
    delete_user = nanoapi.delete("/{username}", request=Username)
    login = nanoapi.get("/login", request=Login, response=str)
    logout = nanoapi.get("/logout", response=str)


# --- clients ------------------------------------------------------------------------

DEFAULT_RETRY = nanoapi.Retry()


class Petstore(PetRoutes, StoreRoutes, UserRoutes, nanoapi.Client):
    """Sync Petstore client. ``api_key`` authorises the store/inventory endpoints."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_key: str | None = "special-key",
        retry: nanoapi.Retry | None = DEFAULT_RETRY,
        transport: httpx.BaseTransport | None = None,  # tests pass httpx.MockTransport
    ) -> None:
        super().__init__(
            base_url,
            auth=ApiKey("api_key", api_key) if api_key else None,
            error_model=ApiResponse,
            retry=retry,
            transport=transport,
        )


class AsyncPetstore(PetRoutes, StoreRoutes, UserRoutes, nanoapi.AsyncClient):
    """Async twin of ``Petstore``."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_key: str | None = "special-key",
        retry: nanoapi.Retry | None = DEFAULT_RETRY,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            base_url,
            auth=ApiKey("api_key", api_key) if api_key else None,
            error_model=ApiResponse,
            retry=retry,
            transport=transport,
        )
