"""Offline tests for the SDK, plus a guard that models.py matches openapi.json."""

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

import nanoapi
from petstore import ApiResponse, AsyncPetstore, Pet, PetStatus, Petstore

PACKAGE_DIR = Path(__file__).parent.parent


class Recorder:
    """Answers every request with ``response``, or ``response(request)`` if callable."""

    def __init__(
        self, response: httpx.Response | Callable[[httpx.Request], httpx.Response]
    ) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if isinstance(self.response, httpx.Response):
            return self.response
        return self.response(request)


def petstore(
    response: httpx.Response | Callable[[httpx.Request], httpx.Response],
) -> tuple[Petstore, Recorder]:
    recorder = Recorder(response)
    return Petstore("https://pets.test/v3", transport=httpx.MockTransport(recorder)), recorder


PET_JSON = {"id": 1, "name": "rex", "photoUrls": ["a.jpg"], "status": "available"}


def test_snake_case_in_python_camel_case_on_the_wire() -> None:
    api, server = petstore(httpx.Response(200, json=PET_JSON))

    pet = api.add_pet(name="rex", photo_urls=["a.jpg"], status=PetStatus.AVAILABLE)

    assert pet.photo_urls == ["a.jpg"]
    # exclude_unset: optional fields we didn't pass (id, category, tags) aren't sent
    assert json.loads(server.requests[0].content) == {
        "name": "rex",
        "photoUrls": ["a.jpg"],
        "status": "available",
    }


def test_path_query_and_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        found = request.url.path.endswith("/findByTags")
        return httpx.Response(200, json=[PET_JSON] if found else PET_JSON)

    api, server = petstore(handler)

    api.find_by_tags(tags=["a", "b"])
    api.update_pet_form(pet_id=1, name="max", status=PetStatus.SOLD)

    find, update = server.requests
    assert str(find.url) == "https://pets.test/v3/pet/findByTags?tags=a&tags=b"
    assert find.headers["api_key"] == "special-key"
    assert str(update.url) == "https://pets.test/v3/pet/1?name=max&status=sold"
    assert update.content == b""


def test_error_bodies_parse_into_the_generated_model() -> None:
    api, _ = petstore(httpx.Response(500, json={"code": 500, "message": "boom"}))

    with pytest.raises(nanoapi.APIStatusError) as info:
        api.get_pet(pet_id=1)

    assert info.value.error == ApiResponse(code=500, message="boom")


async def test_async_client() -> None:
    transport = httpx.MockTransport(Recorder(httpx.Response(200, json=PET_JSON)))
    async with AsyncPetstore("https://pets.test/v3", transport=transport) as api:
        pet = await api.get_pet(pet_id=1)
    assert pet == Pet(id=1, name="rex", photo_urls=["a.jpg"], status=PetStatus.AVAILABLE)


def test_generated_models_are_up_to_date() -> None:
    """Fails if someone edits models.py by hand or updates openapi.json without
    regenerating. Fix: uv run --directory packages/petstore datamodel-codegen"""
    codegen = shutil.which("datamodel-codegen")
    if codegen is None:
        pytest.skip("datamodel-codegen not installed (uv sync --group dev)")
    committed = PACKAGE_DIR / "src" / "petstore" / "models.py"
    # Generate next to the real file: the ruff formatter codegen runs picks up the
    # ruff config for the output path, so a temp dir elsewhere would format differently.
    fresh = committed.with_name("_models_check.py")
    try:
        subprocess.run([codegen, "--output", str(fresh)], cwd=PACKAGE_DIR, check=True)
        assert fresh.read_text() == committed.read_text()
    finally:
        fresh.unlink(missing_ok=True)
