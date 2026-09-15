# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Regression tests for side-effect-free Datacron imports."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

_IMPORT_COMMAND = (
    "import datacron.mcp.tools, datacron.core.vault_writer, datacron.core.operation_log"
)
_CONFIGURE_LOGGING_COMMAND = (
    "from datacron.core.logger import configure_logging; configure_logging()"
)
_HEALTH_IMPORT_COMMAND = (
    "import sys; import datacron.mcp.health; assert 'datacron.mcp.tools' not in sys.modules"
)


def _run_import(
    tmp_path: Path,
    *,
    command: str = _IMPORT_COMMAND,
    log_level: str | None = None,
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    working_directory = tmp_path / "work"
    home.mkdir()
    working_directory.mkdir()
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("DATACRON_")
    }
    environment.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if log_level is not None:
        environment["DATACRON_LOG_LEVEL"] = log_level

    completed = subprocess.run(
        [sys.executable, "-c", command],
        cwd=working_directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert list(home.iterdir()) == []
    assert list(working_directory.iterdir()) == []
    return completed


def test_imports_are_silent_and_do_not_create_runtime_directories(tmp_path: Path) -> None:
    _run_import(tmp_path)


def test_imports_do_not_parse_invalid_logging_environment(tmp_path: Path) -> None:
    _run_import(tmp_path, log_level="INVALID")

    home = tmp_path / "configure-home"
    working_directory = tmp_path / "configure-work"
    home.mkdir()
    working_directory.mkdir()
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("DATACRON_")
    }
    environment.update(
        {
            "DATACRON_LOG_LEVEL": "INVALID",
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", _CONFIGURE_LOGGING_COMMAND],
        cwd=working_directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "Invalid DATACRON_LOG_LEVEL" in completed.stderr


def test_health_import_does_not_initialize_tool_registry(tmp_path: Path) -> None:
    """Health assembly remains independent from the MCPServer tool package."""
    _run_import(tmp_path, command=_HEALTH_IMPORT_COMMAND)


# ---------------------------------------------------------------------------------------
# Layer boundaries, checked on the AST so a violation is named by file and line.
#
# ``core`` and ``organization`` are the domain layers: they must not import the MCP
# transport at runtime (an import under ``if TYPE_CHECKING:`` is a type annotation, not
# a dependency). And no package reaches into another package's private names: a helper
# two packages need is published under a public name where it belongs.

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "datacron"
_DOMAIN_PACKAGES = ("core", "organization")
_TRANSPORT_PACKAGE = "datacron.mcp"
# Private cross-package imports that predate this guard and sit outside the lot that
# introduced it. Each entry is a debt: remove it when the name gets a public home, and
# never add one to make a new import pass.
_PRIVATE_IMPORT_DEBTS = frozenset(
    {
        ("src/datacron/cli.py", "_scopes_for"),
        ("src/datacron/cli.py", "_validate_expected_hash"),
        ("src/datacron/eval/harness.py", "_search_text_impl"),
    }
)


def _module_package(path: Path) -> str:
    """Return the top-level package of a module under ``src/datacron``."""
    relative = path.relative_to(_SOURCE_ROOT)
    return relative.parts[0] if len(relative.parts) > 1 else ""


def _runtime_import_nodes(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    """Collect imports that execute at runtime, skipping ``if TYPE_CHECKING:`` blocks."""
    found: list[ast.Import | ast.ImportFrom] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and _is_type_checking_guard(node.test):
            for statement in node.orelse:
                visit(statement)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def _is_type_checking_guard(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _imported_package(node: ast.ImportFrom, module_path: Path) -> str:
    """Return the ``datacron.<package>`` prefix an import-from targets, or ``""``."""
    if node.level:
        base = module_path.parent
        for _ in range(node.level - 1):
            base = base.parent
        relative = base.relative_to(_SOURCE_ROOT.parent)
        module = ".".join(relative.parts + tuple((node.module or "").split(".")))
    else:
        module = node.module or ""
    parts = module.split(".")
    if len(parts) < 2 or parts[0] != "datacron":
        return ""
    return parts[1]


def _boundary_violations() -> list[str]:
    findings: list[str] = []
    for module_path in sorted(_SOURCE_ROOT.rglob("*.py")):
        package = _module_package(module_path)
        relative = module_path.relative_to(_SOURCE_ROOT.parents[1]).as_posix()
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in _runtime_import_nodes(tree):
            targets = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            if package in _DOMAIN_PACKAGES and any(
                target == _TRANSPORT_PACKAGE or target.startswith(_TRANSPORT_PACKAGE + ".")
                for target in targets
            ):
                findings.append(f"{relative}:{node.lineno}: {package} imports {targets[0]}")
            if isinstance(node, ast.ImportFrom):
                imported_package = _imported_package(node, module_path)
                if imported_package and imported_package != package:
                    for alias in node.names:
                        if alias.name.startswith("_") and (
                            (relative, alias.name) not in _PRIVATE_IMPORT_DEBTS
                        ):
                            findings.append(
                                f"{relative}:{node.lineno}: imports private "
                                f"{alias.name} from datacron.{imported_package}"
                            )
    return findings


def test_domain_layers_do_not_import_the_transport_or_private_names() -> None:
    findings = _boundary_violations()
    assert not findings, "\n".join(findings)
