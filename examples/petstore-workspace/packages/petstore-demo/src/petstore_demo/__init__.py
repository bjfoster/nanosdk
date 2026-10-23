"""A small app that depends on the ``petstore`` workspace package.

Run:  uv run petstore-demo          (from the workspace root; talks to the live demo server)
"""

import asyncio
import random

import nanosdk
from petstore import (
    ApiResponse,
    AsyncPetstore,
    Category,
    OrderStatus,
    Pet,
    PetStatus,
    Petstore,
    Tag,
)


def main() -> None:
    pet_id = random.randint(10_000_000, 99_999_999)  # the demo server is shared; avoid clashes

    with Petstore() as api:
        # Generated model fields are snake_case in Python, camelCase on the wire.
        created = api.add_pet(
            id=pet_id,
            name="Biscuit",
            photo_urls=["https://example.com/biscuit.jpg"],
            category=Category(id=1, name="Dogs"),
            tags=[Tag(id=7, name="good-boy")],
            status=PetStatus.AVAILABLE,
        )
        print(f"created  #{created.id} {created.name} ({created.status})")

        pet = api.get_pet(pet_id=pet_id)
        print(f"fetched  {pet.name}, photos {pet.photo_urls}")

        # Update with the full model (PUT /pet) or the form-style endpoint (POST /pet/{id}).
        api.update_pet(**pet.model_dump(exclude={"status"}), status=PetStatus.PENDING)
        sold = api.update_pet_form(pet_id=pet_id, name=pet.name, status=PetStatus.SOLD)
        print(f"updated  status -> {sold.status}")

        try:
            available = api.find_by_status(status="available")
            print(f"{len(available)} pets currently available")
        except nanosdk.ResponseValidationError as exc:
            # The demo server is shared: other people's pets may break the spec (say, no
            # photoUrls, which the spec requires). The models enforce the spec.
            print(f"available pets: {exc.error.error_count()} records break the spec")

        try:
            counts = api.inventory()  # needs the api_key header, sent by ApiKey auth
            print("inventory:", {k: v for k, v in sorted(counts.items()) if k in PetStatus})
        except nanosdk.APIStatusError as exc:
            # error_model=ApiResponse (generated) parses error bodies into exc.error
            error: ApiResponse | None = exc.error
            print(
                f"inventory unavailable: {exc.status_code} {error.message if error else exc.body}"
            )

        order = api.place_order(pet_id=pet_id, quantity=1, status=OrderStatus.PLACED)
        print(f"ordered  #{order.id} for pet {order.pet_id}: {order.status}")

        print("deleted ", api.delete_pet(pet_id=pet_id) or "ok")
        try:
            api.get_pet(pet_id=pet_id)
        except nanosdk.APIStatusError as exc:
            print(f"gone     {exc.status_code}: {exc.body}")

    asyncio.run(fetch_some())


async def fetch_some() -> None:
    """The same SDK, async: look up a handful of pets concurrently."""
    async with AsyncPetstore() as api:
        try:
            pets = await api.find_by_status(status="sold")
        except nanosdk.ResponseValidationError:
            print("async    skipped: the shared server's data breaks the spec right now")
            return
        ids = [pet.id for pet in pets[:5] if pet.id is not None]
        details: list[Pet] = await asyncio.gather(*(api.get_pet(pet_id=i) for i in ids))
        print("async   ", ", ".join(pet.name for pet in details))
