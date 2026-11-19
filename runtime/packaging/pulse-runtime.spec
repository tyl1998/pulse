# PyInstaller onedir spec for Pulse Runtime.
# Build:  uv run --group package pyinstaller runtime/packaging/pulse-runtime.spec \
#           --distpath dist/runtime --workpath build/pyinstaller --noconfirm
# Output: dist/runtime/pulse-runtime/pulse-runtime[.exe]  (+ pulse-runtimew.exe on Windows)
import sys

from PyInstaller.utils.hooks import collect_submodules

here = SPECPATH  # noqa: F821 - injected by PyInstaller

# uvicorn/websockets load protocol implementations by import string.
hidden = (
    collect_submodules("uvicorn")
    + collect_submodules("websockets")
    + collect_submodules("pulse_runtime")
)

a = Analysis(  # noqa: F821
    [f"{here}/entry.py"],
    pathex=[f"{here}/.."],
    hiddenimports=hidden,
    # Device-side resources (scrcpy-server, clock probe) + pins; fetched by fetch_resources.py.
    datas=[
        (
            f"{here}/../pulse_runtime/drivers/android/resources",
            "pulse_runtime/drivers/android/resources",
        )
    ],
    excludes=["tkinter", "pytest", "mypy"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exes = [
    EXE(  # noqa: F821
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="pulse-runtime",
        console=True,
        upx=False,
    )
]
if sys.platform == "win32":
    # Windowless twin used by the scheduled task so logon does not open a console window.
    exes.append(
        EXE(  # noqa: F821
            pyz,
            a.scripts,
            [],
            exclude_binaries=True,
            name="pulse-runtimew",
            console=False,
            upx=False,
        )
    )

COLLECT(*exes, a.binaries, a.datas, name="pulse-runtime", upx=False)  # noqa: F821
