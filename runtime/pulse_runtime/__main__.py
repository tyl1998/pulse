"""Pulse Runtime CLI.

pulse-runtime serve                       run the local API in the foreground
pulse-runtime health                      probe the running instance via runtime.json
pulse-runtime openapi                     print the local API OpenAPI document
pulse-runtime service install|uninstall|status
pulse-runtime run <project> --case --device [--strategy ...]
pulse-runtime run-suite <project> --device --case ... [--repeat N]
pulse-runtime --version

``run`` is the headless P1.10 entry: it executes one case on one device and returns a
terminal-state exit code (0 passed, 1 failed, 2 blocked, 3 interrupted, 4 canceled,
5 selection refused, 6 project unusable, 7 bad arguments).
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import sys

from pulse_runtime import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pulse-runtime")
    parser.add_argument("-V", "--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the loopback API in the foreground")
    sub.add_parser("health", help="probe the running Runtime")
    sub.add_parser("openapi", help="print the local API OpenAPI document")
    svc = sub.add_parser("service", help="manage the user-level background service")
    svc.add_argument("action", choices=["install", "uninstall", "status"])
    return parser


def cmd_health() -> int:
    from pulse_runtime.paths import connection_file, read_connection
    from pulse_runtime.probe import probe_health

    path = connection_file()
    info = read_connection(path)
    if info is None:
        print(json.dumps({"running": False, "connection_file": str(path)}))
        return 1
    health = probe_health(info)
    print(json.dumps({"running": health is not None, "port": info.port, "health": health}))
    return 0 if health is not None else 1


def cmd_openapi() -> int:
    from pulse_runtime.api.app import create_app

    document = create_app("openapi-export").openapi()
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_service(action: str) -> int:
    from pulse_runtime.service import backend_for

    backend = backend_for()
    if action == "install":
        print(backend.install())
    elif action == "uninstall":
        print(backend.uninstall())
    else:
        st = backend.status()
        print(json.dumps({"installed": st.installed, "running": st.running, "detail": st.detail}))
        return 0 if st.running else 3
    return 0


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    raw = list(sys.argv[1:] if argv is None else argv)
    # ``run`` owns its own parser so every refusal (including bad flags) prints one JSON
    # document on stdout with a selection/invocation exit code.
    if raw and raw[0] in {"run", "run-suite"}:
        from pulse_runtime.engine import cli

        return cli.main(raw[1:]) if raw[0] == "run" else cli.suite(raw[1:])
    args = build_parser().parse_args(raw)
    if args.version:
        print(f"pulse-runtime {__version__}")
        return 0
    if args.command == "serve":
        from pulse_runtime.server import serve

        return serve()
    if args.command == "health":
        return cmd_health()
    if args.command == "openapi":
        return cmd_openapi()
    if args.command == "service":
        return cmd_service(args.action)
    build_parser().print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
