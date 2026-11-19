import plistlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from pulse_runtime.service import (
    LABEL,
    SystemdBackend,
    WindowsTaskBackend,
    render_launchd_plist,
    render_systemd_unit,
    render_windows_task,
    service_env,
    systemd_unit_path,
)

CMD = ["/opt/Pulse Runtime/pulse-runtime", "serve"]


def test_launchd_plist() -> None:
    data = plistlib.loads(render_launchd_plist(CMD, Path("/logs"), {"PULSE_RUNTIME_DIR": "/x"}))
    assert data["Label"] == LABEL
    assert data["ProgramArguments"] == CMD
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["EnvironmentVariables"] == {"PULSE_RUNTIME_DIR": "/x"}
    assert data["StandardErrorPath"] == "/logs/launchd.err.log"


def test_systemd_unit() -> None:
    unit = render_systemd_unit(CMD, {"PULSE_RUNTIME_DIR": "/x"})
    assert "ExecStart='/opt/Pulse Runtime/pulse-runtime' serve" in unit
    assert "Restart=on-failure" in unit
    assert 'Environment="PULSE_RUNTIME_DIR=/x"' in unit
    assert "WantedBy=default.target" in unit
    assert systemd_unit_path(Path("/h"), {}) == Path(
        "/h/.config/systemd/user/pulse-runtime.service"
    )


def test_windows_task_xml_escapes() -> None:
    xml = render_windows_task([r"C:\P & Q\pulse-runtimew.exe", "serve"], r"DOM\u<s>")
    assert r"<Command>C:\P &amp; Q\pulse-runtimew.exe</Command>" in xml
    assert "<Arguments>serve</Arguments>" in xml
    assert r"<UserId>DOM\u&lt;s&gt;</UserId>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
    assert "<LogonTrigger>" in xml


class FakeRunner:
    def __init__(self, stdout: str = "", rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout
        self.rc = rc

    def __call__(self, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), self.rc, self.stdout, "")


def test_systemd_status_parses_is_active(tmp_path: Path) -> None:
    runner = FakeRunner(stdout="active\n")
    st = SystemdBackend(runner, home=tmp_path).status()
    assert st.running
    assert runner.calls == [["systemctl", "--user", "is-active", "pulse-runtime.service"]]


def test_windows_status_parses_list_output() -> None:
    out = "Folder: \\\nTaskName: \\PulseRuntime\nStatus:   Running\n"
    st = WindowsTaskBackend(FakeRunner(stdout=out)).status()
    assert st.installed
    assert st.running
    assert not WindowsTaskBackend(FakeRunner(rc=1)).status().installed


def test_service_env_only_persists_runtime_logging_and_device_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    values = {
        "PULSE_RUNTIME_DIR": str(tmp_path / "runtime"),
        "PULSE_LOG_LEVEL": "WARNING",
        "PULSE_LOG_MAX_BYTES": "1048576",
        "PULSE_LOG_BACKUP_COUNT": "2",
        "PULSE_LOG_STDERR": "0",
        "PULSE_METRICS_INTERVAL_S": "60",
        "PULSE_ADB": "/sdk/platform-tools/adb",
        "ANDROID_HOME": "/sdk",
        "ANDROID_SDK_ROOT": "/sdk",
        "PULSE_AGENT_APK": "/artifacts/agent-debug.apk",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PULSE_RELAY_TOKEN", "must-not-be-persisted")
    assert service_env() == values
