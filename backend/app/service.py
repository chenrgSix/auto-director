"""Manage the single local API process independently of a terminal on macOS."""

import argparse
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from app.core.config import ROOT, Settings

LABEL = "local.autodirector"


class ServiceError(RuntimeError):
    pass


def definition(root: Path, data: Path) -> dict:
    # launchd does not inherit the interactive shell's PATH or virtual environment.
    paths = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    for name in ("ffmpeg", "ffprobe"):
        executable = shutil.which(name)
        if not executable:
            raise ServiceError(f"缺少 {name}，请先安装并加入 PATH。")
        directory = str(Path(executable).parent)
        if directory not in paths:
            paths.insert(0, directory)
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(root / "backend/.venv/bin/python"),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        "WorkingDirectory": str(root / "backend"),
        "EnvironmentVariables": {
            "PATH": ":".join(paths),
            "PYTHONUNBUFFERED": "1",
            "PYTHONFAULTHANDLER": "1",
            "AD_DATA_DIR": str(data),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": 60,
        "Umask": 0o077,
        "StandardOutPath": str(data / "logs/service.stdout.log"),
        "StandardErrorPath": str(data / "logs/service.stderr.log"),
    }


class LocalService:
    def __init__(self, root: Path, data: Path, home: Path, uid: int):
        self.root, self.data = root, data
        self.plist = home / "Library/LaunchAgents" / f"{LABEL}.plist"
        self.domain = f"gui/{uid}"
        self.target = f"{self.domain}/{LABEL}"

    def launchctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["/bin/launchctl", *args], capture_output=True, text=True, timeout=30, check=False
        )
        if check and result.returncode:
            raise ServiceError(f"launchctl {args[0]} 失败：{result.stderr.strip()}")
        return result

    def owned_config(self) -> dict:
        if self.plist.is_symlink():
            raise ServiceError("服务配置是符号链接，拒绝修改。")
        try:
            config = plistlib.loads(self.plist.read_bytes())
        except FileNotFoundError as exc:
            raise ServiceError("尚未安装常驻服务，请先运行 make service-install。") from exc
        if (
            config.get("Label") != LABEL
            or config.get("WorkingDirectory") != str(self.root / "backend")
            or config.get("ProgramArguments", [])[:4]
            != [str(self.root / "backend/.venv/bin/python"), "-m", "uvicorn", "app.main:app"]
            or config.get("EnvironmentVariables", {}).get("AD_DATA_DIR") != str(self.data)
        ):
            raise ServiceError("已有同名服务属于其他工作区或数据目录，拒绝覆盖或停止。")
        return config

    @staticmethod
    def require_free_port() -> None:
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", 8000)) == 0:
                raise ServiceError("8000 已有服务监听；请在制作空闲时停止原服务，再安装常驻服务。")

    def install(self) -> None:
        if self.plist.exists() or self.plist.is_symlink():
            self.owned_config()
        if self.launchctl("print", self.target, check=False).returncode == 0:
            # Never restart a loaded worker just because install was run twice.
            self.owned_config()
            print("常驻服务已加载，未重启。")
            return
        self.require_free_port()
        config = definition(self.root, self.data)
        if not Path(config["ProgramArguments"][0]).is_file():
            raise ServiceError("缺少 backend/.venv/bin/python，请先运行 make install。")
        if not (self.root / "frontend/dist/index.html").is_file():
            raise ServiceError("缺少网页构建，请先运行 make build。")
        (self.data / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)
        self.plist.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=self.plist.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                plistlib.dump(config, output)
                output.flush()
                os.fsync(output.fileno())
                temporary.replace(self.plist)
            finally:
                temporary.unlink(missing_ok=True)
        self.start()

    def start(self) -> None:
        self.owned_config()
        if self.launchctl("print", self.target, check=False).returncode == 0:
            print("常驻服务已加载，未重启。")
            return
        self.require_free_port()
        self.launchctl("enable", self.target)
        self.launchctl("bootstrap", self.domain, str(self.plist))
        print("常驻服务已加载：http://127.0.0.1:8000（启动状态用 make service-status 查看）。")

    def stop(self) -> None:
        self.owned_config()
        # Unload first: killing only the Python PID would trigger KeepAlive.
        if self.launchctl("print", self.target, check=False).returncode == 0:
            self.launchctl("bootout", self.target)
        # Persist an intentional stop across the next login; start enables it again.
        self.launchctl("disable", self.target)
        print("常驻服务已停止。已提交任务保留；再次启动后从短片页恢复。")

    def status(self) -> None:
        self.owned_config()
        result = self.launchctl("print", self.target, check=False)
        if result.returncode:
            print("常驻服务未加载。")
            return
        for line in result.stdout.splitlines():
            if line.strip().startswith(
                ("state =", "pid =", "runs =", "last exit code =", "last terminating signal =")
            ):
                print(line.strip())
        print(f"日志：{self.data / 'logs/service.stderr.log'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "start", "stop", "status"))
    args = parser.parse_args()
    try:
        if sys.platform != "darwin":
            raise ServiceError("此常驻入口仅支持 macOS；其他平台使用 make run 或系统服务管理器。")
        service = LocalService(ROOT, Settings().storage_root, Path.home(), os.getuid())
        getattr(service, args.action)()
    except (ServiceError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
