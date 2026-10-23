"""`nanosdk generate`: the command-line front end of [`nanosdk.codegen`][nanosdk.codegen].

Settings come from the command line, falling back to `[tool.nanosdk.codegen]` in the
`pyproject.toml` of the current directory:

```toml
[tool.nanosdk.codegen]
spec = "spec/openapi.yml"
output = "src/my_sdk/_generated"
name = "MyApi"
base-url = "https://api.example.com/v1"
include-deprecated = false
lenient = false                       # true: every model field optional
transform = "scripts/fix_spec.py:fix"   # or "my_package.module:function"
rename = { getPetById = "get_pet", "GET /store/inventory" = "inventory" }
skip = ["updatePetWithForm"]          # operations you implement by hand
```
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

from . import SpecError, Transform, check, generate

__all__ = ["main"]


def main(argv: list[str] | None = None) -> None:
    """Run `nanosdk generate`."""
    parser = argparse.ArgumentParser(prog="nanosdk", description="nanosdk tools")
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate", help="generate a typed client from an OpenAPI spec")
    gen.add_argument("spec", nargs="?", help="OpenAPI document (YAML or JSON)")
    gen.add_argument("-o", "--output", help="package directory to write")
    gen.add_argument("--name", help="client class name (async client: Async<name>)")
    gen.add_argument("--base-url", help="default base URL (overrides the spec's servers)")
    gen.add_argument(
        "--rename", action="append", default=[], metavar="KEY=NAME",
        help="route name for an operationId or 'METHOD /path' (repeatable)",
    )  # fmt: skip
    gen.add_argument(
        "--skip", action="append", default=[], metavar="OPERATION",
        help="operationId or 'METHOD /path' not to generate (repeatable)",
    )  # fmt: skip
    gen.add_argument("--include-deprecated", action="store_true", default=None)
    gen.add_argument(
        "--lenient", action="store_true", default=None,
        help="make every model field optional (for APIs that break their spec)",
    )  # fmt: skip
    gen.add_argument("--transform", help="'file.py:function' or 'module:function' to fix the spec")
    gen.add_argument("--check", action="store_true", help="fail if the output is out of date")
    args = parser.parse_args(argv)

    root = Path.cwd()
    config = _config(root / "pyproject.toml")
    spec = args.spec or config.get("spec")
    output = args.output or config.get("output")
    if not spec or not output:
        parser.error("need a spec and --output (or [tool.nanosdk.codegen] in pyproject.toml)")
    rename = {str(k): str(v) for k, v in cast(dict[str, Any], config.get("rename", {})).items()}
    for item in cast(list[str], args.rename):
        key, sep, value = item.rpartition("=")
        if not sep:
            parser.error(f"--rename expects KEY=NAME, got {item!r}")
        rename[key] = value
    transform_ref = args.transform or config.get("transform")
    options: dict[str, Any] = {
        "name": args.name or config.get("name"),
        "base_url": args.base_url or config.get("base-url"),
        "rename": rename,
        "skip": [*cast(list[str], config.get("skip", [])), *cast(list[str], args.skip)],
        "include_deprecated": bool(args.include_deprecated or config.get("include-deprecated")),
        "lenient": bool(args.lenient or config.get("lenient")),
        "transform": _load_transform(transform_ref, root) if transform_ref else None,
    }

    try:
        if args.check:
            stale = check(spec, output, **options)
            if stale:
                print("out of date: " + ", ".join(str(p) for p in stale), file=sys.stderr)
                print("run `nanosdk generate` to update", file=sys.stderr)
                raise SystemExit(1)
            print(f"{output} is up to date")
        else:
            for path in generate(spec, output, **options):
                print(f"wrote {path}")
    except SpecError as exc:
        raise SystemExit(f"nanosdk generate: {exc}") from exc


def _config(pyproject: Path) -> dict[str, Any]:
    if not pyproject.exists():
        return {}
    data = tomllib.loads(pyproject.read_text())
    tool = cast(dict[str, Any], data.get("tool", {}))
    return cast(dict[str, Any], cast(dict[str, Any], tool.get("nanosdk", {})).get("codegen", {}))


def _load_transform(ref: str, root: Path) -> Transform:
    target, _, func = ref.rpartition(":")
    if not target or not func:
        raise SystemExit(
            f"--transform expects 'file.py:function' or 'module:function', got {ref!r}"
        )
    if target.endswith(".py"):
        spec = importlib.util.spec_from_file_location("nanosdk_transform", root / target)
        if spec is None or spec.loader is None:
            raise SystemExit(f"can't load {target}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    return cast(Transform, getattr(module, func))


if __name__ == "__main__":
    main()
