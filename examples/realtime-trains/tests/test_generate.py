"""The spec normalizer, and a guard that _generated/ matches the vendored spec."""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from nanoapi.codegen import cli

ROOT = Path(__file__).resolve().parent.parent


def load_generate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate", ROOT / "scripts" / "generate.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generate = load_generate()


def normalize(yaml_text: str) -> Any:
    return generate.normalize(yaml.safe_load(yaml_text))


def test_null_true_means_nullable() -> None:
    assert normalize("type: integer\nnull: true") == {"type": ["integer", "null"]}


def test_type_null_becomes_string() -> None:
    assert normalize("type: null\nexample: gb-nr:CLJ") == {"type": "string", "example": "gb-nr:CLJ"}


def test_nullable_enum_becomes_plain_string_enum() -> None:
    spec = "oneOf: [{type: string}, {type: 'null'}]\nenum: [CALL, PASS]"
    assert normalize(spec) == {"type": "string", "enum": ["CALL", "PASS"]}


def test_date_format_contradicted_by_its_example_is_dropped() -> None:
    assert normalize("type: string\nformat: date\nexample: '1L40'") == {
        "type": "string",
        "example": "1L40",
    }
    assert normalize("type: string\nformat: date\nexample: '2025-10-26'")["format"] == "date"


def test_generated_package_is_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails if _generated/ was edited by hand, or the spec, normalizer or codegen settings
    changed without `uv run scripts/generate.py`."""
    monkeypatch.chdir(ROOT)
    cli.main(["generate", "--check"])
