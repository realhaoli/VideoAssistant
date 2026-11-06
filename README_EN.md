# Video Assistant — Local Chinese Briefing Workspace

The current version provides illustrated briefings, text briefings, mind maps, Chinese subtitles and highest-quality MP4 downloads with switchable Chinese/English subtitle tracks.

All task state, media, subtitles, model weights and exported files are stored locally. The only remote processing is the configured language/multimodal model API. No OSS, database or cloud ASR is required.

The server listens on `0.0.0.0:8080` with no login. Tasks and files are shared, while each job has its own directory, model settings snapshot and checkpoints. Two jobs run concurrently by default (`TASK_WORKERS`), with global limits for model calls, Whisper and PDF export. Submitted jobs continue after the browser closes. Mind map PDFs are generated on the server using the original bundled renderer and local Chromium.

See the maintained [Chinese installation and startup guide](README.md) for setup, usage and limitations.

Windows, macOS and Linux share the `start-local.py` launcher. Use `start-local.cmd` on Windows and `./start-local.sh` on macOS/Linux; append `--dev` for development or `--install-browser` to install Chromium in the configured local data directory. Recreate `.venv`, `node_modules` and browser binaries on each target OS. Target Python 3.12, Node.js 22+, FFmpeg/ffprobe and the OS versions supported by Playwright; Windows requires x64 Python and the Visual C++ runtime.

The platform CI workflow builds and tests on all three operating systems, including real FFmpeg and Chromium PDF exports. Local tests on one system do not establish that all three CI jobs passed.
