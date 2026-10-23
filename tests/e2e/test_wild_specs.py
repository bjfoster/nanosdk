"""End to end on real-world OpenAPI specs: generate, type-check strictly, call the live API.

Opt-in, because it downloads specs (GitHub's is 13.5 MB) and talks to real services:

    uv run pytest -m e2e                      # everything
    uv run pytest -m "e2e and not network"    # generate + strict pyright only

Specs are downloaded once into `tests/e2e/.cache/`, pinned to a commit where the spec
lives in git. Set GITHUB_TOKEN to raise GitHub's unauthenticated rate limit (60/hour).
"""

import importlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from nanosdk.auth import Bearer
from nanosdk.codegen import generate

pytestmark = pytest.mark.e2e

CACHE = Path(__file__).parent / ".cache"
USER_AGENT = "nanosdk-e2e (https://github.com/; nanosdk test suite)"


@dataclass(frozen=True)
class Case:
    """One real-world API: where its spec is, how to generate and how to call it."""

    name: str
    url: str
    client: str
    generate: dict[str, Any] = field(default_factory=dict[str, Any])
    client_kwargs: Callable[[], dict[str, Any]] = dict
    usage: str = ""  # type-checked against the generated client
    live: Callable[[Any, ModuleType], None] = lambda api, module: None

    def __str__(self) -> str:
        return self.name


# --- the cases --------------------------------------------------------------------------


def apisguru_live(api: Any, module: ModuleType) -> None:
    metrics = api.get_metrics()
    assert isinstance(metrics, module.models.Metrics)
    assert metrics.num_specs > 1000
    assert "api.github.com" in api.get_services(provider="github.com").data


def openmeteo_live(api: Any, module: ModuleType) -> None:
    forecast = api.get_v1_forecast(
        latitude="51.5072", longitude="-0.1276", hourly=["temperature_2m"], forecast_days=1
    )
    assert isinstance(forecast, module.models.GetV1ForecastResponse)
    assert forecast.hourly is not None
    assert len(forecast.hourly.time) == 24


def nws_live(api: Any, module: ModuleType) -> None:
    point = api.point(latitude=39.7456, longitude=-97.0892)
    assert isinstance(point, module.models.PointGeoJson)
    alerts = api.alerts_active(area=["KS"])
    assert isinstance(alerts, module.models.ActiveAlertCollectionGeoJson)


def pokeapi_live(api: Any, module: ModuleType) -> None:
    pikachu = api.pokemon_retrieve(id="pikachu")
    assert isinstance(pikachu, module.models.PokemonDetail)
    assert pikachu.id == 25
    assert [t.type.name for t in pikachu.types] == ["electric"]
    assert [p.name for p in api.pokemon_list(limit=3).results] == [
        "bulbasaur",
        "ivysaur",
        "venusaur",
    ]


def github_live(api: Any, module: ModuleType) -> None:
    repo = api.repos_get(owner="octocat", repo="Hello-World")
    assert isinstance(repo, module.models.FullRepository)
    assert repo.full_name == "octocat/Hello-World"
    assert api.users_get_by_username(username="octocat").root.login == "octocat"
    found = api.search_repos(q="repo:octocat/Hello-World")
    assert [r.full_name for r in found.items] == ["octocat/Hello-World"]


def github_kwargs() -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN")
    return {"auth": Bearer(token)} if token else {}


CASES = [
    Case(
        "apisguru",
        "https://api.apis.guru/v2/openapi.yaml",
        "ApisGuru",
        usage="assert_type(api.get_metrics(), models.Metrics)\n"
        "api.get_services(provider=1)  # pyright: ignore[reportArgumentType]\n",
        live=apisguru_live,
    ),
    Case(
        "openmeteo",
        "https://raw.githubusercontent.com/open-meteo/open-meteo/"
        "ae5caa86c8b8b5e9c6d5f0b8b48f69dc36487bb4/openapi/forecast.yml",
        "OpenMeteo",
        client_kwargs=lambda: {"base_url": "https://api.open-meteo.com"},  # spec: no servers
        usage="assert_type(api.get_v1_forecast(latitude='1', longitude='2'), "
        "models.GetV1ForecastResponse)\n"
        "api.get_v1_forecast(latitude='1')  # pyright: ignore[reportCallIssue]\n",
        live=openmeteo_live,
    ),
    Case(
        "nws",
        "https://api.weather.gov/openapi.json",
        "Weather",
        client_kwargs=lambda: {"headers": {"User-Agent": USER_AGENT}},  # NWS requires one
        usage="assert_type(api.point(latitude=1.0, longitude=2.0), models.PointGeoJson)\n",
        live=nws_live,
    ),
    Case(
        "pokeapi",
        "https://raw.githubusercontent.com/PokeAPI/pokeapi/"
        "2bf639e5572812a67d2b94903fdcb9d095f5aeb2/openapi.yml",
        "PokeApi",
        # The live API sends nulls where the spec says fields are required.
        generate={"lenient": True},
        usage="assert_type(api.pokemon_retrieve(id='pikachu'), models.PokemonDetail)\n",
        live=pokeapi_live,
    ),
    Case(
        "github",
        "https://raw.githubusercontent.com/github/rest-api-description/"
        "755ee33b4a81d51f6f07a902cce26cbec3b822f2/descriptions/api.github.com/api.github.com.json",
        "GitHub",
        client_kwargs=github_kwargs,
        usage="assert_type(api.repos_get(owner='o', repo='r'), models.FullRepository)\n"
        "api.repos_get(owner='o')  # pyright: ignore[reportCallIssue]\n",
        live=github_live,
    ),
]


# --- plumbing -------------------------------------------------------------------------


def spec_path(case: Case) -> Path:
    suffix = ".json" if case.url.endswith(".json") else ".yml"
    path = CACHE / f"{case.name}{suffix}"
    if not path.exists():
        CACHE.mkdir(exist_ok=True)
        request = urllib.request.Request(case.url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                path.write_bytes(response.read())
        except OSError as exc:
            pytest.skip(f"couldn't download the {case.name} spec: {exc}")
    return path


@pytest.fixture(scope="session")
def built(tmp_path_factory: pytest.TempPathFactory) -> Callable[[Case], tuple[Path, ModuleType]]:
    """Generate each case's package once per session, and import it."""
    root = tmp_path_factory.mktemp("wild")
    cache: dict[str, tuple[Path, ModuleType]] = {}

    def build(case: Case) -> tuple[Path, ModuleType]:
        if case.name not in cache:
            package = f"wild_{case.name}"
            generate(spec_path(case), root / package, name=case.client, **case.generate)
            sys.path.insert(0, str(root))
            try:
                cache[case.name] = (root, importlib.import_module(package))
            finally:
                sys.path.remove(str(root))
        return cache[case.name]

    return build


# --- the tests ----------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=str)
def test_generated_client_passes_strict_pyright(
    case: Case, built: Callable[[Case], tuple[Path, ModuleType]]
) -> None:
    pyright = shutil.which("pyright")
    if pyright is None:
        pytest.skip("pyright not installed")
    root, _ = built(case)
    package = f"wild_{case.name}"
    usage = root / f"usage_{case.name}.py"
    usage.write_text(
        "from typing import assert_type\n"
        f"from {package} import {case.client}, models\n\n"
        f"def use(api: {case.client}) -> None:\n"
        + "".join(f"    {line}\n" for line in case.usage.splitlines())
        + "    pass\n"
    )
    config = {
        "typeCheckingMode": "strict",
        "pythonVersion": "3.13",
        "reportUnnecessaryTypeIgnoreComment": "error",
        "include": [f"{package}/client.py", usage.name],
    }
    (root / f"pyrightconfig_{case.name}.json").write_text(json.dumps(config))
    result = subprocess.run(
        [pyright, "-p", f"pyrightconfig_{case.name}.json", "--pythonpath", sys.executable],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:]


@pytest.mark.network
@pytest.mark.parametrize("case", CASES, ids=str)
def test_live_api(case: Case, built: Callable[[Case], tuple[Path, ModuleType]]) -> None:
    _, module = built(case)
    with getattr(module, case.client)(**case.client_kwargs()) as api:
        case.live(api, module)
