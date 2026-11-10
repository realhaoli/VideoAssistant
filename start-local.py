"""Start Video Assistant on Windows, macOS or Linux using the local venv."""
import argparse
import contextlib
import importlib
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def venv_python():
    return ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def stop_process(process):
    if os.name == "nt":
        if process.poll() is None:
            # Terminate Node/Chromium descendants too, not just their parent.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()


def serve(commands, env):
    processes = []
    try:
        with contextlib.ExitStack() as files:
            for name, command, logfile in commands:
                output = files.enter_context(logfile.open("ab")) if logfile else None
                options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=output,
                                           stderr=subprocess.STDOUT if output else None, **options)
                processes.append((name, process))
            while True:
                for name, process in processes:
                    if process.poll() is not None:
                        print(f"{name} stopped (exit {process.returncode}). Check the terminal or logs/.", flush=True)
                        return process.returncode or 1
                time.sleep(.2)
    except KeyboardInterrupt:
        return 0
    finally:
        for _, process in reversed(processes):
            stop_process(process)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", action="store_true", help="run the Vite dev server too")
    parser.add_argument("--host", help="listen address; default: HOST or 0.0.0.0")
    parser.add_argument("--port", type=int, help="backend port; default: PORT or 8080")
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument("--install-browser", action="store_true", help="install the local Chromium headless shell")
    parser.add_argument("--with-deps", action="store_true", help="also install Linux browser system dependencies")
    parser.add_argument("--check", action="store_true", help="check runtime dependencies without starting services")
    args = parser.parse_args(argv)
    python = venv_python()
    if not python.is_file():
        parser.exit(1, "Missing .venv. Create a Python 3.12 virtual environment and install backend/requirements.txt.\n")
    if Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        os.execv(str(python), [str(python), str(Path(__file__).resolve()), *(sys.argv[1:] if argv is None else argv)])
    os.chdir(ROOT)
    try:
        from dotenv import load_dotenv
    except ImportError:
        parser.exit(1, "Missing dependencies. Run: python -m pip install -r backend/requirements.txt\n")
    load_dotenv(ROOT / "variables.env", encoding="utf-8-sig")
    sys.path.insert(0, str(ROOT / "backend"))
    from local.paths import configure_browser_path
    configure_browser_path()
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
    if args.with_deps and not args.install_browser:
        parser.error("--with-deps requires --install-browser")
    if args.install_browser:
        return subprocess.call([str(python), "-m", "playwright", "install", "chromium", "--only-shell",
                                *(["--with-deps"] if args.with_deps else [])], env=env, cwd=ROOT)
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    missing += [name for name in ("uvicorn", "fastapi", "faster_whisper", "playwright", "opencc") if importlib.util.find_spec(name) is None]
    if missing:
        parser.exit(1, "Missing runtime dependencies: " + ", ".join(missing) + ". See README.md.\n")
    if args.check:
        try:
            for module in ("faster_whisper", "av", "opencc"):
                importlib.import_module(module)
        except (ImportError, OSError) as exc:
            parser.exit(1, f"Cannot load native media dependencies: {exc}\nOn Windows, install the Visual C++ x64 runtime.\n")
        print("Runtime dependencies found. Browser directory:", env["PLAYWRIGHT_BROWSERS_PATH"])
        print("Build the frontend and run --install-browser before first use.")
        return 0
    host = args.host or os.getenv("HOST", "0.0.0.0")
    port = args.port if args.port is not None else int(os.getenv("PORT", "8080"))
    if not 1 <= port <= 65535 or not 1 <= args.frontend_port <= 65535:
        parser.error("ports must be between 1 and 65535")
    backend = [str(python), "-m", "uvicorn", "app:app", "--app-dir", str(ROOT / "backend"), "--host", host, "--port", str(port)]
    if args.dev:
        vite = ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
        node = shutil.which("node")
        if not node or not vite.is_file():
            parser.exit(1, "Install Node.js 22+ and run: npm --prefix frontend ci\n")
        (ROOT / "logs").mkdir(exist_ok=True)
        env["BACKEND_PORT"] = str(port)
        commands = [("backend", backend, ROOT / "logs" / "backend.log"),
                    ("frontend", [node, str(vite), str(ROOT / "frontend"), "--host", host,
                                  "--port", str(args.frontend_port), "--strictPort"], ROOT / "logs" / "frontend.log")]
        print(f"Development: http://localhost:{args.frontend_port} (logs/)", flush=True)
    else:
        if not all((ROOT / "frontend" / "dist" / name).is_file() for name in ("index.html", "mindmap-renderer.js")):
            parser.exit(1, "Build the frontend first: npm --prefix frontend ci && npm --prefix frontend run build\n")
        commands = [("backend", backend, None)]
        print(f"Video Assistant: http://localhost:{port}", flush=True)
    def interrupt(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    return serve(commands, env)


if __name__ == "__main__":
    raise SystemExit(main())
