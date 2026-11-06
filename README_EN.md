# Video Assistant — Local Chinese Briefing Workspace

The current version provides illustrated briefings, text briefings, mind maps, Chinese subtitles and highest-quality MP4 downloads with switchable Chinese/English subtitle tracks.

All task state, media, subtitles, model weights and exported files are stored locally. The only remote processing is the configured language/multimodal model API. No OSS, database or cloud ASR is required.

The server listens on `0.0.0.0:8080` with no login. Tasks and files are shared, while each job has its own directory, model settings snapshot and checkpoints. Two jobs run concurrently by default (`TASK_WORKERS`), with global limits for model calls, Whisper and PDF export. Submitted jobs continue after the browser closes. Mind map PDFs are generated on the server using the original bundled renderer and local Chromium.

See the maintained [Chinese installation and startup guide](README.md) for setup, usage and limitations.
