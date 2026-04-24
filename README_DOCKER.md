# Docker / Docker Compose (local-only)

This repository includes Docker / Docker Compose settings for running the Streamlit-based audiometry screening app locally in a stable way.

This README assumes the following files are present at the repository root:

- `Dockerfile`
- `docker-compose.yml`
- `docker-compose.dev.yml`
- `.dockerignore`
- `requirements.txt`
- `audiometry_app.py`

## 1. Standard Local Run

```bash
docker compose up --build -d
```

Open:

```text
http://localhost:60000
```

Stop:

```bash
docker compose down
```

In this mode, the Streamlit file watcher is disabled inside the container to prioritize stable fixed behavior for routine local use.

## 2. Development Mode (live edit)

```bash
docker compose -f docker-compose.dev.yml up --build
```

In this mode, the current directory is mounted into the container, so local file edits are more likely to be reflected without rebuilding the image.

Stop:

```bash
docker compose -f docker-compose.dev.yml down
```

## Notes

- The host binds to `127.0.0.1:60000`, so the app is not exposed to the local network.
- Inside the container, Streamlit listens on `0.0.0.0:8501`.
- Standard mode uses `--server.fileWatcherType=none`, while development mode uses `auto`.
- The image is based on `python:3.11-slim` and installs dependencies from `requirements.txt`.
