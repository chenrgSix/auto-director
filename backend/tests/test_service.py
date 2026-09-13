import plistlib
import subprocess
from copy import deepcopy

import pytest

from app import service as module
from app.service import LABEL, LocalService, ServiceError, definition


@pytest.fixture
def service(tmp_path, monkeypatch):
    root, data, home = [tmp_path / name for name in ("workspace with spaces", "data", "home")]
    (root / "backend/.venv/bin").mkdir(parents=True)
    (root / "backend/.venv/bin/python").touch()
    (root / "frontend/dist").mkdir(parents=True)
    (root / "frontend/dist/index.html").touch()
    instance = LocalService(root, data, home, 501)
    state = {"loaded": False, "calls": []}

    def launchctl(*args, check=True):
        state["calls"].append(args)
        if args[0] == "print":
            return subprocess.CompletedProcess(args, 0 if state["loaded"] else 113, "", "")
        if args[0] == "bootstrap":
            state["loaded"] = True
        if args[0] == "bootout":
            state["loaded"] = False
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(instance, "launchctl", launchctl)
    monkeypatch.setattr(instance, "require_free_port", lambda: None)
    monkeypatch.setattr(module.shutil, "which", lambda name: f"/custom/media tools/{name}")
    return instance, state


def test_install_creates_private_launch_agent_with_correct_runtime(service, monkeypatch):
    instance, state = service
    monkeypatch.setenv("AD_LLM_API_KEY", "secret-not-for-plist")
    instance.install()
    saved = plistlib.loads(instance.plist.read_bytes())
    assert saved == definition(instance.root, instance.data)
    assert saved["ProgramArguments"][0] == str(instance.root / "backend/.venv/bin/python")
    assert "--reload" not in saved["ProgramArguments"]
    assert "--workers" not in saved["ProgramArguments"]
    assert saved["RunAtLoad"] and saved["KeepAlive"]
    assert saved["ThrottleInterval"] == 10
    assert saved["Umask"] == 0o077
    assert "/custom/media tools" in saved["EnvironmentVariables"]["PATH"]
    assert b"secret-not-for-plist" not in instance.plist.read_bytes()
    assert instance.plist.stat().st_mode & 0o777 == 0o600
    assert state["calls"][-2:] == [
        ("enable", f"gui/501/{LABEL}"),
        ("bootstrap", "gui/501", str(instance.plist)),
    ]


def test_reinstall_and_start_leave_running_worker_untouched(service):
    instance, state = service
    instance.install()
    before = instance.plist.stat().st_mtime_ns
    state["calls"].clear()
    instance.install()
    instance.start()
    assert instance.plist.stat().st_mtime_ns == before
    assert [args[0] for args in state["calls"]] == ["print", "print"]


def test_stop_unloads_before_disabling_and_start_enables_again(service):
    instance, state = service
    instance.install()
    state["calls"].clear()
    instance.stop()
    assert state["calls"] == [
        ("print", instance.target),
        ("bootout", instance.target),
        ("disable", instance.target),
    ]
    assert instance.plist.exists()
    instance.start()
    assert state["loaded"]
    assert ("enable", instance.target) in state["calls"]


@pytest.mark.parametrize("action", ["install", "start", "stop"])
@pytest.mark.parametrize("field", ["WorkingDirectory", "ProgramArguments", "EnvironmentVariables"])
def test_refuses_to_touch_another_workspace_or_data_directory(service, action, field):
    instance, state = service
    instance.install()
    config = deepcopy(instance.owned_config())
    config[field] = {
        "WorkingDirectory": "/other/backend",
        "ProgramArguments": ["/other/python"],
        "EnvironmentVariables": {"AD_DATA_DIR": "/other/data"},
    }[field]
    instance.plist.write_bytes(plistlib.dumps(config))
    before = instance.plist.read_bytes()
    state["calls"].clear()
    with pytest.raises(ServiceError, match="其他工作区或数据目录"):
        getattr(instance, action)()
    assert instance.plist.read_bytes() == before
    assert state["calls"] == []


def test_port_conflict_does_not_install_or_kill_anything(service, monkeypatch):
    instance, state = service

    def occupied():
        raise ServiceError("8000 已有服务监听")

    monkeypatch.setattr(instance, "require_free_port", occupied)
    with pytest.raises(ServiceError, match="8000"):
        instance.install()
    assert not instance.plist.exists()
    assert state["calls"] == [("print", instance.target)]


def test_install_refuses_symlink(service, tmp_path):
    instance, state = service
    other = tmp_path / "other.plist"
    other.write_bytes(b"do not overwrite")
    instance.plist.parent.mkdir(parents=True)
    instance.plist.symlink_to(other)
    with pytest.raises(ServiceError, match="符号链接"):
        instance.install()
    assert other.read_bytes() == b"do not overwrite"
    assert state["calls"] == []


@pytest.mark.parametrize("missing", ["backend/.venv/bin/python", "frontend/dist/index.html"])
def test_missing_dependencies_do_not_enable_a_restart_loop(service, missing):
    instance, state = service
    (instance.root / missing).unlink()
    with pytest.raises(ServiceError, match="缺少"):
        instance.install()
    assert not instance.plist.exists()
    assert all(args[0] == "print" for args in state["calls"])


def test_subprocess_uses_argument_array_and_surfaces_launchctl_errors(service, monkeypatch):
    instance, _ = service
    calls = []

    def fail(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 5, "", "bootstrap refused")

    monkeypatch.setattr(module.subprocess, "run", fail)
    with pytest.raises(ServiceError, match="bootstrap refused"):
        LocalService.launchctl(instance, "bootstrap", instance.domain, str(instance.plist))
    assert calls[0][0] == ["/bin/launchctl", "bootstrap", "gui/501", str(instance.plist)]
    assert "shell" not in calls[0][1]
    assert calls[0][1]["timeout"] == 30
