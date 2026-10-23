"""nanosdk.codegen: generate clients from OpenAPI specs, then use and type-check them."""

import importlib
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from openapi_spec_validator import validate
from pydantic import ValidationError

import nanosdk
from nanosdk.codegen import SpecError, check, generate, load_spec
from nanosdk.codegen.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
KITCHEN = FIXTURES / "kitchen_sink.yml"
PETSTORE = Path(__file__).parents[1] / "examples/petstore-workspace/packages/petstore/openapi.json"


def import_package(root: Path, name: str) -> ModuleType:
    sys.path.insert(0, str(root))
    try:
        importlib.invalidate_caches()
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(root))


@pytest.fixture
def kitchen(tmp_path: Path) -> Iterator[ModuleType]:
    generate(KITCHEN, tmp_path / "kitchen_gen", name="Kitchen")
    yield import_package(tmp_path, "kitchen_gen")
    for module in [m for m in sys.modules if m.startswith("kitchen_gen")]:
        del sys.modules[module]


def serve(*responses: httpx.Response) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return queue.pop(0) if queue else httpx.Response(200, json={})

    return httpx.MockTransport(handler), seen


ITEM = {"id": 1, "name": "whisk", "kind": "tool", "createdAt": "2026-01-02T03:04:05Z"}


def test_fixture_is_a_valid_spec() -> None:
    validate(load_spec(KITCHEN))  # pyright: ignore[reportUnknownMemberType]


# --- the generated client on the wire ---------------------------------------------------


def test_query_header_and_enum_params(kitchen: ModuleType) -> None:
    transport, seen = serve(httpx.Response(200, json=[ITEM]))
    api = kitchen.Kitchen(transport=transport)

    items = api.list_items(page_size=10, sort_order="desc", tag=["a", "b"], x_request_id="r1")

    assert items == [kitchen.models.Item.model_validate(ITEM)]
    assert items[0].kind is kitchen.models.ItemKind.TOOL
    request = seen[0]
    assert str(request.url) == (
        "https://api.kitchen.test/v2/items?pageSize=10&sort-order=desc&tag=a&tag=b"
    )
    assert request.headers["X-Request-Id"] == "r1"


def test_constraints_and_literals_validate_before_sending(kitchen: ModuleType) -> None:
    transport, seen = serve()
    api = kitchen.Kitchen(transport=transport)
    with pytest.raises(ValidationError):
        api.list_items(page_size=500)  # maximum: 100
    with pytest.raises(ValidationError):
        api.list_items(sort_order="sideways")
    with pytest.raises(ValidationError):
        api.get_item(item_id=0)  # minimum: 1
    with pytest.raises(ValidationError):
        api.search(class_="lower")  # pattern ^[A-Z]+$
    assert seen == []


def test_bodies(kitchen: ModuleType) -> None:
    transport, seen = serve(httpx.Response(201, json=ITEM), httpx.Response(204))
    api = kitchen.Kitchen(transport=transport)

    assert api.create_item(name="whisk", kind="tool").id == 1  # the body model is the request
    new_item = kitchen.models.NewItem(name="pan")
    assert api.replace_item(item_id=1, body=new_item) is None

    assert json.loads(seen[0].content) == {"name": "whisk", "kind": "tool"}
    assert seen[1].method == "PUT"
    assert seen[1].url.path == "/v2/items/1"
    assert json.loads(seen[1].content) == {"name": "pan"}  # exclude_unset: no nulls


def test_inline_response_text_dict_and_multipart(kitchen: ModuleType) -> None:
    transport, seen = serve(
        httpx.Response(200, json={"views": 3, "rating": None}),
        httpx.Response(200, text="stored"),
        httpx.Response(200, json={"A": 1}),
    )
    api = kitchen.Kitchen(transport=transport)

    stats = api.get_items_stats(item_id=1)
    assert type(stats).__name__ == "GetItemsStatsResponse"
    assert (stats.views, stats.rating) == (3, None)
    assert api.upload(caption="hi", file=("a.txt", b"data", "text/plain")) == "stored"
    assert api.search(class_="ABC") == {"A": 1}

    assert seen[1].headers["content-type"].startswith("multipart/form-data")
    assert b'name="caption"' in seen[1].content
    assert str(seen[2].url).endswith("/search?class=ABC")


def test_error_model_reserved_names_and_deprecated(kitchen: ModuleType) -> None:
    transport, _ = serve(httpx.Response(404, json={"message": "nope", "code": 7}))
    api = kitchen.Kitchen(transport=transport)
    with pytest.raises(nanosdk.APIStatusError) as info:
        api.get_item(item_id=9)
    assert info.value.error == kitchen.models.Error(message="nope", code=7)

    assert hasattr(api, "close_")  # operationId `close` doesn't shadow Client.close
    assert not hasattr(api, "delete_item")  # deprecated: skipped by default


async def test_async_client(kitchen: ModuleType) -> None:
    transport, seen = serve(httpx.Response(200, json=ITEM))
    async with kitchen.AsyncKitchen(transport=transport) as api:
        item = await api.get_item(item_id=1)
    assert item.name == "whisk"
    assert seen[0].url.path == "/v2/items/1"


def test_http_client_and_base_url_defaults(kitchen: ModuleType) -> None:
    transport, seen = serve(httpx.Response(200, json=ITEM))
    own = httpx.Client(base_url="https://elsewhere.test", transport=transport)
    kitchen.Kitchen(http_client=own).get_item(item_id=1)
    assert str(seen[0].url) == "https://elsewhere.test/items/1"
    assert kitchen.DEFAULT_BASE_URL == "https://api.kitchen.test/v2"


# --- type checking ------------------------------------------------------------------------


def test_generated_code_passes_strict_pyright(tmp_path: Path) -> None:
    pyright = shutil.which("pyright")
    if pyright is None:
        pytest.skip("pyright not installed")
    # skip: the skipped route's response model must not be imported (unused import).
    generate(KITCHEN, tmp_path / "kitchen_gen", name="Kitchen", skip=["GET /items/{item-id}/stats"])
    generate(REGRESSIONS, tmp_path / "reg_gen", name="Reg")
    (tmp_path / "usage.py").write_text(
        "from typing import assert_type\n"
        "from kitchen_gen import Kitchen\n"
        "from kitchen_gen.models import Item\n\n"
        "def use(api: Kitchen) -> None:\n"
        "    assert_type(api.list_items(page_size=5), list[Item])\n"
        "    assert_type(api.create_item(name='x'), Item)\n"
        "    assert_type(api.search(class_='A'), dict[str, int])\n"
        "    api.get_item(item_id='one')  # pyright: ignore[reportArgumentType]\n"
        "    api.list_items(bogus=1)  # pyright: ignore[reportCallIssue]\n"
    )
    config = {
        "typeCheckingMode": "strict",
        "pythonVersion": "3.13",
        "reportUnnecessaryTypeIgnoreComment": "error",
        "exclude": ["kitchen_gen/models.py", "reg_gen/models.py"],  # datamodel-codegen's
    }
    (tmp_path / "pyrightconfig.json").write_text(json.dumps(config))
    result = subprocess.run(
        [pyright, "--pythonpath", sys.executable],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout


# --- generate / check / options -----------------------------------------------------------


def test_check_reports_stale_files(tmp_path: Path) -> None:
    out = tmp_path / "gen"
    assert check(KITCHEN, out, name="Kitchen") == [
        out / f for f in ("__init__.py", "client.py", "models.py")
    ]
    generate(KITCHEN, out, name="Kitchen")
    assert check(KITCHEN, out, name="Kitchen") == []
    (out / "client.py").write_text((out / "client.py").read_text() + "\n# edited\n")
    assert check(KITCHEN, out, name="Kitchen") == [out / "client.py"]


def test_rename_skip_and_include_deprecated(tmp_path: Path) -> None:
    generate(
        KITCHEN,
        tmp_path / "renamed_gen",
        name="Kitchen",
        rename={"listItems": "items", "GET /items/{item-id}/stats": "stats"},
        skip=["search", "POST /upload", "GET /items/{item-id}/stats"],
        include_deprecated=True,
    )
    source = (tmp_path / "renamed_gen" / "client.py").read_text()
    assert "search =" not in source
    assert "upload =" not in source
    assert "stats =" not in source
    assert "class StatsParams" not in source
    models = (tmp_path / "renamed_gen" / "models.py").read_text()
    assert "class StatsResponse(BaseModel)" in models  # skipped routes keep their models
    assert "    items = nanosdk.get(" in source
    assert "    delete_item = nanosdk.delete(" in source
    assert "Deprecated." in source


def test_transform_fixes_the_spec_first(tmp_path: Path) -> None:
    def add_operation_id(spec: dict[str, Any]) -> dict[str, Any]:
        spec["paths"]["/items/{item-id}/stats"]["get"]["operationId"] = "itemStats"
        return spec

    generate(KITCHEN, tmp_path / "t_gen", name="Kitchen", transform=add_operation_id)
    assert "    item_stats = nanosdk.get(" in (tmp_path / "t_gen" / "client.py").read_text()


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        (lambda s: s.update(openapi="2.0"), "only OpenAPI 3"),
        (lambda s: s["paths"]["/search"]["get"].update(operationId="listItems"), "both become"),
        (
            lambda s: s["paths"]["/search"]["get"].update(
                responses={
                    "200": {
                        "description": "x",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "https://example.com/other.json#/X"}
                            }
                        },
                    }
                }
            ),
            "only local",
        ),
    ],
)
def test_spec_errors(tmp_path: Path, patch: Callable[[dict[str, Any]], None], message: str) -> None:
    def transform(spec: dict[str, Any]) -> dict[str, Any]:
        patch(spec)
        return spec

    with pytest.raises(SpecError, match=message):
        generate(KITCHEN, tmp_path / "bad", transform=transform)


# --- the command line -------------------------------------------------------------------


def test_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.nanosdk.codegen]\n"
        f"spec = {str(KITCHEN)!r}\n"
        'output = "cli_gen"\n'
        'name = "Kitchen"\n'
        "rename = { listItems = 'items' }\n"
    )
    main(["generate"])
    assert f"wrote {Path('cli_gen', 'client.py')}" in capsys.readouterr().out  # native separators
    assert "    items = nanosdk.get(" in (tmp_path / "cli_gen/client.py").read_text()

    main(["generate", "--check"])
    assert "up to date" in capsys.readouterr().out

    (tmp_path / "cli_gen/models.py").write_text("# stale\n")
    with pytest.raises(SystemExit) as stale:
        main(["generate", "--check"])
    assert stale.value.code == 1


# --- a real spec --------------------------------------------------------------------------


def test_petstore(tmp_path: Path) -> None:
    generate(PETSTORE, tmp_path / "pet_gen", name="Petstore", base_url="https://pets.test/v3")
    pet_gen = import_package(tmp_path, "pet_gen")
    transport, seen = serve(
        httpx.Response(200, json={"id": 1, "name": "rex", "photoUrls": []}),
        httpx.Response(200, json=[]),
    )
    api = pet_gen.Petstore(transport=transport)
    assert api.add_pet(name="rex", photo_urls=[]).name == "rex"
    assert api.find_pets_by_status(status="sold") == []
    assert json.loads(seen[0].content) == {"name": "rex", "photoUrls": []}
    assert str(seen[1].url) == "https://pets.test/v3/pet/findByStatus?status=sold"
    for module in [m for m in sys.modules if m.startswith("pet_gen")]:
        del sys.modules[module]


# --- regressions from real-world specs (see tests/e2e) ------------------------------------

REGRESSIONS = FIXTURES / "regressions.yml"


@pytest.fixture
def reg(tmp_path: Path) -> Iterator[ModuleType]:
    generate(REGRESSIONS, tmp_path / "reg_gen", name="Reg")
    yield import_package(tmp_path, "reg_gen")
    for module in [m for m in sys.modules if m.startswith("reg_gen")]:
        del sys.modules[module]


def test_regression_spec_is_valid() -> None:
    validate(load_spec(REGRESSIONS))  # pyright: ignore[reportUnknownMemberType]


def test_openapi_30_nullable_formats_and_noop_constraints(reg: ModuleType) -> None:
    """GitHub: required + `nullable: true` fields must accept null (datamodel-codegen
    ignores `nullable` on required fields); `email`/`uri` are plain str."""
    body = {"name": "x", "language": None, "homepage": None, "topics": [], "owner": None}
    transport, _ = serve(httpx.Response(200, json=body))
    repo = reg.Reg(transport=transport).get_repo(name="x")
    assert (repo.language, repo.homepage, repo.owner, repo.topics) == (None, None, None, [])
    assert reg.models.Repo.model_fields["homepage"].annotation == str | None
    assert reg.models.Repo.model_fields["topics"].annotation == list[str]  # no wrapper


def test_discriminated_union_survives_component_renames(reg: ModuleType) -> None:
    """GitHub: `discriminator.mapping` refs must follow `public-user` -> `PublicUser`."""
    transport, _ = serve(
        httpx.Response(200, json={"kind": "public", "login": "a", "email": "not-an-email"}),
        httpx.Response(200, json={"kind": "private", "login": "b", "plan": "pro"}),
    )
    api = reg.Reg(transport=transport)
    assert isinstance(api.get_owner_ids(owner_id="1").root, reg.models.PublicUser)
    assert isinstance(api.get_owner_ids(owner_id="2").root, reg.models.PrivateUser)


def test_parameter_naming_unions_and_untyped_responses(reg: ModuleType) -> None:
    transport, seen = serve(httpx.Response(200, json={"anything": [1, 2]}))
    api = reg.Reg(transport=transport)

    result = api.get_interval(interval="P1D", date_=date(2026, 1, 2), id=7)

    assert result == {"anything": [1, 2]}  # no schema: `object`
    assert str(seen[0].url) == "https://reg.test/intervals/P1D?date=2026-01-02&id=7"
    params = reg.client.GetIntervalParams.model_fields
    assert params["interval"].annotation is str  # union of strings, not a missing class
    assert "id" in params  # builtins are fine as field names; only `date` shadows a type


def test_geojson_properties_property(reg: ModuleType) -> None:
    """NWS: an inline object with a property named `properties` crashed
    datamodel-codegen; it's hoisted into a named class first."""
    feature = {"id": "f1", "properties": {"name": "Zone 1"}}
    transport, _ = serve(
        httpx.Response(200, json={"type": "FeatureCollection", "features": [feature]})
    )
    collection = reg.Reg(transport=transport).list_features()
    assert collection.features[0].properties.name == "Zone 1"
    assert hasattr(reg.models, "FeatureCollectionFeaturesItem")


def test_lenient_accepts_responses_that_break_the_spec(tmp_path: Path) -> None:
    """PokeAPI: the live API omits/nulls fields the spec requires."""
    generate(REGRESSIONS, tmp_path / "lenient_gen", name="Reg", lenient=True)
    lenient_gen = import_package(tmp_path, "lenient_gen")
    transport, _ = serve(httpx.Response(200, json={"name": "x"}))  # most required fields missing
    repo = lenient_gen.Reg(transport=transport).get_repo(name="x")
    assert (repo.name, repo.topics) == ("x", None)
    for module in [m for m in sys.modules if m.startswith("lenient_gen")]:
        del sys.modules[module]


def test_naming() -> None:
    from nanosdk.codegen._openapi import field_name, snake

    assert snake("listAPIs") == "list_apis"  # APIs.guru
    assert snake("getURLsForRepo") == "get_urls_for_repo"
    assert snake("userIDs") == "user_ids"
    assert snake("HTTPServer") == "http_server"
    assert [field_name(n) for n in ("date", "json", "list", "id", "filter")] == [
        "date_",
        "json_",
        "list_",
        "id",
        "filter",
    ]
