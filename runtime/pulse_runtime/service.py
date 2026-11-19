"""User-level background service registration (no admin rights required).

- macOS: launchd LaunchAgent ``~/Library/LaunchAgents/com.pulse.runtime.plist``
- Linux: ``systemd --user`` unit ``~/.config/systemd/user/pulse-runtime.service``
- Windows: Task Scheduler task ``PulseRuntime`` (logon trigger, current user, least privilege)

Each backend renders its definition with pure functions (unit tested) and shells out to the
OS tool for install/uninstall/status.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from pulse_runtime.gate.ledger import ENV_LEDGER_PATH
from pulse_runtime.paths import ENV_RUNTIME_DIR, ensure_private_dir, runtime_dir

LABEL = "com.pulse.runtime"
SYSTEMD_UNIT = "pulse-runtime.service"
WINDOWS_TASK = "PulseRuntime"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False)  # noqa: S603


def serve_command() -> list[str]:
    """Command line the service manager should execute to start the Runtime."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        # Windows: prefer the windowless twin so logon does not pop a console window.
        windowless = exe.with_name("pulse-runtimew.exe")
        if sys.platform == "win32" and windowless.exists():
            exe = windowless
        return [str(exe), "serve"]
    return [sys.executable, "-m", "pulse_runtime", "serve"]


def service_env() -> dict[str, str]:
    names = (
        ENV_RUNTIME_DIR,
        ENV_LEDGER_PATH,
        "PULSE_LOG_LEVEL",
        "PULSE_LOG_MAX_BYTES",
        "PULSE_LOG_BACKUP_COUNT",
        "PULSE_LOG_STDERR",
        "PULSE_METRICS_INTERVAL_S",
        "PULSE_ADB",
        "ANDROID_HOME",
        "ANDROID_SDK_ROOT",
        "PULSE_AGENT_APK",
    )
    return {name: value for name in names if (value := os.environ.get(name))}


@dataclass
class ServiceStatus:
    installed: bool
    running: bool
    detail: str


# ---------------------------------------------------------------- macOS launchd


def launchd_plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def render_launchd_plist(command: list[str], log_dir: Path, env: dict[str, str]) -> bytes:
    plist: dict[str, object] = {
        "Label": LABEL,
        "ProgramArguments": command,
        "RunAtLoad": True,
        # Restart after crashes; a clean exit (e.g. "already running") is left alone.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "launchd.out.log"),
        "StandardErrorPath": str(log_dir / "launchd.err.log"),
    }
    if env:
        plist["EnvironmentVariables"] = env
    return plistlib.dumps(plist)


class LaunchdBackend:
    def __init__(self, runner: Runner = run, home: Path | None = None) -> None:
        self.runner = runner
        self.home = home or Path.home()
        self.domain = f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/0"

    @property
    def path(self) -> Path:
        return launchd_plist_path(self.home)

    def install(self) -> str:
        log_dir = runtime_dir()
        ensure_private_dir(log_dir)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(render_launchd_plist(serve_command(), log_dir, service_env()))
        self.runner(["launchctl", "bootout", f"{self.domain}/{LABEL}"])
        res = self.runner(["launchctl", "bootstrap", self.domain, str(self.path)])
        if res.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap failed: {res.stderr.strip()}")
        return f"installed {self.path}"

    def uninstall(self) -> str:
        self.runner(["launchctl", "bootout", f"{self.domain}/{LABEL}"])
        self.path.unlink(missing_ok=True)
        return f"removed {self.path}"

    def status(self) -> ServiceStatus:
        res = self.runner(["launchctl", "print", f"{self.domain}/{LABEL}"])
        running = res.returncode == 0 and "state = running" in res.stdout
        detail = "loaded" if res.returncode == 0 else "not loaded"
        return ServiceStatus(self.path.exists(), running, detail)


# ---------------------------------------------------------------- Linux systemd --user


def systemd_unit_path(home: Path, env: dict[str, str] | None = None) -> Path:
    env = dict(os.environ) if env is None else env
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
    return base / "systemd" / "user" / SYSTEMD_UNIT


def render_systemd_unit(command: list[str], env: dict[str, str]) -> str:
    lines = [
        "[Unit]",
        "Description=Pulse Runtime (user-level local execution service)",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={shlex.join(command)}",
        "Restart=on-failure",
        "RestartSec=5",
        *[f'Environment="{k}={v}"' for k, v in env.items()],
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


class SystemdBackend:
    def __init__(self, runner: Runner = run, home: Path | None = None) -> None:
        self.runner = runner
        self.home = home or Path.home()

    @property
    def path(self) -> Path:
        return systemd_unit_path(self.home)

    def _ctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.runner(["systemctl", "--user", *args])

    def install(self) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(render_systemd_unit(serve_command(), service_env()), encoding="utf-8")
        self._ctl("daemon-reload")
        res = self._ctl("enable", "--now", SYSTEMD_UNIT)
        if res.returncode != 0:
            raise RuntimeError(f"systemctl enable failed: {res.stderr.strip()}")
        self._ctl("restart", SYSTEMD_UNIT)
        return f"installed {self.path} (to keep running after logout: loginctl enable-linger $USER)"

    def uninstall(self) -> str:
        self._ctl("disable", "--now", SYSTEMD_UNIT)
        self.path.unlink(missing_ok=True)
        self._ctl("daemon-reload")
        return f"removed {self.path}"

    def status(self) -> ServiceStatus:
        res = self._ctl("is-active", SYSTEMD_UNIT)
        state = res.stdout.strip() or "unknown"
        return ServiceStatus(self.path.exists(), state == "active", state)


# ---------------------------------------------------------------- Windows Task Scheduler


def render_windows_task(command: list[str], user: str) -> str:
    exe, *args = command
    arguments = subprocess.list2cmdline(args)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Pulse Runtime (user-level)</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Hidden>true</Hidden>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(exe)}</Command><Arguments>{escape(arguments)}</Arguments></Exec>
  </Actions>
</Task>
"""


class WindowsTaskBackend:
    def __init__(self, runner: Runner = run) -> None:
        self.runner = runner

    def install(self) -> str:
        user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
        xml = render_windows_task(serve_command(), user.strip("\\"))
        with tempfile.TemporaryDirectory() as tmp:
            xml_path = Path(tmp) / "task.xml"
            xml_path.write_text(xml, encoding="utf-16")
            res = self.runner(
                ["schtasks", "/Create", "/TN", WINDOWS_TASK, "/XML", str(xml_path), "/F"]
            )
        if res.returncode != 0:
            raise RuntimeError(f"schtasks /Create failed: {res.stderr.strip() or res.stdout}")
        self.runner(["schtasks", "/Run", "/TN", WINDOWS_TASK])
        return f"installed scheduled task {WINDOWS_TASK}"

    def uninstall(self) -> str:
        self.runner(["schtasks", "/End", "/TN", WINDOWS_TASK])
        self.runner(["schtasks", "/Delete", "/TN", WINDOWS_TASK, "/F"])
        return f"removed scheduled task {WINDOWS_TASK}"

    def status(self) -> ServiceStatus:
        res = self.runner(["schtasks", "/Query", "/TN", WINDOWS_TASK, "/FO", "LIST"])
        if res.returncode != 0:
            return ServiceStatus(False, False, "not installed")
        state = next(
            (
                ln.split(":", 1)[1].strip()
                for ln in res.stdout.splitlines()
                if ":" in ln and ln.split(":", 1)[0].strip() == "Status"
            ),
            "unknown",
        )
        return ServiceStatus(True, state.lower() == "running", state)


def backend_for(
    platform: str = sys.platform,
) -> LaunchdBackend | SystemdBackend | WindowsTaskBackend:
    if platform == "darwin":
        return LaunchdBackend()
    if platform == "win32":
        return WindowsTaskBackend()
    if platform.startswith("linux"):
        return SystemdBackend()
    raise RuntimeError(f"unsupported platform: {platform}")
