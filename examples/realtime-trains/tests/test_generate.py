"""The spec normalizer, and guards that generated files match the vendored spec."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

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


def test_normalized_spec_is_up_to_date() -> None:
    """Fails if spec/rtt-api.yml changed without `uv run scripts/generate.py`."""
    upstream = yaml.safe_load((ROOT / "spec" / "rtt-api.yml").read_text())
    expected = json.loads(json.dumps(generate.normalize(upstream), default=str))
    assert json.loads((ROOT / "spec" / "rtt-api.normalized.json").read_text()) == expected


def test_models_are_up_to_date() -> None:
    """Fails if models.py was edited by hand or not regenerated after a spec change."""
    codegen = shutil.which("datamodel-codegen")
    if codegen is None:
        pytest.skip("datamodel-codegen not installed (uv sync)")
    committed = ROOT / "src" / "realtime_trains" / "models.py"
    fresh = committed.with_name("_models_check.py")  # same dir: same ruff config for formatting
    try:
        subprocess.run([codegen, "--output", str(fresh)], cwd=ROOT, check=True)
        assert fresh.read_text() == committed.read_text()
    finally:
        fresh.unlink(missing_ok=True)
