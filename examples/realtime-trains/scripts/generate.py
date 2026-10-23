"""Regenerate ``src/realtime_trains/_generated`` from the Realtime Trains OpenAPI spec.

    uv run scripts/generate.py           # nanoapi generate, with the settings in pyproject
    uv run scripts/generate.py --fetch   # download the latest upstream spec first
    uv run scripts/generate.py --check   # CI: fail if _generated/ is out of date

``spec/rtt-api.yml`` is the upstream spec, byte for byte. It has a few constructs that
aren't valid JSON Schema; ``normalize`` (below) corrects them in memory, as the
generator's ``transform``.
"""

import argparse
import os
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

from nanoapi.codegen import cli

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_URL = "https://realtimetrains.github.io/api-specification/specification/main.yml"
UPSTREAM = ROOT / "spec" / "rtt-api.yml"


def normalize(node: Any) -> Any:
    """Return a copy of a spec node with the upstream quirks corrected.

    - ``null: true`` (a YAML key that parses as ``None``) is meant as "nullable":
      add ``"null"`` to the node's type.
    - ``type: null`` on ``uniqueIdentity`` should be ``string``, as its examples show.
    - ``oneOf: [{type: string}, {type: "null"}]`` next to an ``enum`` makes codegen emit
      two copies of the enum. Make it a plain string enum: every field using these
      enums is optional, so the generated field type (``Enum | None``) still allows null.
    - ``format: date`` / ``date-time`` whose own ``example`` isn't one (the headcode
      ``trainReportingIdentity: "1L40"`` is marked as a date): drop the format, or every
      response containing it would fail validation.
    """
    if isinstance(node, list):
        return [normalize(item) for item in cast(list[Any], node)]
    if not isinstance(node, dict):
        return node
    source = cast(dict[Any, Any], node)
    out: dict[str, Any] = {k: normalize(v) for k, v in source.items() if isinstance(k, str)}

    if source.get(None) is True:
        kind = out.get("type", "string")
        out["type"] = [kind, "null"] if isinstance(kind, str) else [*kind, "null"]

    if "type" in source and source["type"] is None:
        out["type"] = "string"

    if "enum" in out and out.get("oneOf") == [{"type": "string"}, {"type": "null"}]:
        del out["oneOf"]
        out["type"] = "string"

    if out.get("format") in ("date", "date-time") and not _parses_as(out["format"], out):
        del out["format"]
    return out


def _parses_as(fmt: str, schema: dict[str, Any]) -> bool:
    """Whether the schema's example (if any) is a valid ``date`` / ``date-time``."""
    example = schema.get("example")
    if not isinstance(example, str):
        return True  # no string example to contradict the format
    parse = date.fromisoformat if fmt == "date" else datetime.fromisoformat
    try:
        parse(example)
    except ValueError:
        return False
    return True


def main() -> None:
    """Optionally fetch the upstream spec, then run `nanoapi generate`."""
    parser = argparse.ArgumentParser(description="Regenerate the client from the spec.")
    parser.add_argument("--fetch", action="store_true", help="download the upstream spec first")
    parser.add_argument("--check", action="store_true", help="fail if the output is stale")
    args = parser.parse_args()

    if args.fetch:
        with urllib.request.urlopen(UPSTREAM_URL) as response:
            UPSTREAM.write_bytes(response.read())
        print(f"fetched {UPSTREAM_URL}")
    # Settings, including `transform = "scripts/generate.py:normalize"`, are in pyproject.toml.
    os.chdir(ROOT)
    cli.main(["generate", *(["--check"] if args.check else [])])


if __name__ == "__main__":
    main()
