# Versioning and stability

nanosdk follows [Semantic Versioning](https://semver.org/), with the usual rule for
versions before 1.0: the API can still change, but never in a patch release.

| Release | Example | What it can contain |
|---|---|---|
| Patch | `0.1.0` → `0.1.1` | Bug fixes and new features; existing code keeps working |
| Minor | `0.1.x` → `0.2.0` | Breaking changes, listed under **⚠ BREAKING CHANGES** in the [changelog](changelog.md) |

So pin to the minor version, for example `nanosdk>=0.1.1,<0.2`, and read the changelog
before moving to the next one. From 1.0 on, breaking changes need a major release.

## What counts as public API

- Everything importable from the top-level `nanosdk` package, plus the `nanosdk.auth`,
  `nanosdk.frames` and `nanosdk.codegen` modules, as documented in the
  [API reference](reference/index.md).
- The documented client hooks, such as
  [`_prepare_request`][nanosdk.Client._prepare_request]. They start with an underscore so
  they stay out of your client's namespace, but they're public.
- The `nanosdk` command-line options.
- The types. If a new release makes code that type-checked before fail under pyright,
  mypy or ty, that's a breaking change too.

Anything else that starts with an underscore, including the modules under
`nanosdk.codegen`, is private and can change in any release.

**Generated code:** `nanosdk generate` may produce different output from one release to
the next, for example a clearer name or a tighter type. That counts as a change to your
code, not to nanosdk's API. Regenerate when you upgrade, and use `nanosdk generate --check`
in CI to see the difference.

## Deprecations

Before something is removed, it goes through at least one release in which it still works
but emits a `DeprecationWarning`, and the changelog says what to use instead. Run your tests
with `-W error::DeprecationWarning` to see deprecations early.

## Supported Python and dependency versions

nanosdk supports the CPython versions that are not end-of-life and are listed in the
package's `requires-python`. Dropping a Python version is announced in the changelog and
happens in a minor release.

The lower bounds on httpx, pydantic and the optional dependencies are tested: CI runs the
test suite with every dependency at its minimum, on the oldest supported Python.
