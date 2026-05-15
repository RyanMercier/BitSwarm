"""
Per-language profiles for the BitSwarm coordinator.

Each ``LanguageProfile`` carries the language-specific bits the Phase 2
prompt needs: the idiom for an unimplemented stub body, the local
import / include conventions, the test framework conventions, the
default integration-test filename, and the test command miners should
use to verify their work.

The profile registry is the single source of truth. Adding a new
language is one ``LanguageProfile(...)`` entry plus the matching parser
in ``validator/parsers/`` (which is already in place for the seven
languages we support).

``profile_for(...)`` resolves a language identifier (env var, repo
detection, or explicit caller argument) to the right profile. Falls
back to the Python profile so an unset coordinator behaves exactly
like the POC.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


# Some users will hand us a path or build-system marker rather than a
# pretty name. Map common shapes to canonical profile names.
_REPO_HINTS = (
    ("Cargo.toml", "rust"),
    ("package.json", "typescript"),
    ("pom.xml", "java"),
    ("build.gradle", "java"),
    ("build.gradle.kts", "java"),
    ("CMakeLists.txt", "cpp"),
    ("Makefile", "cpp"),     # weak hint; checked after package-system files
    ("requirements.txt", "python"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
)


@dataclass(frozen=True)
class LanguageProfile:
    """Per-language metadata fed into the Phase 2 prompt."""

    name: str                                  # canonical id ("python", "cpp", ...)
    display_name: str                          # human label ("Python 3.x", "C++17", ...)
    extensions: tuple[str, ...]
    integration_test_filename: str
    phase2_intro: str
    stub_rules: str
    test_rules: str
    integration_rules: str
    test_command_hint: str                     # what miners should run to verify
    aliases: tuple[str, ...] = ()              # alternative names users might type


# ---- Python ----

_PYTHON = LanguageProfile(
    name="python",
    display_name="Python",
    extensions=(".py",),
    integration_test_filename="tests/test_integration.py",
    phase2_intro="You are writing Python stub files for a parallel implementation project.",
    stub_rules="""\
- Include all class definitions and function signatures with type hints.
- Every function body: ``raise NotImplementedError(f"{self.__class__.__name__}.method_name not implemented")``
  (for free functions, drop the ``self.__class__.__name__`` prefix).
- Include docstrings explaining what each function must do.
- CRITICAL: Always use FULL package paths for imports. If files live inside a
  package directory (e.g. ``mypackage/``), use the full dotted path:
    CORRECT:  ``from mypackage.module import MyClass``
    WRONG:    ``from module import MyClass``     (bare name, runtime error)
    WRONG:    ``import module``                  (bare name, runtime error)
    WRONG:    ``from .module import MyClass``    (relative import, avoid)
- Import from shared files and the standard library only (no cross-subtask
  imports).
- Objects that pass between subtasks must carry every field both sides need.
  If subtask A produces an object that subtask B consumes, the shared type
  definition must include all those fields.""",
    test_rules="""\
- Tests MUST FAIL when run against stubs. This verifies the stubs are real.
- Import from the corresponding stub module (the subtask being tested).
- Each test calls a stub function and asserts something about the RETURN VALUE.
- DO NOT use ``pytest.raises(NotImplementedError)`` -- that makes the test
  PASS on stubs, which is wrong.
- DO NOT write tests that only import or check class existence.
- CORRECT pattern: ``result = vec.dot(other); assert result == 6.0``.
- WRONG pattern:   ``with pytest.raises(NotImplementedError): vec.dot(other)``.
- At least 3 meaningful tests per subtask, each calling real functions and
  asserting real return values.
- If your test needs an object from ANOTHER subtask, mock it with
  ``unittest.mock.MagicMock()`` rather than importing the real class.""",
    integration_rules="""\
- Test that implementations from different subtasks work together.
- Import every module explicitly (``import numpy as np`` etc.).
- Use ``@pytest.mark.xfail(raises=NotImplementedError, strict=False)`` on
  each test so integration tests are allowed to fail during scaffolding.
- If the repo has JSON config/data files, use those exact field names.""",
    test_command_hint="pytest <test_file> -x --tb=short",
    aliases=("py", "python3"),
)


# ---- TypeScript / JavaScript ----

_TYPESCRIPT = LanguageProfile(
    name="typescript",
    display_name="TypeScript",
    extensions=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    integration_test_filename="tests/test_integration.test.ts",
    phase2_intro="You are writing TypeScript stub files for a parallel implementation project.",
    stub_rules="""\
- Use ES module syntax (``export``, ``import``). Configure files to compile
  under ``tsconfig`` with ``strict`` enabled.
- Every function body throws:
    ``throw new Error("not implemented: <function_or_method_name>");``
- Include type annotations on every parameter and return type. ``any`` is a
  last resort.
- ``import`` paths use the relative form (``./types``, ``../shared/x``) so
  they resolve against the importer's directory. NEVER omit a path entirely.
- A class's constructor parameters must match how callers will construct it
  across the project. Pick a single constructor signature and stick to it.""",
    test_rules="""\
- Use vitest. Tests live alongside source in ``*.test.ts`` files OR under
  ``tests/``; pick one and stay consistent across subtasks.
- Each test imports the stub under test (relative path) and calls a real
  function with concrete arguments, asserting on the return value.
- The stub throws ``Error("not implemented...")`` so the assertion never
  runs -- that's the desired "test fails on stub" behaviour. Do NOT wrap
  the call in ``expect(() => ...).toThrow(...)``; that would make the test
  pass against a stub.
- Mock cross-subtask dependencies with ``vi.fn()`` / a hand-written stub
  object literal -- do NOT import another subtask's real class.""",
    integration_rules="""\
- Integration tests pull from every relevant module via relative imports.
- A short ``describe`` block per cross-subtask interaction.
- Use ``it.skip`` (or omit ``await``) if a stub-only run would block the
  test suite; integration is allowed to fail before all miners finish.""",
    test_command_hint="npx vitest run <test_file>",
    aliases=("ts", "js", "javascript"),
)


# ---- Java ----

_JAVA = LanguageProfile(
    name="java",
    display_name="Java 17",
    extensions=(".java",),
    integration_test_filename="src/test/java/IntegrationTest.java",
    phase2_intro="You are writing Java stub files for a parallel implementation project.",
    stub_rules="""\
- Java 17 with the standard Maven/Gradle source layout:
  ``src/main/java/<pkg>/<Type>.java`` and
  ``src/test/java/<pkg>/<Type>Test.java``. Stick to ONE build system per
  project; do NOT mix.
- Every method body throws
  ``throw new UnsupportedOperationException("not implemented: <name>");``
- Use ``package <pkg>;`` declarations at the top of each file. The package
  must match the directory structure under ``src/main/java``.
- ``import`` statements use fully qualified names. No wildcard imports.
- Constructors: pick ONE signature per class. If optional behaviour is
  needed, use overloaded constructors that delegate via ``this(...)``,
  not two independent bodies.""",
    test_rules="""\
- Use JUnit 5 (``org.junit.jupiter.api.Test``).
- Each test method calls a real method on the stub and asserts on the
  result with ``Assertions.assertEquals(...)`` etc.
- Do NOT wrap the call in ``assertThrows(UnsupportedOperationException.class, ...)``
  -- that would make the test pass against the stub.
- Mock cross-subtask dependencies with ``Mockito.mock(Other.class)``; never
  instantiate the real class from another subtask.""",
    integration_rules="""\
- Integration tests live in ``src/test/java/<pkg>/IntegrationTest.java``.
- Pull from every subtask's classes via fully qualified imports.
- Tests are allowed to fail during the scaffold phase.""",
    test_command_hint="mvn -q test -Dtest=<ClassName>  (or gradle test --tests <ClassName>)",
    aliases=(),
)


# ---- C# ----

_CSHARP = LanguageProfile(
    name="csharp",
    display_name="C# (.NET 8)",
    extensions=(".cs",),
    integration_test_filename="tests/IntegrationTests.cs",
    phase2_intro="You are writing C# stub files for a parallel implementation project.",
    stub_rules="""\
- Target .NET 8 with file-scoped namespaces (``namespace Demo;``).
- Every method body throws
  ``throw new NotImplementedException("not implemented: <name>");``
- ``using`` directives at the top, alphabetical.
- Pick ONE constructor signature per class. If you need optional
  parameters, use C# default-value syntax
  (``public Widget(string name, int port = 8080)``) -- NOT two overloads.""",
    test_rules="""\
- Use xUnit (``[Fact]`` and ``[Theory]``).
- Each test calls a real method and asserts on the return value with
  ``Assert.Equal(expected, actual)``.
- Do NOT wrap calls in ``Assert.Throws<NotImplementedException>(...)`` --
  that would pass against the stub.
- Mock cross-subtask dependencies with Moq (``new Mock<IOther>().Object``);
  never instantiate the real class from another subtask.""",
    integration_rules="""\
- Integration tests live in ``tests/IntegrationTests.cs``.
- Pull from every subtask's namespace.""",
    test_command_hint='dotnet test --filter "FullyQualifiedName~<ClassName>"',
    aliases=("cs", "dotnet"),
)


# ---- C ----

_C = LanguageProfile(
    name="c",
    display_name="C11",
    extensions=(".c", ".h"),
    integration_test_filename="tests/test_integration.c",
    phase2_intro="You are writing C11 stub files for a parallel implementation project.",
    stub_rules="""\
- Header guards (``#ifndef MODULE_H``) on every ``.h``.
- Function declarations live in the ``.h``; definitions live in the
  matching ``.c``. Every stub definition begins with
  ``assert(0 && "not implemented: <function_name>");`` followed by a
  default return (e.g. ``return 0;``) to satisfy the type checker.
- Include the matching header from your ``.c`` (``#include "mymod.h"``).
- For cross-module headers use directory-relative includes
  (``#include "../shared/types.h"`` from ``tests/`` etc.), NOT
  project-rooted paths. BitSwarm's Phase 1.5 import check is
  filesystem-relative.""",
    test_rules="""\
- Plain C, ``int main(void)`` per test file using ``<assert.h>``.
- The Makefile in ``shared_files`` compiles each ``tests/test_*.c`` into
  a binary that links against the full library.
- Each test ``main`` makes real function calls and asserts on the result.
  The stub's ``assert(0)`` will abort the binary on the first call ->
  non-zero exit -> test fails. That is the desired behaviour.
- Mock cross-subtask functions with locally-defined stub functions in
  your own test file -- never include another subtask's ``.h`` if you
  can avoid it.""",
    integration_rules="""\
- ``tests/test_integration.c`` exercises the end-to-end pipeline.
- Include every relevant header. Returns 0 on success, non-zero on
  failure.""",
    test_command_hint="make tests/test_<name> && ./tests/test_<name>",
    aliases=(),
)


# ---- C++ ----

_CPP = LanguageProfile(
    name="cpp",
    display_name="C++17",
    extensions=(".cpp", ".cc", ".cxx", ".hpp", ".hh", ".h"),
    integration_test_filename="tests/test_integration.cpp",
    phase2_intro="You are writing C++17 stub files for a parallel implementation project.",
    stub_rules="""\
- Header guards (``#ifndef MODULE_HPP``) on every ``.hpp``.
- Every function/method body throws
  ``throw std::logic_error("not implemented: <name>");``
- Use the project's single top-level namespace as named in the spec
  (``namespace projectname { ... }``).
- For ``#include``, use filesystem-relative paths from the including
  file's own directory:
    From ``wordle/x.cpp`` or ``wordle/x.hpp``: ``#include "types.hpp"``,
    ``#include "scorer.hpp"`` (siblings, NO prefix).
    From ``tests/test_x.cpp``: ``#include "../wordle/types.hpp"``.
  NEVER use project-rooted paths from inside the package directory.
- Pick ONE constructor signature per class -- no overloads. If a class
  needs optional behaviour, use default arguments
  (``Game(const Words& w, std::string target = "")``), NOT two
  declarations.""",
    test_rules="""\
- Plain C++17, ``int main()`` per test file using ``<cassert>``.
- DO NOT use Catch2, doctest, gtest, or any external framework.
- The Makefile in ``shared_files`` compiles each ``tests/test_*.cpp``
  into a binary. Stubs throwing ``logic_error`` make the binary exit
  non-zero on the first call -- that is the desired "test fails on
  stub" behaviour. Do NOT wrap calls in ``try {} catch (logic_error&) {}``.
- Every subtask gets its own ``tests/test_<sid>.cpp``. EVERY subtask,
  including ``cli`` and other thin wrappers.
- Mock cross-subtask dependencies with a hand-written stub class in
  your test file -- do NOT include another subtask's ``.hpp``.""",
    integration_rules="""\
- ``tests/test_integration.cpp`` with a single ``int main()`` returning
  0 on success, non-zero on any failed assertion.
- Include every relevant header. Construct objects using ONLY the
  public constructor signatures pinned by the spec's "C++ API"
  section.""",
    test_command_hint="make tests/test_<name> && ./tests/test_<name>",
    aliases=("c++",),
)


# ---- Rust ----

_RUST = LanguageProfile(
    name="rust",
    display_name="Rust 1.x (edition 2021)",
    extensions=(".rs",),
    integration_test_filename="tests/integration.rs",
    phase2_intro="You are writing Rust stub files for a parallel implementation project.",
    stub_rules="""\
- Edition 2021 with Cargo layout: ``src/lib.rs`` is the crate root,
  ``src/<module>.rs`` per module, ``src/<module>/mod.rs`` for module
  directories.
- Every function body uses ``unimplemented!("not implemented: <name>");``
  (or ``todo!(...)``).
- ``use`` statements at the top, grouped by std / external crates /
  ``crate::``.
- Constructors: idiomatically a ``pub fn new(...)`` associated function
  in an ``impl Type`` block. ONE ``new`` per type. For optional behaviour,
  prefer builder methods chained on the result of ``new``.
- ``pub`` exports only what cross-subtask code needs.""",
    test_rules="""\
- Use the built-in ``#[test]`` framework -- no external crates.
- Per-subtask tests live in the same source file under
  ``#[cfg(test)] mod tests { ... }`` OR in ``tests/test_<sid>.rs`` at
  the crate's top level. Stay consistent within the project.
- Each test calls real functions and asserts via ``assert_eq!`` /
  ``assert!``.
- Stubs panic via ``unimplemented!``, which makes the test fail. Do NOT
  use ``#[should_panic]`` to mask this -- that would make the test pass
  on a stub.
- Mock cross-subtask types with hand-written stub structs in your test
  module; never import another subtask's real types.""",
    integration_rules="""\
- ``tests/integration.rs`` (Cargo automatically picks up files under
  ``tests/`` as integration tests).
- ``use crate_name::*;`` to pull from every subtask.""",
    test_command_hint="cargo test <test_name> -- --nocapture",
    aliases=("rs",),
)


_REGISTRY: dict[str, LanguageProfile] = {}
for _p in (_PYTHON, _TYPESCRIPT, _JAVA, _CSHARP, _C, _CPP, _RUST):
    _REGISTRY[_p.name] = _p
    for _a in _p.aliases:
        _REGISTRY[_a] = _p


def all_profiles() -> tuple[LanguageProfile, ...]:
    """Return the canonical-name-keyed list of profiles, deduplicated."""
    seen: set[str] = set()
    out: list[LanguageProfile] = []
    for p in _REGISTRY.values():
        if p.name in seen:
            continue
        seen.add(p.name)
        out.append(p)
    return tuple(out)


def profile_for(language: str | None = None,
                 repo_path: str | None = None) -> LanguageProfile:
    """Resolve a language identifier (or auto-detect from a repo) to a profile.

    Resolution order:
      1. Explicit ``language`` argument (canonical name or alias).
      2. ``COORDINATOR_LANGUAGE`` env var.
      3. Build-system markers in ``repo_path`` (Cargo.toml -> rust, etc.).
      4. Python (the default, matches POC behaviour).
    """
    if language:
        key = language.strip().lower()
        if key in _REGISTRY:
            return _REGISTRY[key]

    env = os.environ.get("COORDINATOR_LANGUAGE", "").strip().lower()
    if env and env in _REGISTRY:
        return _REGISTRY[env]

    if repo_path:
        detected = _detect_from_repo(repo_path)
        if detected is not None:
            return detected

    return _PYTHON


def _detect_from_repo(repo_path: str) -> LanguageProfile | None:
    """Sniff a repo's top-level files for a build-system marker."""
    try:
        entries = set(os.listdir(repo_path))
    except OSError:
        return None
    for marker, lang in _REPO_HINTS:
        if marker in entries:
            p = _REGISTRY.get(lang)
            if p is not None:
                return p
    return None
