#!/usr/bin/env python3
"""Start the backend and the frontend together.

One command, because two terminals is two chances to start only one of them and
then debug a UI that cannot reach its API. Ctrl-C stops both.

    python3 run.py                 # API :8010, web :5173
    python3 run.py --api-port 9000
    python3 run.py --build         # rebuild the specs from the PDFs first
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
CALCULATORS = ROOT / "calculators"
EXTRACTOR = ROOT / "backup" / "extractor"

C = {"ok": "\033[92m", "bad": "\033[91m", "warn": "\033[93m",
     "accent": "\033[38;5;208m", "dim": "\033[90m", "b": "\033[1m", "r": "\033[0m"}


def say(msg: str) -> None:
    print(f"{C['accent']}▸{C['r']} {msg}", flush=True)


def fail(msg: str) -> int:
    print(f"{C['bad']}✗{C['r']} {msg}", file=sys.stderr)
    return 1


def free_port(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def ensure_backend() -> Path:
    """The venv, created on first run so `python3 run.py` is the whole setup."""
    venv = BACKEND / ".venv"
    python = venv / "bin" / "python"
    if not python.exists():
        say("creating the backend virtualenv (first run only)")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run(
            [str(venv / "bin" / "pip"), "install", "-q", "-r",
             str(BACKEND / "requirements.txt")],
            check=True,
        )
    return python


def ensure_frontend() -> None:
    if (FRONTEND / "node_modules").is_dir():
        return
    if not shutil.which("npm"):
        raise SystemExit(fail("npm is not installed; install Node.js to run the frontend"))
    say("installing frontend dependencies (first run only)")
    subprocess.run(["npm", "install"], cwd=FRONTEND, check=True)


def rebuild_specs() -> None:
    pdfs = ROOT / "backup" / "source-pdfs"
    if not pdfs.is_dir():
        raise SystemExit(fail(f"no source PDFs at {pdfs}"))
    say("rebuilding the specs from the source PDFs")
    subprocess.run([sys.executable, "build_all.py", str(pdfs)], cwd=EXTRACTOR, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-port", type=int, default=8010)
    ap.add_argument("--web-port", type=int, default=5173)
    ap.add_argument("--build", action="store_true",
                    help="rebuild the specs from the PDFs before starting")
    ap.add_argument("--api-only", action="store_true")
    ap.add_argument("--web-only", action="store_true")
    args = ap.parse_args()

    if args.build:
        rebuild_specs()

    n_specs = len([p for p in CALCULATORS.glob("*.json")
                   if p.name not in ("index.json", "categories.json")])
    if not n_specs:
        return fail(
            f"no calculators in {CALCULATORS}. Build them with:\n"
            f"  python3 run.py --build"
        )

    procs: list[tuple[str, subprocess.Popen]] = []

    if not args.web_only:
        if not free_port(args.api_port):
            return fail(f"port {args.api_port} is in use — try --api-port 8011")
        python = ensure_backend()
        procs.append((
            "api",
            subprocess.Popen(
                [str(python), "-m", "uvicorn", "app.main:app",
                 "--host", "127.0.0.1", "--port", str(args.api_port), "--reload"],
                cwd=BACKEND,
            ),
        ))

    if not args.api_only:
        if not free_port(args.web_port):
            return fail(f"port {args.web_port} is in use — try --web-port 5174")
        ensure_frontend()
        env = dict(os.environ, VITE_API_BASE=f"http://127.0.0.1:{args.api_port}")
        procs.append((
            "web",
            subprocess.Popen(
                ["npm", "run", "dev", "--", "--port", str(args.web_port), "--strictPort"],
                cwd=FRONTEND,
                env=env,
            ),
        ))

    time.sleep(1.2)
    print(f"\n{C['b']}  Calculator{C['r']}  {C['dim']}{n_specs} clinical calculators{C['r']}")
    if not args.web_only:
        print(f"  {C['accent']}API{C['r']}  http://127.0.0.1:{args.api_port}/docs")
    if not args.api_only:
        print(f"  {C['accent']}Web{C['r']}  http://localhost:{args.web_port}")
    print(f"\n{C['dim']}  Ctrl-C stops both.{C['r']}\n")

    def stop(*_: object) -> None:
        for name, p in procs:
            if p.poll() is None:
                p.terminate()
        for _name, p in procs:
            try:
                p.wait(timeout=6)
            except subprocess.TimeoutExpired:
                p.kill()

    signal.signal(signal.SIGINT, lambda *_: (stop(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *_: (stop(), sys.exit(0)))

    try:
        # If either process dies, take the other one down rather than leaving a
        # half-running system that looks fine until the first request.
        while True:
            for name, p in procs:
                code = p.poll()
                if code is not None:
                    print(f"\n{C['bad']}✗{C['r']} {name} exited ({code}); stopping the rest")
                    stop()
                    return code or 1
            time.sleep(0.4)
    except KeyboardInterrupt:
        stop()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
