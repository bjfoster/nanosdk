"""Read an OpenAPI 3.x document into the operations and types the renderer needs.

Inline schemas that need a class (request/response objects, inline enums used as
bodies, ...) are hoisted into `components/schemas` under names chosen here, and
component names are made valid Python identifiers, so the class names the client
refers to are known before datamodel-codegen runs.
"""

# This module walks untyped JSON (`Any` everywhere), so "partially unknown" is noise here.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false

from __future__ import annotations

import keyword
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import BaseModel

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


class SpecError(ValueError):
    """The OpenAPI document can't be turned into a client."""


# --- naming ----------------------------------------------------------------------------


def words(name: str) -> list[str]:
    """Split `getPetById`, `pet-id`, `X_Rate.Limit`, `listAPIs` into lower-case words."""
    name = re.sub(r"([A-Z]{2,})s(?![a-z])", lambda m: m[1].capitalize() + "s", name)  # APIs
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def snake(name: str) -> str:
    """A valid snake_case identifier for `name`."""
    ident = "_".join(words(name)) or "value"
    if ident[0].isdigit():
        ident = f"n{ident}"
    return f"{ident}_" if keyword.iskeyword(ident) or keyword.issoftkeyword(ident) else ident


# Field names that would shadow a name the generated annotations use, or a BaseModel
# attribute (`date: date | None` breaks the class body; `json` shadows BaseModel.json).
# Other builtins (`id`, `filter`, ...) are fine as field names.
SHADOWED = frozenset(
    {"date", "datetime", "Any", "Literal", "Annotated", "Field", "BaseModel", "nanosdk"}
    | {"str", "int", "float", "bool", "bytes", "list", "dict", "object", "type", "None"}
    | {n for n in dir(BaseModel) if not n.startswith("_")}
)


def field_name(wire: str) -> str:
    """A request-model field name for wire name `wire` (aliases keep the wire name)."""
    name = snake(wire)
    return f"{name}_" if name in SHADOWED else name


def pascal(name: str) -> str:
    """A valid PascalCase identifier for `name`."""
    ident = "".join(w[:1].upper() + w[1:] for w in words(name)) or "Model"
    return f"Model{ident}" if ident[0].isdigit() else ident


# --- the extracted API ------------------------------------------------------------------


@dataclass
class Param:
    """One request-model field."""

    name: str  # python name
    wire: str  # name on the wire
    location: str  # path | query | header | body | form | file
    type: str  # python type expression
    required: bool
    description: str | None = None
    constraints: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass
class Operation:
    """One route to generate."""

    name: str
    method: str
    path: str  # with placeholders renamed to python names
    operation_id: str
    tag: str
    summary: str | None
    deprecated: bool
    params: list[Param]
    request_model: str | None  # a generated model used directly as the request
    response: str | None  # python type expression, None for no body
    error_schema: str | None  # model name of the error response, if any


@dataclass
class Api:
    """Everything the renderer needs."""

    title: str
    version: str
    description: str | None
    servers: list[str]
    tags: dict[str, str | None]  # name -> description, in spec order
    operations: list[Operation]
    error_model: str | None
    spec: dict[str, Any]  # with hoisted / renamed schemas, for datamodel-codegen
    model_names: set[str]  # component classes referenced by the client
    imports: set[str]  # names needed from typing / datetime


class Reader:
    """Turns a spec dict into an `Api`."""

    def __init__(
        self,
        spec: dict[str, Any],
        *,
        rename: Mapping[str, str] | None = None,
        skip: Iterable[str] = (),
        include_deprecated: bool = False,
        reserved: set[str] | None = None,
    ) -> None:
        version = str(spec.get("openapi", ""))
        if not version.startswith("3."):
            raise SpecError(f"only OpenAPI 3.x is supported (got openapi={version or '?'})")
        self.spec = spec
        self.rename = dict(rename or {})
        self.skip = set(skip)
        self.include_deprecated = include_deprecated
        self.reserved = reserved or set()
        self.schemas: dict[str, Any] = spec.setdefault("components", {}).setdefault("schemas", {})
        self.used_models: set[str] = set()
        self.imports: set[str] = set()
        self._rename_components()
        self._nullable_to_null_type()
        self._drop_noop_constraints()
        self._hoist_property_named_properties()
        self._name_union_branches()

    # -- refs and components ---------------------------------------------------------

    def _rename_components(self) -> None:
        """Make every component schema name a unique Python class name."""
        mapping: dict[str, str] = {}
        taken: set[str] = set()
        for old in list(self.schemas):
            new = pascal(old)
            while new in taken:
                new += "_"
            taken.add(new)
            mapping[old] = new
        self.schemas = {mapping[k]: v for k, v in self.schemas.items()}
        self.spec["components"]["schemas"] = self.schemas
        refs = {
            f"#/components/schemas/{o}": f"#/components/schemas/{n}" for o, n in mapping.items()
        }
        for node in _dicts(self.spec):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref in refs:
                node["$ref"] = refs[ref]
            tags: Any = node.get("mapping")  # discriminator mappings are refs too
            if isinstance(tags, dict) and isinstance(node.get("propertyName"), str):
                node["mapping"] = {
                    tag: refs.get(str(target), target)
                    for tag, target in cast(dict[str, Any], tags).items()
                }

    def _nullable_to_null_type(self) -> None:
        """Rewrite OpenAPI 3.0 `nullable: true` as a JSON Schema null type.

        datamodel-codegen ignores `nullable` on required fields, so GitHub's
        `language: {type: string, nullable: true}` would reject the `null`s the API sends.
        `type: [string, "null"]` / `anyOf: [..., {type: "null"}]` it handles.
        """
        for node in list(_dicts(self.spec)):
            if node.get("nullable") is not True:
                continue
            del node["nullable"]
            kind = node.get("type")
            if isinstance(kind, str):
                node["type"] = [kind, "null"]
            elif isinstance(kind, list):
                if "null" not in kind:
                    kind.append("null")
            elif node:  # $ref / allOf / oneOf / ...: wrap the whole schema
                inner = dict(node)
                node.clear()
                node["anyOf"] = [inner, {"type": "null"}]

    def _drop_noop_constraints(self) -> None:
        """Drop `minLength: 0` / `minItems: 0` / `minProperties: 0`.

        They constrain nothing, but make datamodel-codegen wrap the value in a root
        model (`list[Datum]` instead of `list[str]`).
        """
        for node in _dicts(self.spec):
            for key in ("minLength", "minItems", "minProperties"):
                if node.get(key) == 0:
                    del node[key]

    def _hoist_property_named_properties(self) -> None:
        """Work around a datamodel-codegen crash on GeoJSON-style schemas.

        An inline object with a property literally named `properties` (GeoJSON's
        `Feature.properties`), nested in arrays and `allOf`s, makes datamodel-codegen 0.86
        misread the property map as a schema. Moving such objects into named components
        avoids it, and gives the classes better names.
        """
        hoisted: dict[str, Any] = {}

        def walk(node: Any, hint: str, root: bool) -> Any:
            if not isinstance(node, dict):
                return node
            schema = cast(dict[str, Any], node)
            props = schema.get("properties")
            if isinstance(props, dict):
                schema["properties"] = {
                    k: walk(v, hint + pascal(k), False) for k, v in props.items()
                }
            for key in ("allOf", "oneOf", "anyOf"):
                if isinstance(schema.get(key), list):
                    schema[key] = [walk(v, hint, root) for v in schema[key]]
            if isinstance(schema.get("items"), dict):
                schema["items"] = walk(schema["items"], hint + "Item", False)
            if not root and isinstance(props, dict) and "properties" in props:
                name = hint
                while name in self.schemas or name in hoisted:
                    name += "_"
                hoisted[name] = schema
                return {"$ref": f"#/components/schemas/{name}"}
            return schema

        for name in list(self.schemas):
            self.schemas[name] = walk(self.schemas[name], name, True)
        self.schemas.update(hoisted)

    def _name_union_branches(self) -> None:
        """Give every inline object branch of a union component its own component.

        datamodel-codegen collapses a union component referenced by other schemas into
        `A | B`, and names inline branches unpredictably. With every branch a `$ref`,
        the generator can spell the union out itself (see `type_of`).
        """
        for name in list(self.schemas):
            schema = self.schemas[name]
            if not _is_union(schema):
                continue
            key = "oneOf" if "oneOf" in schema else "anyOf"
            branches: list[Any] = []
            for i, branch in enumerate(cast(list[Any], schema[key]), 1):
                if isinstance(branch, dict) and "$ref" not in branch and self.needs_class(branch):
                    title = cast(dict[str, Any], branch).get("title")
                    branch_name = f"{name}{pascal(str(title))}" if title else f"{name}Option{i}"
                    while branch_name in self.schemas:
                        branch_name += "_"
                    self.schemas[branch_name] = branch
                    branch = {"$ref": f"#/components/schemas/{branch_name}"}
                branches.append(branch)
            schema[key] = branches

    def resolve(self, node: Any) -> Any:
        """Follow local `$ref`s to the referenced object."""
        current: Any = node
        seen: set[str] = set()
        while isinstance(current, dict) and "$ref" in current:
            ref = cast(dict[str, Any], current)["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise SpecError(f"only local $refs are supported, got {ref!r}")
            if ref in seen:
                raise SpecError(f"circular $ref {ref!r}")
            seen.add(ref)
            current = self.spec
            for part in ref[2:].split("/"):
                current = cast(dict[str, Any], current)[part.replace("~1", "/").replace("~0", "~")]
        return cast(Any, current)

    def needs_class(self, schema: Any, depth: int = 0) -> bool:
        """Objects with fields, `allOf`s, and unions containing an object get a class."""
        if not isinstance(schema, dict) or depth > 8:
            return False
        node = cast(dict[str, Any], schema)
        if "$ref" in node:
            return self.needs_class(self.resolve(node), depth + 1)
        if "properties" in node or "allOf" in node:
            return True
        union = node.get("oneOf") or node.get("anyOf")
        if isinstance(union, list):
            return any(self.needs_class(b, depth + 1) or _is_object(self.resolve(b)) for b in union)
        return False

    def _hoist(self, schema: dict[str, Any], hint: str) -> str:
        name = pascal(hint)
        base, n = name, 2
        while name in self.schemas:
            name, n = f"{base}{n}", n + 1
        self.schemas[name] = schema
        self.used_models.add(name)
        return name

    # -- types -------------------------------------------------------------------------

    def type_of(self, schema: Any, hint: str, *, inline_enums: bool = True) -> str:
        """A Python type expression for `schema`, hoisting objects into components."""
        if not isinstance(schema, dict) or not schema:
            self.imports.add("Any")
            return "Any"
        node = cast(dict[str, Any], schema)
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[1]
            target = self.resolve(node)
            if self.needs_class(target) and not _is_union(target):
                self.used_models.add(name)
                return name
            return self.type_of(target, name, inline_enums=inline_enums)
        if "$ref" in node:
            return self.type_of(self.resolve(node), hint, inline_enums=inline_enums)

        nullable = bool(node.get("nullable"))
        kinds = node.get("type")
        if isinstance(kinds, list):
            listed: list[Any] = kinds
            types = [k for k in listed if k != "null"]
            nullable = nullable or "null" in listed
            kinds = types[0] if len(types) == 1 else None
            if kinds is None and types:
                expr = " | ".join(self.type_of({**node, "type": k}, hint) for k in types)
                return f"{expr} | None" if nullable else expr

        union = node.get("oneOf") or node.get("anyOf")
        if isinstance(union, list) and (_is_union(node) or not self.needs_class(node)):
            branches = [self.type_of(b, hint, inline_enums=inline_enums) for b in union]
            expr = " | ".join(dict.fromkeys(b for b in branches if b != "None")) or "None"
            nullable = nullable or "None" in branches
            return f"{expr} | None" if nullable and expr != "None" else expr

        if "enum" in node and inline_enums and kinds in (None, "string", "integer"):
            values = [v for v in cast(list[Any], node["enum"]) if v is not None]
            if values:
                self.imports.add("Literal")
                expr = f"Literal[{', '.join(repr(v) for v in values)}]"
                return f"{expr} | None" if nullable else expr
        if self.needs_class(node):
            expr = self._hoist(node, hint)
        elif kinds == "array" or "items" in node:
            expr = f"list[{self.type_of(node.get('items'), f'{hint}Item')}]"
        elif kinds == "object" or "additionalProperties" in node:
            extra = node.get("additionalProperties")
            value = self.type_of(extra, f"{hint}Value") if isinstance(extra, dict) else "Any"
            if value == "Any":
                self.imports.add("Any")
            expr = f"dict[str, {value}]"
        elif kinds == "string":
            fmt = str(node.get("format"))
            expr = {"date": "date", "date-time": "datetime", "binary": "bytes"}.get(fmt, "str")
            if expr in ("date", "datetime"):
                self.imports.add(expr)
        else:
            expr = {"integer": "int", "number": "float", "boolean": "bool", "null": "None"}.get(
                str(kinds), ""
            )
            if not expr:
                self.imports.add("Any")
                expr = "Any"
        return f"{expr} | None" if nullable and expr != "None" else expr

    def response_type(self, schema: Any, hint: str) -> str:
        """Like `type_of`, but always usable as `response=` (a class, list or dict)."""
        expr = self.type_of(schema, hint, inline_enums=False)
        if "|" in expr or expr.startswith("Literal") or expr == "Any":
            if expr == "Any":
                if _is_object(self.resolve(schema)):
                    self.imports.add("Any")
                    return "dict[str, Any]"
                return "object"  # no schema: the API promises nothing; narrow it yourself
            resolved: dict[str, Any] = self.resolve(schema) if isinstance(schema, dict) else {}
            return self._hoist(resolved, hint)
        return expr

    # -- operations ----------------------------------------------------------------------

    def read(self) -> Api:
        """Extract every operation, then the client-level details."""
        info = cast(dict[str, Any], self.spec.get("info") or {})
        operations = list(self._operations())
        errors = [op.error_schema for op in operations if op.error_schema]
        error_model = max(set(errors), key=errors.count) if errors else None
        spec_tags: list[dict[str, Any]] = self.spec.get("tags") or []
        tags: dict[str, str | None] = {str(t["name"]): t.get("description") for t in spec_tags}
        for op in operations:
            tags.setdefault(op.tag, None)
        used = {op.tag for op in operations}
        servers: list[dict[str, Any]] = self.spec.get("servers") or []
        return Api(
            title=str(info.get("title") or "API"),
            version=str(info.get("version") or ""),
            description=info.get("description"),
            servers=[str(s["url"]) for s in servers if "url" in s],
            tags={k: v for k, v in tags.items() if k in used},
            operations=operations,
            error_model=error_model,
            spec=self.spec,
            model_names=self.used_models,
            imports=self.imports,
        )

    def _operations(self) -> Iterator[Operation]:
        names: dict[str, str] = {}
        for path, item in cast(dict[str, Any], self.spec.get("paths") or {}).items():
            item = cast(dict[str, Any], self.resolve(item))
            shared = cast(list[Any], item.get("parameters") or [])
            for method in HTTP_METHODS:
                if method not in item:
                    continue
                op = cast(dict[str, Any], item[method])
                if op.get("deprecated") and not self.include_deprecated:
                    continue
                operation = self._operation(path, method, op, shared)
                if {op.get("operationId"), f"{method.upper()} {path}"} & self.skip:
                    continue  # implemented by hand; its body models are still generated
                if operation.name in names:
                    raise SpecError(
                        f"{method.upper()} {path} and {names[operation.name]} both become route "
                        f"{operation.name!r}; give one a different name with `rename`"
                    )
                names[operation.name] = f"{method.upper()} {path}"
                yield operation

    def _route_name(self, method: str, path: str, operation_id: str | None) -> str:
        for key in (f"{method.upper()} {path}", operation_id):
            if key and key in self.rename:
                return self.rename[key]
        if operation_id:
            name = snake(operation_id)
        else:
            segments = [s for s in path.split("/") if s]
            parts = [method]
            for i, seg in enumerate(segments):
                if seg.startswith("{"):
                    if i == len(segments) - 1:
                        parts += ["by", seg.strip("{}")]
                else:
                    parts.append(seg)
            name = snake("_".join(parts))
        return f"{name}_" if name in self.reserved else name

    def _operation(
        self, path: str, method: str, op: dict[str, Any], shared: list[Any]
    ) -> Operation:
        name = self._route_name(method, path, op.get("operationId"))
        hint = pascal(name)
        params = self._params(op, shared, hint)
        renamed = {p.wire: p.name for p in params if p.location == "path"}
        py_path = re.sub(r"{([^}]+)}", lambda m: "{" + renamed.get(m[1], snake(m[1])) + "}", path)

        request_model: str | None = None
        body = op.get("requestBody")
        if body is not None:
            body_params, model = self._body(cast(dict[str, Any], self.resolve(body)), hint, params)
            params += body_params
            request_model = model

        tags = cast(list[Any], op.get("tags") or ["default"])
        return Operation(
            name=name,
            method=method,
            path=py_path,
            operation_id=str(op.get("operationId") or name),
            tag=str(tags[0]),
            summary=op.get("summary") or _first_line(op.get("description")),
            deprecated=bool(op.get("deprecated")),
            params=params,
            request_model=request_model,
            response=self._success(op, hint),
            error_schema=self._error(op),
        )

    def _params(self, op: dict[str, Any], shared: list[Any], hint: str) -> list[Param]:
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in [*shared, *cast(list[Any], op.get("parameters") or [])]:
            p = cast(dict[str, Any], self.resolve(raw))
            merged[(str(p["name"]), str(p["in"]))] = p
        params: list[Param] = []
        taken: set[str] = set()
        for (wire, where), p in merged.items():
            if where == "cookie":
                continue  # not supported by nanosdk's field markers; set cookies on the client
            name = field_name(wire)
            if name in taken:
                name = f"{name}_{where}"
            taken.add(name)
            schema = cast(dict[str, Any], p.get("schema") or {})
            params.append(
                Param(
                    name=name,
                    wire=wire,
                    location=where,
                    type=self.type_of(schema, f"{hint}{pascal(wire)}"),
                    required=bool(p.get("required")) or where == "path",
                    description=_clean(p.get("description") or schema.get("description")),
                    constraints=_constraints(cast(dict[str, Any], self.resolve(schema))),
                )
            )
        return params

    def _body(
        self, body: dict[str, Any], hint: str, params: list[Param]
    ) -> tuple[list[Param], str | None]:
        content = cast(dict[str, Any], body.get("content") or {})
        required = bool(body.get("required"))
        json_type = _json_type(content)
        if json_type is not None:
            schema = cast(dict[str, Any], content[json_type]).get("schema")
            expr = self.response_type(schema, f"{hint}Request")
            if not params and expr in self.schemas and _is_object(self.schemas[expr]):
                return [], expr  # the model *is* the request: `add_pet(name=...)`
            return [Param("body", "body", "body", expr, required)], None
        form_type = next((t for t in content if t in FORM_TYPES), None)
        if form_type is not None:
            schema = cast(dict[str, Any], self.resolve(content[form_type].get("schema") or {}))
            needed = set(cast(list[Any], schema.get("required") or []))
            fields: list[Param] = []
            for wire, prop in cast(dict[str, Any], schema.get("properties") or {}).items():
                prop = cast(dict[str, Any], self.resolve(prop))
                binary = prop.get("format") in ("binary", "base64") or prop.get("type") == "file"
                fields.append(
                    Param(
                        name=field_name(wire),
                        wire=wire,
                        location="file" if binary else "form",
                        type="Any" if binary else self.type_of(prop, f"{hint}{pascal(wire)}"),
                        required=wire in needed,
                        description=_clean(prop.get("description")),
                    )
                )
                if binary:
                    self.imports.add("Any")
            return fields, None
        if content:
            kind = next(iter(content))
            expr = "str" if kind.startswith("text/") else "bytes"
            return [Param("body", "body", "body", expr, required)], None
        return [], None

    def _success(self, op: dict[str, Any], hint: str) -> str | None:
        responses = {str(k): v for k, v in cast(dict[Any, Any], op.get("responses") or {}).items()}
        codes = sorted(c for c in responses if c.startswith("2"))
        for code in codes:
            response = cast(dict[str, Any], self.resolve(responses[code]))
            content = cast(dict[str, Any], response.get("content") or {})
            if not content:
                continue
            kind = _json_type(content)
            if kind is not None:
                schema = cast(dict[str, Any], content[kind]).get("schema")
                return self.response_type(schema, f"{hint}Response")
            first = next(iter(content))
            return "str" if first.startswith("text/") else "bytes"
        return None

    def _error(self, op: dict[str, Any]) -> str | None:
        for code, raw in cast(dict[Any, Any], op.get("responses") or {}).items():
            if not (str(code)[0] in "45" or str(code) == "default"):
                continue
            content = cast(dict[str, Any], self.resolve(raw).get("content") or {})
            schema: Any = cast(dict[str, Any], content.get("application/json") or {}).get("schema")
            ref = cast(dict[str, Any], schema).get("$ref") if isinstance(schema, dict) else None
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[1]
                if self.needs_class(self.resolve(schema)):
                    self.used_models.add(name)
                    return name
        return None


# --- helpers ---------------------------------------------------------------------------


def _dicts(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        mapping = cast(dict[str, Any], node)
        yield mapping
        for value in mapping.values():
            yield from _dicts(value)
    elif isinstance(node, list):
        items: list[Any] = node
        for value in items:
            yield from _dicts(value)


def _is_union(schema: Any) -> bool:
    """A bare oneOf/anyOf (no own properties): datamodel-codegen makes it a root model."""
    if not isinstance(schema, dict):
        return False
    node = cast(dict[str, Any], schema)
    return isinstance(node.get("oneOf") or node.get("anyOf"), list) and not (
        "properties" in node or "allOf" in node
    )


def _json_type(content: dict[str, Any]) -> str | None:
    return next((t for t in content if t == "application/json" or t.endswith("+json")), None)


def _is_object(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    node = cast(dict[str, Any], schema)
    return (
        "properties" in node
        or "allOf" in node
        or (node.get("type") == "object" and ("additionalProperties" not in node))
    )


def _constraints(schema: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "pattern": "pattern",
        "minimum": "ge",
        "maximum": "le",
        "exclusiveMinimum": "gt",
        "exclusiveMaximum": "lt",
        "minLength": "min_length",
        "maxLength": "max_length",
    }
    out: dict[str, Any] = {}
    for key, arg in keys.items():
        value = schema.get(key)
        if value is not None and not isinstance(value, bool):
            out[arg] = value
    return out


def _clean(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    return " ".join(text.split()) or None


def _first_line(text: Any) -> str | None:
    cleaned = _clean(text)
    return cleaned.split(". ")[0] if cleaned else None


Transform = Callable[[dict[str, Any]], dict[str, Any]]
"""A function that fixes up a spec dict before generation."""
