"""Generate a typed nanosdk client package from an OpenAPI 3.x document.

```bash
nanosdk generate openapi.yml -o src/my_sdk/_generated --name MyApi
```

writes a package with two modules:

- `models.py`: pydantic models for the spec's schemas, by datamodel-codegen;
- `client.py`: a parameter model per operation, a `nanosdk.Routes` group per tag, and
  `MyApi` / `AsyncMyApi` clients whose `base_url` defaults to the spec's first server.

Treat the package as build output: regenerate rather than edit it, and customise by
subclassing the clients in your own module. Needs the `nanosdk[codegen]` extra.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

from ._openapi import Api, Reader, SpecError, Transform, pascal

__all__ = ["SpecError", "Transform", "check", "generate", "load_spec"]

FILES = ("__init__.py", "client.py", "models.py")

DATAMODEL_CODEGEN_ARGS = [
    "--input-file-type", "openapi",
    "--openapi-scopes", "schemas",
    "--output-model-type", "pydantic_v2.BaseModel",
    "--output-datetime-class", "datetime",
    "--target-python-version", "3.13",
    "--use-annotated",
    "--use-standard-collections",
    "--use-union-operator",
    "--field-constraints",
    "--snake-case-field",
    "--alias-generator", "to_camel",
    "--allow-population-by-field-name",
    "--collapse-root-models",
    "--naming-strategy", "parent-prefixed",
    "--capitalise-enum-members",
    "--set-default-enum-member",
    "--use-subclass-enum",
    "--use-schema-description",
    "--use-field-description",
    "--use-double-quotes",
    "--disable-timestamp",
    "--formatters", "ruff-format",
    # Plain `str` for these: `AnyUrl` isn't a str, `EmailStr` needs email-validator, and
    # strict validation of response values only breaks on real-world data.
    "--type-mappings",
    "email=string", "idn-email=string", "uri=string", "uri-reference=string",
    "iri=string", "iri-reference=string",
]  # fmt: skip
"""datamodel-codegen settings: snake_case fields with camelCase (or explicit) aliases,
modern typing, `datetime` that accepts offset-less values, and enums as `StrEnum`."""


def load_spec(path: str | Path) -> dict[str, Any]:
    """Read an OpenAPI document from a YAML or JSON file.

    Args:
        path: The spec file.

    Returns:
        The spec as a dict.
    """
    text = Path(path).read_text()
    if Path(path).suffix == ".json":
        loaded: Any = json.loads(text)
    else:
        import yaml  # the codegen extra

        loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise SpecError(f"{path} isn't an OpenAPI document (expected a mapping)")
    return cast(dict[str, Any], loaded)


def generate(
    spec: str | Path,
    output: str | Path,
    *,
    name: str | None = None,
    base_url: str | None = None,
    rename: Mapping[str, str] | None = None,
    skip: Iterable[str] = (),
    include_deprecated: bool = False,
    lenient: bool = False,
    transform: Transform | None = None,
) -> list[Path]:
    """Generate (or regenerate) a client package.

    Args:
        spec: Path to the OpenAPI document (YAML or JSON).
        output: The package directory to write, such as `src/my_sdk/_generated`.
        name: Class name of the sync client (the async one is `Async<name>`); defaults
            to one derived from the spec's title.
        base_url: Default base URL, overriding the spec's first server (needed when
            the spec only has a relative server URL).
        rename: Route names to override, keyed by operationId or `"METHOD /path"`.
        skip: Operations whose route and parameter model you write by hand
            (operationIds or `"METHOD /path"`). Their request and response models are
            still generated, named after the (renamed) route.
        include_deprecated: Also generate deprecated operations.
        lenient: Make every model field optional (`X | None = None`), for APIs whose
            responses leave out or null fields their spec says are required.
        transform: Fixes up the spec dict before generation (for broken specs).

    Returns:
        The files written.

    Raises:
        SpecError: If the spec can't be turned into a client.
    """
    out = Path(output)
    files = _build(
        Path(spec), out, name, base_url, rename, skip, include_deprecated, lenient, transform
    )
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, text in files.items():
        target = out / filename
        target.write_text(text)
        written.append(target)
    return written


def check(
    spec: str | Path,
    output: str | Path,
    *,
    name: str | None = None,
    base_url: str | None = None,
    rename: Mapping[str, str] | None = None,
    skip: Iterable[str] = (),
    include_deprecated: bool = False,
    lenient: bool = False,
    transform: Transform | None = None,
) -> list[Path]:
    """Report generated files that are missing or differ from a fresh generation.

    Takes the same arguments as [`generate`][nanosdk.codegen.generate]; writes nothing.

    Returns:
        The stale files; empty when the package is up to date.
    """
    out = Path(output)
    files = _build(
        Path(spec), out, name, base_url, rename, skip, include_deprecated, lenient, transform
    )
    return [
        out / filename
        for filename, text in files.items()
        if not (out / filename).exists() or (out / filename).read_text() != text
    ]


# --- implementation -----------------------------------------------------------------------


def _build(
    spec_path: Path,
    out: Path,
    name: str | None,
    base_url: str | None,
    rename: Mapping[str, str] | None,
    skip: Iterable[str],
    include_deprecated: bool,
    lenient: bool,
    transform: Transform | None,
) -> dict[str, str]:
    from ..client import AsyncClient, Client
    from ._render import render_client, render_init

    spec = load_spec(spec_path)
    if transform is not None:
        spec = transform(spec)
    reserved = set(dir(Client)) | set(dir(AsyncClient))  # names a route mustn't shadow
    api = Reader(
        spec, rename=rename, skip=skip, include_deprecated=include_deprecated, reserved=reserved
    ).read()
    client_name = name or _default_name(api)
    url = base_url if base_url is not None else _default_url(api)

    models = _datamodel_codegen(api, out, spec_path.name, lenient=lenient)
    missing = api.model_names - _class_names(models)
    if missing:
        raise SpecError(
            f"datamodel-codegen didn't produce {sorted(missing)}; rename those schemas "
            "(they may clash with Python or pydantic names)"
        )
    client = render_client(api, name=client_name, base_url=url, spec_name=spec_path.name)
    init = render_init(api, name=client_name, spec_name=spec_path.name)
    return {
        "__init__.py": _ruff_format(init, out / "__init__.py"),
        "client.py": _ruff_format(client, out / "client.py"),
        "models.py": models,
    }


def _datamodel_codegen(api: Api, out: Path, spec_name: str, *, lenient: bool) -> str:
    """Run datamodel-codegen on the prepared spec and return the models source.

    It runs in a scratch directory next to `out`, so the ruff config it formats with
    is the one that applies to the real output.
    """
    staging = out.parent / f".{out.name}.nanosdk-tmp"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        spec_file = staging / "spec.json"
        spec_file.write_text(json.dumps(api.spec, default=str))
        target = staging / "models.py"
        header = (
            f"# Generated by datamodel-codegen via nanosdk from {spec_name}; don't edit by hand."
        )
        command = [
            *_tool("datamodel-codegen", "datamodel_code_generator"),
            "--input", str(spec_file),
            "--output", str(target),
            "--custom-file-header", header,
            *DATAMODEL_CODEGEN_ARGS,
            *(["--force-optional"] if lenient else []),
        ]  # fmt: skip
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise SpecError(f"datamodel-codegen failed:\n{result.stderr.strip()}")
        return target.read_text()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _ruff_format(source: str, path: Path) -> str:
    """Format with ruff, using the config that applies at `path`."""
    command = [*_tool("ruff", "ruff"), "format", "--stdin-filename", str(path), "-"]
    result = subprocess.run(command, input=source, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SpecError(f"generated code didn't parse (a nanosdk bug):\n{result.stderr}")
    return result.stdout


def _tool(script: str, module: str) -> list[str]:
    """Run a codegen dependency from this interpreter's environment."""
    found = shutil.which(script, path=str(Path(sys.executable).parent))
    if found:
        return [found]
    return [sys.executable, "-m", module]


def _class_names(source: str) -> set[str]:
    return {n.name for n in ast.parse(source).body if isinstance(n, ast.ClassDef)}


def _default_name(api: Api) -> str:
    words = [w for w in pascal(api.title).replace("API", "Api").split() if w]
    return words[0] if words else "Api"


def _default_url(api: Api) -> str:
    for url in api.servers:
        if url.startswith(("http://", "https://")):
            return url.rstrip("/")
    return ""
