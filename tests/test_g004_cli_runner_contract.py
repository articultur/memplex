"""Contracts for the real-process G004 CLI test runner."""

from __future__ import annotations

import ast
import importlib
import json
import re
import shlex
import socket
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests import g004_cli_runner
from tests.g004_cli_runner import parse_json_stdout, run_cli


REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_VALUE_GUIDE = REPO_ROOT / "docs/guides/real-value-cli.md"
COMMUNITY_FILES = (
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "CODE_OF_CONDUCT.md",
    REPO_ROOT / "SECURITY.md",
    REPO_ROOT / "GOVERNANCE.md",
    REPO_ROOT / "SUPPORT.md",
)
CANONICAL_GUIDE_LINK = "docs/guides/real-value-cli.md"
ISSUE_TEMPLATE_DIR = REPO_ROOT / ".github/ISSUE_TEMPLATE"
ISSUES_URL = "https://github.com/articultur/memplex/issues"
SECURITY_ADVISORY_URL = (
    "https://github.com/articultur/memplex/security/advisories/new"
)
CONDUCT_FORM_URL = (
    "https://github.com/articultur/memplex/issues/new?template=conduct_report.yml"
)
GITHUB_ABUSE_URL = "https://support.github.com/contact/report-abuse"
G004_COMMAND_SOURCES = (
    REPO_ROOT / "tests/test_g004_lite_real_value.py",
    REPO_ROOT / "tests/test_g004_agent_real_value.py",
    REPO_ROOT / "tests/test_g004_sync_real_loopback.py",
    REPO_ROOT / "tests/test_g004_postgres_backup_real_value.py",
)
_DYNAMIC_ARG = "<dynamic>"


def _logical_shell_lines(block: str) -> list[str]:
    logical_lines: list[str] = []
    logical_line = ""
    for raw_line in block.splitlines():
        line = re.sub(r"^(?:\$|>)\s+", "", raw_line.strip())
        if not line or line.startswith("#"):
            continue
        logical_line = f"{logical_line} {line}".strip()
        if logical_line.endswith("\\"):
            logical_line = logical_line[:-1].rstrip()
            continue
        logical_lines.append(logical_line)
        logical_line = ""
    if logical_line:
        logical_lines.append(logical_line)
    return logical_lines


def _shell_commands(markdown: str) -> list[str]:
    commands: list[str] = []
    fence_pattern = re.compile(
        r"^```(?:bash|sh|shell)\s*\n(.*?)^```\s*$",
        flags=re.DOTALL | re.MULTILINE | re.IGNORECASE,
    )
    for block in fence_pattern.findall(markdown):
        commands.extend(_logical_shell_lines(block))
    return commands


def _documented_memplex_commands(markdown: str) -> list[str]:
    commands = [
        command
        for command in _shell_commands(markdown)
        if command.startswith(("memplex ", "python -m memplex "))
    ]
    commands.extend(
        re.findall(
            r"`((?:python -m )?memplex\s+[^`\n]+)`",
            markdown,
        )
    )
    return commands


def _static_argv_part(
    node: ast.expr,
    bindings: dict[str, list[str]],
) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name):
        return bindings.get(node.id, [_DYNAMIC_ARG])
    if isinstance(node, (ast.List, ast.Tuple)):
        values: list[str] = []
        for element in node.elts:
            if isinstance(element, ast.Starred):
                values.extend(_static_argv_part(element.value, bindings))
            else:
                values.extend(_static_argv_part(element, bindings))
        return values
    return [_DYNAMIC_ARG]


def _memplex_command_form(argv: list[str]) -> tuple[str, ...] | None:
    if argv and (argv[0] == "memplex" or argv[0].endswith("/memplex")):
        offset = 1
    elif len(argv) >= 3 and argv[1:3] == ["-m", "memplex"]:
        offset = 3
    else:
        return None
    if argv[offset : offset + 2] != ["--output", "json"]:
        raise AssertionError(f"Memplex command lacks top-level --output json: {argv!r}")
    tail = argv[offset + 2 :]
    form: list[str] = []
    for token in tail:
        if token == _DYNAMIC_ARG or token.startswith(("--", "$")) or " " in token:
            break
        form.append(token)
    form.extend(token for token in tail if token.startswith("--"))
    return tuple(form)


def _tested_g004_command_forms() -> set[tuple[str, ...]]:
    forms: set[tuple[str, ...]] = set()
    for path in G004_COMMAND_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_bindings: dict[str, list[str]] = {}
        for statement in tree.body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
            ):
                module_bindings[statement.targets[0].id] = _static_argv_part(
                    statement.value,
                    module_bindings,
                )
        for function in (
            node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            bindings = dict(module_bindings)
            assignments = sorted(
                (
                    node
                    for node in ast.walk(function)
                    if isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                ),
                key=lambda node: node.lineno,
            )
            for assignment in assignments:
                target = assignment.targets[0]
                assert isinstance(target, ast.Name)
                bindings[target.id] = _static_argv_part(assignment.value, bindings)
            for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                if not isinstance(call.func, ast.Name):
                    continue
                if call.func.id not in {"run_cli", "_run_json"} or not call.args:
                    continue
                form = _memplex_command_form(
                    _static_argv_part(call.args[0], bindings)
                )
                if form is not None:
                    forms.add(form)
    return forms


def _fenced_blocks(markdown: str) -> list[tuple[str, str]]:
    return re.findall(
        r"^```([^\n]*)\n(.*?)^```\s*$",
        markdown,
        flags=re.DOTALL | re.MULTILINE,
    )


def _json_response_candidates(markdown: str) -> list[Any]:
    without_fences = re.sub(
        r"^```[^\n]*\n.*?^```\s*$",
        "",
        markdown,
        flags=re.DOTALL | re.MULTILINE,
    )
    candidates: list[Any] = []
    for paragraph in re.split(r"\n\s*\n", without_fences):
        stripped = paragraph.strip()
        if not stripped:
            continue
        try:
            candidates.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    for language, block in _fenced_blocks(markdown):
        if language.strip().lower() not in {"bash", "sh", "shell"}:
            continue
        lines = _logical_shell_lines(block)
        for start in range(len(lines)):
            for end in range(start + 1, len(lines) + 1):
                candidate = "\n".join(lines[start:end])
                try:
                    value = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
                candidates.append(value)
                break
    return candidates


def _github_heading_anchor(heading: str) -> str:
    anchor = heading.strip().lower()
    anchor = re.sub(r"[^\w\- ]", "", anchor)
    return re.sub(r"\s+", "-", anchor)


def _assert_local_markdown_links_resolve(path: Path) -> None:
    markdown = path.read_text(encoding="utf-8")
    for target in re.findall(r"(?<!!)\[[^]]+]\(([^)]+)\)", markdown):
        if target.startswith(("https://", "http://", "#")):
            target_path, _, anchor = target.partition("#")
            if not target_path:
                target_document = path
            else:
                continue
        else:
            target_path, _, anchor = target.partition("#")
            target_document = (path.parent / target_path).resolve()
            assert target_document.exists(), f"broken link in {path}: {target}"
        if anchor and target_document.is_file():
            target_markdown = target_document.read_text(encoding="utf-8")
            anchors = {
                _github_heading_anchor(heading)
                for heading in re.findall(r"^#{1,6}\s+(.+)$", target_markdown, re.MULTILINE)
            }
            assert anchor in anchors, f"broken anchor in {path}: {target}"


def test_markdown_command_parser_covers_shell_fences_prompts_and_inline() -> None:
    markdown = """
```sh
$ memplex --output json query \\
>   "durable lookup"
```
```shell
python -m memplex --output json recall "backup lookup"
```
Inline: `memplex --output json scope list`.
"""

    assert _documented_memplex_commands(markdown) == [
        'memplex --output json query "durable lookup"',
        'python -m memplex --output json recall "backup lookup"',
        'memplex --output json scope list',
    ]


@pytest.mark.parametrize(
    ("markdown", "expected"),
    (
        ('{\n  "status": "ok"\n}', {"status": "ok"}),
        ('[\n  "first",\n  "second"\n]', ["first", "second"]),
        ("true", True),
        ('"scalar response"', "scalar response"),
        ("42", 42),
    ),
)
def test_fake_response_detector_catches_multiline_and_scalar_json(
    markdown: str,
    expected: Any,
) -> None:
    assert _json_response_candidates(markdown) == [expected]


def test_fake_response_detector_rejects_json_mixed_with_shell_command() -> None:
    malicious = '''```bash
memplex --output json sync status
{"status":"active"}
```'''

    assert _json_response_candidates(malicious) == [{"status": "active"}]


def test_fake_response_detector_allows_shell_documentation_syntax() -> None:
    safe = '''```sh
# Explain the prerequisite.
export MEMPLEX_STORAGE_BACKEND=lite
MEMPLEX_STORAGE_PATH="$PWD/.memplex"
if test -n "$MEMPLEX_STORAGE_PATH"; then
  memplex --output json sync status
fi
```'''

    assert _json_response_candidates(safe) == []


@pytest.mark.parametrize("language", ("", "json", "text", "plaintext", "output", "console"))
def test_output_like_fences_are_not_shell_command_fences(language: str) -> None:
    fenced = f"```{language}\nresponse payload\n```"
    assert [name.strip().lower() for name, _ in _fenced_blocks(fenced)] == [
        language
    ]


def test_g004_real_value_documents_exist_and_cover_bounded_workflows() -> None:
    required_documents = (REAL_VALUE_GUIDE, *COMMUNITY_FILES)

    assert all(path.is_file() for path in required_documents)
    guide = REAL_VALUE_GUIDE.read_text(encoding="utf-8")
    for section in (
        "## Local Lite workflow",
        "## Agent CLI workflow",
        "## Loopback sync workflow",
        "## Temporary PostgreSQL backup drill",
        "## Environment, identity, and configuration",
    ):
        assert section in guide
    for boundary in (
        "Lite local",
        "not real-host G008 evidence",
        "not WAN or HA evidence",
        "not deployment RPO/RTO evidence",
    ):
        assert boundary in guide


def test_mandatory_loopback_e2e_has_no_dependency_skip_path() -> None:
    source = (REPO_ROOT / "tests/test_g004_sync_real_loopback.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "importorskip"
        for node in ast.walk(tree)
    )


def test_mandatory_loopback_e2e_fails_if_uvicorn_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests import test_g004_sync_real_loopback as loopback

    def missing_uvicorn(name: str) -> None:
        assert name == "uvicorn"
        raise ImportError

    monkeypatch.setattr(loopback.importlib, "import_module", missing_uvicorn)

    with pytest.raises(
        pytest.fail.Exception,
        match="required test dependency unavailable: uvicorn",
    ):
        loopback.test_public_sync_cli_converges_two_real_loopback_peers(tmp_path)


def test_g004_guide_uses_only_tested_top_level_json_command_forms() -> None:
    guide = REAL_VALUE_GUIDE.read_text(encoding="utf-8")
    documented_commands = _documented_memplex_commands(guide)
    documented_forms: list[tuple[str, ...]] = []
    for command in documented_commands:
        form = _memplex_command_form(shlex.split(command))
        assert form is not None
        documented_forms.append(form)
    assert set(documented_forms) <= _tested_g004_command_forms()
    recall_command = next(
        command for command in documented_commands if "agent recall" in command
    )
    recall_argv = shlex.split(recall_command)
    assert "durable release lookup" in recall_argv
    assert not any("alpha-7f9c" in argument for argument in recall_argv)
    assert _json_response_candidates(guide) == []
    assert {
        language.strip().lower() for language, _ in _fenced_blocks(guide)
    } <= {"bash", "sh", "shell"}


def test_g004_guide_has_exact_sync_and_backup_prerequisite_tables() -> None:
    guide = REAL_VALUE_GUIDE.read_text(encoding="utf-8")
    for sync_requirement in (
        "| `MEMPLEX_SYNC_CURSOR_SIGNING_KEY_ID` |",
        "| `MEMPLEX_SYNC_CURSOR_SIGNING_SECRET` | Same secret on both peers; at least 32 bytes |",
        "| Server override: `MEMPLEX_SYNC_NODE_ID` | `central-node` |",
        "| Server override: `MEMPLEX_SYNC_TARGETS_JSON` | `{}` |",
        "| Real loopback service | Real uvicorn child bound to `127.0.0.1` using `memplex.adapters.http_api:create_app --factory` |",
        "| Readiness | Successful HTTP check of `/health/ready` before sync commands |",
    ):
        assert sync_requirement in guide
    for backup_requirement in (
        "| `MEMPLEX_STORAGE_PATH` | Application-role PostgreSQL DSN |",
        "| `MEMPLEX_STORAGE_MIGRATION_DSN` | Migration-role PostgreSQL DSN for the same disposable schema |",
        "| `MEMPLEX_BACKUP_KEY_ID` | Non-secret active backup signing key identifier |",
        "| `MEMPLEX_BACKUP_HMAC_KEY` | Canonical Base64 encoding of exactly 32 secret bytes |",
        "| PostgreSQL tools | `pg_dump` and `pg_restore` available on `PATH` |",
        "| PostgreSQL capability | pgvector extension and vector operator probe succeed |",
    ):
        assert backup_requirement in guide
    assert "major versions matching" not in guide


def test_g004_community_contracts_link_to_guide_and_are_actionable() -> None:
    for path in COMMUNITY_FILES:
        content = path.read_text(encoding="utf-8")
        assert CANONICAL_GUIDE_LINK in content
        assert not re.search(r"\b(TODO|TBD|FIXME)\b", content, flags=re.IGNORECASE)
        assert not re.search(r"<[^>\n]+>", content)
        assert "contact the maintainer" not in content.lower()
        _assert_local_markdown_links_resolve(path)

    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for gate in (
        "uv lock --check",
        ".venv/bin/ruff check memplex tests",
        ".venv/bin/lint-imports",
        ".venv/bin/mypy",
        ".venv/bin/python -m pytest tests -q --cov=memplex --cov-fail-under=68",
        "MEMPLEX_REQUIRE_PGVECTOR=1 pytest tests/test_postgres_integration.py tests/test_postgres_backup_integration.py tests/test_sync_postgres_integration.py tests/test_sync_repository_contract.py tests/test_g014_postgres_task_repository.py tests/test_ci_postgres_contract.py",
        ".venv/bin/python -m pytest tests/test_g004_cli_runner_contract.py -q",
        ".venv/bin/ruff check tests/g004_cli_runner.py tests/test_g004_cli_runner_contract.py",
    ):
        assert gate in contributing
    assert "do not replace the full gates" in contributing
    assert "report the actual dynamically selected test count" in contributing
    assert "~3,100" not in contributing
    assert "~730" not in contributing
    assert "/opt/homebrew/Cellar/node@24/24.19.0/bin" in contributing
    assert "does not commit" in contributing

    security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert SECURITY_ADVISORY_URL in security
    assert ISSUES_URL not in security
    assert CONDUCT_FORM_URL not in security
    assert "Code of Conduct report" not in security
    assert "confidential conduct" not in security.lower()
    assert "vulnerability-only" in security.lower()
    assert "Do not open a public issue" in security
    assert "credential" in security.lower()
    assert "backup artifact" in security.lower()

    support = (REPO_ROOT / "SUPPORT.md").read_text(encoding="utf-8")
    assert ISSUES_URL in support
    assert "/issues/new?template=support_request.yml" in support
    assert CONDUCT_FORM_URL in support
    assert SECURITY_ADVISORY_URL not in support
    assert "github.com/articultur/memplex/discussions" not in support
    assert "redact" in support.lower()

    code_of_conduct = (REPO_ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")
    for section in ("## Scope", "## Expected behavior", "## Unacceptable behavior", "## Enforcement"):
        assert section in code_of_conduct
    assert ISSUES_URL in code_of_conduct
    assert CONDUCT_FORM_URL in code_of_conduct
    assert SECURITY_ADVISORY_URL not in code_of_conduct
    assert GITHUB_ABUSE_URL in code_of_conduct
    assert "platform abuse only" in code_of_conduct.lower()
    assert "confidential" in code_of_conduct.lower()

    governance = (REPO_ROOT / "GOVERNANCE.md").read_text(encoding="utf-8")
    for section in ("## Roles", "## Decisions", "## Escalation"):
        assert section in governance
    assert "/issues/new?template=governance_proposal.yml" in governance
    assert CONDUCT_FORM_URL in governance
    assert SECURITY_ADVISORY_URL not in governance
    assert "github.com/articultur/memplex/discussions" not in governance

    for content in (support, code_of_conduct, governance):
        normalized = re.sub(r"\s+", " ", content)
        assert "No project-controlled confidential conduct intake is currently available." in normalized

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert CANONICAL_GUIDE_LINK in readme
    _assert_local_markdown_links_resolve(REPO_ROOT / "README.md")
    readme_commands = _documented_memplex_commands(readme)
    duplicates = {
        command: count
        for command, count in Counter(readme_commands).items()
        if count > 1
    }
    assert duplicates == {}


def test_g004_issue_forms_make_each_public_channel_actionable() -> None:
    expected_forms = {
        "bug_report.yml": ("Bug report", "[Bug] ", "diagnostics"),
        "support_request.yml": ("Support request", "[Support] ", "redaction"),
        "conduct_report.yml": ("Public conduct report", "[Conduct] ", "public_notice"),
        "governance_proposal.yml": (
            "Governance proposal",
            "[Governance] ",
            "decision",
        ),
    }
    for filename, (name, title, required_id) in expected_forms.items():
        path = ISSUE_TEMPLATE_DIR / filename
        assert path.is_file()
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(payload) == {
            "name",
            "description",
            "title",
            "labels",
            "assignees",
            "body",
        }
        assert payload["name"] == name
        assert payload["title"] == title
        assert isinstance(payload["description"], str) and payload["description"]
        assert isinstance(payload["labels"], list)
        assert isinstance(payload["assignees"], list)
        body = payload["body"]
        assert isinstance(body, list) and body
        ids: list[str] = []
        for item in body:
            assert isinstance(item, dict)
            assert item["type"] in {
                "markdown",
                "input",
                "textarea",
                "dropdown",
                "checkboxes",
            }
            attributes = item.get("attributes")
            assert isinstance(attributes, dict)
            if item["type"] == "markdown":
                assert isinstance(attributes.get("value"), str)
                assert attributes["value"]
                continue
            item_id = item.get("id")
            assert isinstance(item_id, str) and re.fullmatch(r"[a-z][a-z0-9_-]*", item_id)
            ids.append(item_id)
            assert isinstance(attributes.get("label"), str) and attributes["label"]
            assert isinstance(attributes.get("description"), str)
            assert attributes["description"]
            assert item.get("validations") == {"required": True}
            if item["type"] == "checkboxes":
                options = attributes.get("options")
                assert isinstance(options, list) and options
                assert all(
                    isinstance(option, dict)
                    and isinstance(option.get("label"), str)
                    and option["label"]
                    and option.get("required") is True
                    for option in options
                )
        assert len(ids) == len(set(ids))
        assert required_id in ids

    config = yaml.safe_load(
        (ISSUE_TEMPLATE_DIR / "config.yml").read_text(encoding="utf-8")
    )
    assert config["blank_issues_enabled"] is False
    assert set(config) == {"blank_issues_enabled", "contact_links"}
    assert config["contact_links"] == [
        {
            "name": "Report a vulnerability privately",
            "url": SECURITY_ADVISORY_URL,
            "about": "Security Advisory intake is for vulnerabilities only.",
        }
    ]
    assert "conduct" not in json.dumps(config, sort_keys=True).lower()


def test_non_security_docs_and_templates_do_not_route_to_advisories() -> None:
    non_security_paths = (
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / "CODE_OF_CONDUCT.md",
        REPO_ROOT / "GOVERNANCE.md",
        REPO_ROOT / "SUPPORT.md",
        REAL_VALUE_GUIDE,
        ISSUE_TEMPLATE_DIR / "bug_report.yml",
        ISSUE_TEMPLATE_DIR / "support_request.yml",
        ISSUE_TEMPLATE_DIR / "conduct_report.yml",
        ISSUE_TEMPLATE_DIR / "governance_proposal.yml",
    )
    for path in non_security_paths:
        content = path.read_text(encoding="utf-8")
        assert SECURITY_ADVISORY_URL not in content, path
        assert "/security/advisories" not in content, path

    governance_form = (
        ISSUE_TEMPLATE_DIR / "governance_proposal.yml"
    ).read_text(encoding="utf-8")
    assert "private advisory" not in governance_form.lower()
    assert "Do not include confidential content in this public form." in governance_form
    assert (
        "The project currently has no controlled private governance or conduct intake."
        in governance_form
    )


def test_local_run_requires_storage_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMPLEX_STORAGE_PATH", raising=False)

    with pytest.raises(
        ValueError,
        match="^MEMPLEX_STORAGE_PATH is required for local CLI runs$",
    ):
        run_cli([sys.executable, "-c", "print('must not run')"])


def test_run_uses_argument_sequence_without_shell(tmp_path: Path) -> None:
    literal_argument = "left; printf shell-was-used"

    completed = run_cli(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", literal_argument],
        env={"MEMPLEX_STORAGE_PATH": str(tmp_path / "store")},
    )

    assert completed.returncode == 0
    assert completed.stdout == f"{literal_argument}\n"
    assert completed.stderr == ""


def test_run_forwards_optional_stdin(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "import sys; print(sys.stdin.read())"]
    env = {"MEMPLEX_STORAGE_PATH": str(tmp_path / "store")}

    without_stdin = run_cli(command, env=env)
    with_stdin = run_cli(command, env=env, stdin="forwarded input")

    assert without_stdin.stdout == "\n"
    assert with_stdin.stdout == "forwarded input\n"


def test_run_merges_environment_additions_with_current_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("G004_AMBIENT_VALUE", "from-parent")
    additions = {
        "G004_CALLER_VALUE": "from-caller",
        "MEMPLEX_STORAGE_PATH": str(tmp_path / "store"),
    }
    script = (
        "import json, os; "
        "print(json.dumps([os.environ['G004_AMBIENT_VALUE'], "
        "os.environ['G004_CALLER_VALUE']]))"
    )

    completed = run_cli([sys.executable, "-c", script], env=additions)

    assert json.loads(completed.stdout) == ["from-parent", "from-caller"]
    assert additions == {
        "G004_CALLER_VALUE": "from-caller",
        "MEMPLEX_STORAGE_PATH": str(tmp_path / "store"),
    }


@pytest.mark.parametrize(
    "payload",
    [
        [1, "two", None],
        {"unfamiliar": {"nested": True}, "items": []},
        "scalar-json",
    ],
)
def test_parse_json_stdout_accepts_generic_json_values(
    payload: Any,
    tmp_path: Path,
) -> None:
    completed = run_cli(
        [sys.executable, "-c", f"print({json.dumps(json.dumps(payload))})"],
        env={"MEMPLEX_STORAGE_PATH": str(tmp_path / "store")},
    )

    assert parse_json_stdout(completed) == payload


def test_parse_json_stdout_reports_exact_process_diagnostics(tmp_path: Path) -> None:
    storage_secret = "postgresql://user:dsn-secret@db.invalid/memplex"
    hmac_secret = "hmac-secret-value"
    argv_secret = "g004-json-argv-secret"
    argv_uri = f"postgresql://json-user:{argv_secret}@db.invalid/memplex"
    script = (
        "import os, sys; print(os.environ['MEMPLEX_STORAGE_PATH']); "
        "print(os.environ['MEMPLEX_BACKUP_HMAC_KEY'], file=sys.stderr); "
        "raise SystemExit(9)"
    )
    command = [
        sys.executable,
        "-c",
        script,
        "--authorization",
        argv_secret,
        argv_uri,
    ]
    completed = run_cli(
        command,
        env={
            "MEMPLEX_STORAGE_PATH": storage_secret,
            "MEMPLEX_BACKUP_HMAC_KEY": hmac_secret,
        },
    )

    with pytest.raises(AssertionError) as raised:
        parse_json_stdout(completed)

    sanitized_command = [
        sys.executable,
        "-c",
        script,
        "--authorization",
        "<redacted>",
        "postgresql://<redacted>@db.invalid/memplex",
    ]
    assert str(raised.value) == (
        "CLI JSON parse failed\n"
        f"args: {sanitized_command!r}\n"
        "status: 9\n"
        f"stdout_chars: {len(storage_secret) + 1}\n"
        f"stderr_chars: {len(hmac_secret) + 1}"
    )
    assert storage_secret not in str(raised.value)
    assert hmac_secret not in str(raised.value)
    assert argv_secret not in str(raised.value)
    assert completed.args == command
    assert completed.stdout == f"{storage_secret}\n"
    assert completed.stderr == f"{hmac_secret}\n"


def test_sanitizer_fails_closed_for_malformed_uri_and_dsn() -> None:
    uri_secret = "g004-malformed-uri-secret"
    quoted_dsn_secret = "g004-malformed-quoted-dsn-secret"
    mixed_dsn_secret = "g004-malformed-mixed-dsn-secret"
    malformed_uri = f"postgresql://user:{uri_secret}@[invalid/memplex"
    malformed_quoted_dsn = f"host=db.invalid password='{quoted_dsn_secret}"
    malformed_mixed_dsn = (
        f"host=db.invalid password={mixed_dsn_secret} malformed-token"
    )
    completed = subprocess.CompletedProcess(
        ["memplex", malformed_uri, malformed_quoted_dsn, malformed_mixed_dsn],
        2,
        stdout="",
        stderr="",
    )

    diagnostic = g004_cli_runner.process_diagnostic(completed)

    for secret in (uri_secret, quoted_dsn_secret, mixed_dsn_secret):
        assert secret not in diagnostic
    assert diagnostic.count("<redacted>") >= 3
    assert completed.args[1:] == [
        malformed_uri,
        malformed_quoted_dsn,
        malformed_mixed_dsn,
    ]


def test_explicit_credential_names_are_sanitized_in_flags_query_and_fragment() -> None:
    credential_names = (
        "signature",
        "sig",
        "authorization",
        "oauth_code",
        "sslpassword",
        "access_token",
        "client_secret",
        "api_key",
        "private_key",
    )
    args = ["memplex"]
    secrets: list[str] = []
    for index, name in enumerate(credential_names):
        flag_secret = f"g004-flag-secret-{index}"
        uri_secret = f"g004-uri-secret-{index}"
        fragment_secret = f"g004-fragment-secret-{index}"
        secrets.extend((flag_secret, uri_secret, fragment_secret))
        args.extend(
            (
                f"--{name}",
                flag_secret,
                (
                    f"https://example.invalid/path?mode=probe&{name}={uri_secret}"
                    f"#view=summary&{name}={fragment_secret}"
                ),
            )
        )

    completed = subprocess.CompletedProcess(args, 3, stdout="", stderr="")
    diagnostic = g004_cli_runner.process_diagnostic(completed)

    for secret in secrets:
        assert secret not in diagnostic
    for name in credential_names:
        assert f"--{name}" in diagnostic
        assert name in diagnostic
    assert "mode=probe" in diagnostic
    assert "view=summary" in diagnostic


@pytest.mark.parametrize(
    "module_name",
    (
        "tests.test_g004_lite_real_value",
        "tests.test_g004_agent_real_value",
        "tests.test_g004_sync_real_loopback",
    ),
)
def test_g004_scenarios_use_the_shared_process_diagnostic(module_name: str) -> None:
    module = importlib.import_module(module_name)

    assert getattr(module, "process_diagnostic", None) is g004_cli_runner.process_diagnostic
    assert not hasattr(module, "_process_diagnostic")


def test_process_diagnostic_sanitizes_argv_without_mutating_completed_process() -> None:
    following_secret = "g004-following-secret"
    equals_secret = "g004-equals-secret"
    uri_secret = "g004-uri-secret"
    query_secret = "g004-query-secret"
    dsn_secret = "g004-dsn-secret"
    stream_secret = "g004-stream-secret"
    uri = (
        f"postgresql://dbuser:{uri_secret}@db.invalid/memplex"
        f"?sslmode=require&api_token={query_secret}"
    )
    dsn = f"host=db.invalid user=dbuser password={dsn_secret} dbname=memplex"
    args = [
        "memplex",
        "backup",
        "--password",
        following_secret,
        f"--api-token={equals_secret}",
        "--destination",
        "/tmp/nonsecret-artifact",
        uri,
        dsn,
    ]
    completed = subprocess.CompletedProcess(
        args,
        9,
        stdout=stream_secret,
        stderr=stream_secret,
    )

    diagnostic = g004_cli_runner.process_diagnostic(completed)

    assert completed.args == args
    assert completed.stdout == stream_secret
    assert completed.stderr == stream_secret
    for secret in (
        following_secret,
        equals_secret,
        uri_secret,
        query_secret,
        dsn_secret,
        stream_secret,
    ):
        assert secret not in diagnostic
    for command_shape in (
        "memplex",
        "backup",
        "--password",
        "--api-token=",
        "--destination",
        "/tmp/nonsecret-artifact",
        "postgresql://",
        "db.invalid",
        "sslmode",
        "api_token",
        "host=db.invalid",
        "dbname=memplex",
        "status: 9",
    ):
        assert command_shape in diagnostic


def test_http_readiness_exit_diagnostic_never_emits_environment_secrets() -> None:
    secret = "postgresql://user:readiness-secret@db.invalid/memplex"
    argv_secret = "g004-http-exit-argv-secret"
    argv_uri = (
        f"https://exit-user:{argv_secret}@example.invalid/ready"
        f"?signature={argv_secret}#oauth_code={argv_secret}"
    )
    script = (
        "import os, sys; print(os.environ['G004_READINESS_SECRET']); "
        "print(os.environ['G004_READINESS_SECRET'], file=sys.stderr); "
        "raise SystemExit(7)"
    )

    with g004_cli_runner.running_process(
        [sys.executable, "-c", script, "--sslpassword", argv_secret, argv_uri],
        env={"G004_READINESS_SECRET": secret},
        local=False,
    ) as process:
        with pytest.raises(AssertionError) as raised:
            g004_cli_runner.wait_for_http_ready(
                "http://127.0.0.1:1/ready",
                process,
                timeout=2,
            )

    diagnostic = str(raised.value)
    assert secret not in diagnostic
    assert argv_secret not in diagnostic
    assert process.args[-2:] == [argv_secret, argv_uri]
    assert "--sslpassword" in diagnostic
    assert "example.invalid" in diagnostic
    assert "status: 7" in diagnostic
    assert f"stdout_chars: {len(secret) + 1}" in diagnostic
    assert f"stderr_chars: {len(secret) + 1}" in diagnostic


def test_http_timeout_diagnostic_uses_the_same_argv_sanitizer() -> None:
    flag_secret = "g004-http-flag-secret"
    uri_secret = "g004-http-uri-secret"
    fragment_secret = "g004-http-fragment-secret"
    uri = (
        f"https://api-user:{uri_secret}@example.invalid/ready?token={uri_secret}"
        f"#signature={fragment_secret}&view=probe"
    )
    args = [
        sys.executable,
        "-c",
        "import time; time.sleep(2)",
        "--access-token",
        flag_secret,
        uri,
        "--mode",
        "probe",
    ]

    with g004_cli_runner.running_process(args, local=False) as process:
        with pytest.raises(AssertionError) as raised:
            g004_cli_runner.wait_for_http_ready(
                "http://127.0.0.1:1/ready",
                process,
                timeout=0.01,
            )

    diagnostic = str(raised.value)
    assert process.args == args
    assert flag_secret not in diagnostic
    assert uri_secret not in diagnostic
    assert fragment_secret not in diagnostic
    assert "--access-token" in diagnostic
    assert "https://" in diagnostic
    assert "example.invalid" in diagnostic
    assert "token" in diagnostic
    assert "--mode" in diagnostic
    assert "probe" in diagnostic


def test_run_preserves_nonzero_status_stdout_and_stderr(tmp_path: Path) -> None:
    script = (
        "import sys; print('kept-out'); "
        "print('kept-error', file=sys.stderr); raise SystemExit(7)"
    )
    command = [sys.executable, "-c", script]

    completed = run_cli(
        command,
        env={"MEMPLEX_STORAGE_PATH": str(tmp_path / "store")},
    )

    assert isinstance(completed, subprocess.CompletedProcess)
    assert completed.args == command
    assert completed.returncode == 7
    assert completed.stdout == "kept-out\n"
    assert completed.stderr == "kept-error\n"


def test_run_forwards_timeout(tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        run_cli(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            env={"MEMPLEX_STORAGE_PATH": str(tmp_path / "store")},
            timeout=0.01,
        )


def test_reserved_loopback_listener_is_handed_to_real_child_process() -> None:
    listener = g004_cli_runner.reserve_loopback_listener()
    port = int(listener.getsockname()[1])
    descriptor = listener.fileno()
    script = (
        "import socket, sys; "
        "listener = socket.socket(fileno=int(sys.argv[1])); "
        "connection, _ = listener.accept(); "
        "data = connection.recv(4); "
        "connection.sendall(b'pong:' + data); "
        "connection.close()"
    )

    with listener, g004_cli_runner.running_process(
        [sys.executable, "-c", script, str(descriptor)],
        local=False,
        pass_fds=(descriptor,),
    ):
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall(b"ping")
            assert client.recv(9) == b"pong:ping"
