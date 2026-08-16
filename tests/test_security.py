"""Security suite (spec §34).

Every test builds its own sandbox under `tmp_path` and either passes a
sandboxed `Config` directly to the `app.security.paths` functions (which
accept `cfg` as a parameter) or monkeypatches `load_config` inside the
modules that call it internally with no way to inject one (tool handlers).
Nothing here touches the real `~/Documents/Projects` or the developer's real
`.env`.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from pathlib import Path

import pytest

from app.config import load_config
from app.logging_setup import _RedactFilter
from app.security.commands import (
    ALLOWED_BINARIES,
    HARD_BLOCKED_BINARIES,
    is_high_risk_command,
    sanitized_env,
    validate_command,
)
from app.security.paths import (
    is_sensitive,
    is_within_allowed,
    resolve_read_path,
    resolve_write_path,
)
from app.tools.registry import RiskLevel, ToolExecutionError, registry

# Importing these modules registers delete_file / run_command / etc. into the
# shared registry singleton, which the risk-level assertions below check.
import app.tools.filesystem as fs_tools  # noqa: E402  (import needed for side effect)
import app.tools.shell as shell_tools  # noqa: E402  (import needed for side effect)


def test_logging_filter_redacts_current_gemini_key_formats():
    for secret in (
        "AIza" + "SySyntheticKeyThatMustNeverReachLogs",
        "AQ." + "SyntheticGeminiCredentialValueForRedactionOnly",
    ):
        record = logging.LogRecord("test", logging.INFO, "", 0, "key=%s", (secret,), None)
        assert _RedactFilter().filter(record)
        assert secret not in record.getMessage()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox_cfg(tmp_path):
    """A real Config whose only allowed root is a fresh tmp_path directory."""
    root = tmp_path / "sandbox"
    root.mkdir()
    base = load_config()
    return dataclasses.replace(base, allowed_dirs=[root.resolve()])


@pytest.fixture
def wired_cfg(sandbox_cfg, monkeypatch):
    """Point every module's `load_config()` at the sandboxed config.

    Tool handlers (read_file, delete_file, run_command, ...) take exactly one
    argument — the validated pydantic model — so tests can't hand them a
    `cfg` directly. This monkeypatches the name each module bound at import
    time (`from app.config import load_config`), which is the only place the
    real config could otherwise leak into a test.
    """
    monkeypatch.setattr("app.security.paths.load_config", lambda: sandbox_cfg)
    monkeypatch.setattr("app.tools.filesystem.load_config", lambda: sandbox_cfg)
    monkeypatch.setattr("app.tools.shell.load_config", lambda: sandbox_cfg)
    return sandbox_cfg


# ---------------------------------------------------------------------------
# commands.py — shell allowlist
# ---------------------------------------------------------------------------


def test_rm_rf_root_rejected():
    with pytest.raises(ToolExecutionError):
        validate_command(["rm", "-rf", "/"])


def test_sudo_rejected():
    with pytest.raises(ToolExecutionError):
        validate_command(["sudo", "ls"])


@pytest.mark.parametrize("binary", sorted(HARD_BLOCKED_BINARIES))
def test_all_hard_blocked_binaries_rejected(binary):
    with pytest.raises(ToolExecutionError):
        validate_command([binary])


@pytest.mark.parametrize("binary", sorted(ALLOWED_BINARIES))
def test_all_allowed_binaries_pass_validation(binary):
    # Validation only checks the binary name and metacharacters — it doesn't
    # require the binary to exist on this machine.
    assert validate_command([binary]) == [binary]


def test_unknown_binary_rejected():
    with pytest.raises(ToolExecutionError, match="not an allowed command"):
        validate_command(["perl", "-e", "1"])


def test_path_separator_in_binary_rejected():
    with pytest.raises(ToolExecutionError):
        validate_command(["/bin/sh", "-c", "ls"])
    with pytest.raises(ToolExecutionError):
        validate_command(["./script.sh"])


@pytest.mark.parametrize(
    "bad_arg",
    ["; rm -rf /", "foo | bar", "a && b", "$(whoami)", "`whoami`", "a > b", "a < b", "a\nb"],
)
def test_shell_metacharacters_rejected(bad_arg):
    with pytest.raises(ToolExecutionError):
        validate_command(["git", "status", bad_arg])


def test_git_push_allowed_by_validation_but_flagged_high_risk():
    # run_command is HIGH_RISK for every invocation (spec §31 always confirms
    # arbitrary shell), but is_high_risk_command still identifies *which*
    # subcommands are the consequential ones, per the contract's list.
    assert validate_command(["git", "push"]) == ["git", "push"]
    assert is_high_risk_command(["git", "push"]) is True
    assert is_high_risk_command(["git", "status"]) is False
    assert is_high_risk_command(["npm", "install", "left-pad"]) is True
    assert is_high_risk_command(["npm", "run", "build"]) is False
    assert is_high_risk_command(["git", "checkout", "-f", "main"]) is True


def test_sanitized_env_has_no_secret_keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "leak-me-not")
    monkeypatch.setenv("GROQ_API_KEY", "leak-me-not")
    monkeypatch.setenv("SOME_OTHER_SECRET_TOKEN", "leak-me-not")
    monkeypatch.setenv("DB_PASSWORD", "leak-me-not")

    env = sanitized_env()

    secret_pattern = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)
    assert not any(secret_pattern.search(k) for k in env)
    assert "GEMINI_API_KEY" not in env
    assert "GROQ_API_KEY" not in env


# ---------------------------------------------------------------------------
# paths.py — filesystem sandbox
# ---------------------------------------------------------------------------


def test_empty_allowed_dirs_denies_everything(tmp_path):
    cfg = dataclasses.replace(load_config(), allowed_dirs=[])
    target = tmp_path / "file.txt"
    target.write_text("data")
    with pytest.raises(ToolExecutionError, match="no allowed directories"):
        resolve_read_path(target, cfg=cfg)


def test_ssh_key_outside_sandbox_rejected(tmp_path, sandbox_cfg):
    # A file that looks exactly like a real ~/.ssh/id_rsa, but sitting
    # outside the sandbox entirely — must be rejected for containment,
    # independent of the sensitive-name check.
    outside_ssh = tmp_path / "home" / ".ssh"
    outside_ssh.mkdir(parents=True)
    key = outside_ssh / "id_rsa"
    key.write_text("fake private key")

    with pytest.raises(ToolExecutionError, match="outside the allowed directories"):
        resolve_read_path(key, cfg=sandbox_cfg)


def test_ssh_key_sensitive_pattern_rejected_even_inside_sandbox(sandbox_cfg):
    # The same filename, this time placed *inside* the allowed root — still
    # rejected, this time by the sensitive-file denylist.
    root = sandbox_cfg.allowed_dirs[0]
    ssh_dir = root / ".ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "id_rsa"
    key.write_text("fake private key")

    with pytest.raises(ToolExecutionError, match="sensitive"):
        resolve_read_path(key, cfg=sandbox_cfg)

    # And the bare pattern check agrees, independent of sandbox containment.
    assert is_sensitive(key) is True
    assert is_sensitive(root / "id_ed25519.pub") is True


def test_env_file_inside_allowed_root_rejected(sandbox_cfg):
    root = sandbox_cfg.allowed_dirs[0]
    env_file = root / ".env"
    env_file.write_text("GEMINI_API_KEY=abc123")

    with pytest.raises(ToolExecutionError, match="sensitive"):
        resolve_read_path(env_file, cfg=sandbox_cfg)

    env_local = root / ".env.production"
    env_local.write_text("SECRET=1")
    with pytest.raises(ToolExecutionError, match="sensitive"):
        resolve_read_path(env_local, cfg=sandbox_cfg)


@pytest.mark.parametrize(
    "name",
    ["credentials.json", "secrets.yaml", "credentials", "server.pem", "cert.key", "vault.p12"],
)
def test_other_sensitive_filenames_rejected(sandbox_cfg, name):
    root = sandbox_cfg.allowed_dirs[0]
    target = root / name
    target.write_text("data")
    with pytest.raises(ToolExecutionError, match="sensitive"):
        resolve_read_path(target, cfg=sandbox_cfg)


def test_path_traversal_rejected(sandbox_cfg):
    root = sandbox_cfg.allowed_dirs[0]
    # Enough ".." segments to guarantee escaping past filesystem root and
    # landing on a real, existing file outside any allowed directory.
    traversal = Path(str(root) + ("/.." * 25) + "/etc/passwd")

    with pytest.raises(ToolExecutionError, match="outside the allowed directories"):
        resolve_read_path(traversal, cfg=sandbox_cfg)


def test_symlink_inside_allowed_root_pointing_outside_rejected(tmp_path, sandbox_cfg):
    root = sandbox_cfg.allowed_dirs[0]
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    secret = outside_dir / "secret.txt"
    secret.write_text("private data that must not leak")

    link = root / "innocuous_link.txt"
    link.symlink_to(secret)

    with pytest.raises(ToolExecutionError, match="outside the allowed directories"):
        resolve_read_path(link, cfg=sandbox_cfg)


def test_sibling_prefix_directory_rejected(tmp_path):
    # `Projects-evil` shares a string prefix with `Projects` but is not a
    # descendant of it — a naive str.startswith containment check would let
    # this through; component-wise comparison must not.
    projects = tmp_path / "Projects"
    projects.mkdir()
    evil = tmp_path / "Projects-evil"
    evil.mkdir()
    evil_file = evil / "file.txt"
    evil_file.write_text("data")

    cfg = dataclasses.replace(load_config(), allowed_dirs=[projects.resolve()])
    with pytest.raises(ToolExecutionError, match="outside the allowed directories"):
        resolve_read_path(evil_file, cfg=cfg)


def test_is_within_allowed_rejects_string_prefix_match():
    root = Path("/Users/x/Documents/Projects")
    sibling = Path("/Users/x/Documents/Projects-evil/file.txt")
    child = Path("/Users/x/Documents/Projects/app/main.py")

    assert is_within_allowed(sibling, [root]) is False
    assert is_within_allowed(child, [root]) is True
    assert is_within_allowed(root, [root]) is True  # the root itself is "within"


def test_write_path_allows_nonexistent_target_under_existing_ancestor(sandbox_cfg):
    root = sandbox_cfg.allowed_dirs[0]
    target = root / "new" / "nested" / "file.txt"
    resolved = resolve_write_path(target, cfg=sandbox_cfg)
    assert resolved == (root / "new" / "nested" / "file.txt")


def test_write_path_rejects_nonexistent_target_escaping_via_missing_ancestor(tmp_path, sandbox_cfg):
    root = sandbox_cfg.allowed_dirs[0]
    target = root / ".." / "escaped" / "file.txt"
    with pytest.raises(ToolExecutionError, match="outside the allowed directories"):
        resolve_write_path(target, cfg=sandbox_cfg)


# ---------------------------------------------------------------------------
# registry declarations — proving the risk levels are actually right
# ---------------------------------------------------------------------------


def test_delete_file_is_high_risk():
    tool = registry.get("delete_file")
    assert tool is not None
    assert tool.risk is RiskLevel.HIGH_RISK
    assert tool.confirm_template is not None


def test_run_command_is_high_risk():
    tool = registry.get("run_command")
    assert tool is not None
    assert tool.risk is RiskLevel.HIGH_RISK
    assert tool.confirm_template is not None


def test_move_file_is_high_risk():
    tool = registry.get("move_file")
    assert tool is not None
    assert tool.risk is RiskLevel.HIGH_RISK


@pytest.mark.parametrize(
    "name", ["read_file", "list_directory", "search_files"]
)
def test_read_only_filesystem_tools_are_read_only(name):
    tool = registry.get(name)
    assert tool is not None
    assert tool.risk is RiskLevel.READ_ONLY


@pytest.mark.parametrize("name", ["write_file", "create_directory", "copy_file"])
def test_low_risk_write_filesystem_tools(name):
    tool = registry.get(name)
    assert tool is not None
    assert tool.risk is RiskLevel.LOW_RISK_WRITE


# ---------------------------------------------------------------------------
# tool-handler integration — delete_file / run_command end to end
# ---------------------------------------------------------------------------


def test_delete_file_refuses_to_delete_sandbox_root(wired_cfg):
    root = wired_cfg.allowed_dirs[0]
    args = fs_tools.DeleteFileArgs(path=str(root))
    with pytest.raises(ToolExecutionError, match="sandbox root"):
        fs_tools.delete_file(args)


def test_delete_file_refuses_directory_without_recursive(wired_cfg):
    root = wired_cfg.allowed_dirs[0]
    sub = root / "sub"
    sub.mkdir()
    (sub / "child.txt").write_text("x")
    args = fs_tools.DeleteFileArgs(path=str(sub), recursive=False)
    with pytest.raises(ToolExecutionError, match="recursive"):
        fs_tools.delete_file(args)


def test_delete_file_deletes_with_recursive(wired_cfg):
    root = wired_cfg.allowed_dirs[0]
    sub = root / "sub2"
    sub.mkdir()
    (sub / "child.txt").write_text("x")
    args = fs_tools.DeleteFileArgs(path=str(sub), recursive=True)
    result = fs_tools.delete_file(args)
    assert result["deleted"] is True
    assert not sub.exists()


def test_write_then_read_round_trip(wired_cfg):
    root = wired_cfg.allowed_dirs[0]
    write_args = fs_tools.WriteFileArgs(path=str(root / "hello.txt"), content="hi there")
    fs_tools.write_file(write_args)

    read_args = fs_tools.ReadFileArgs(path=str(root / "hello.txt"))
    result = fs_tools.read_file(read_args)
    assert result["content"] == "hi there"
    assert result["truncated"] is False


def test_write_file_refuses_overwrite_by_default(wired_cfg):
    root = wired_cfg.allowed_dirs[0]
    target = root / "exists.txt"
    target.write_text("original")
    args = fs_tools.WriteFileArgs(path=str(target), content="clobber")
    with pytest.raises(ToolExecutionError, match="already exists"):
        fs_tools.write_file(args)
    assert target.read_text() == "original"


def test_run_command_executes_allowed_binary(wired_cfg):
    args = shell_tools.RunCommandArgs(command=["echo", "hello-sandbox"])
    result = shell_tools.run_command(args)
    assert result["exit_code"] == 0
    assert "hello-sandbox" in result["stdout"]


def test_run_command_rejects_disallowed_binary(wired_cfg):
    args = shell_tools.RunCommandArgs(command=["perl", "-e", "1"])
    with pytest.raises(ToolExecutionError):
        shell_tools.run_command(args)


def test_run_command_rejects_rm(wired_cfg):
    args = shell_tools.RunCommandArgs(command=["rm", "-rf", "/"])
    with pytest.raises(ToolExecutionError):
        shell_tools.run_command(args)


def test_run_command_cwd_defaults_to_sandbox_root(wired_cfg):
    root = wired_cfg.allowed_dirs[0]
    args = shell_tools.RunCommandArgs(command=["pwd"])
    result = shell_tools.run_command(args)
    assert result["stdout"].strip() == str(root)
