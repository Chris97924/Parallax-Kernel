# Installing Parallax

## Recommended: pipx (end-user CLI + server)

For running the `parallax` CLI and server as a tool (not as a library dependency), pipx is the recommended path. It installs Parallax in an isolated environment and puts the `parallax` command on your PATH.

```bash
pipx install 'parallax-kernel[server]'

# Verify
parallax --help

# Start the server
parallax serve --host 127.0.0.1 --port 8765
```

## From source (development + editing code)

Use this path when you want to modify the code, run the test suite, or contribute.

```bash
# 1. Clone
git clone https://github.com/<your-user>/parallax-kernel.git
cd parallax-kernel

# 2. Create a virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

# 3. Install in editable mode with dev extras
# (Use double quotes on Windows PowerShell / cmd)
pip install -e ".[dev]"

# 4. Bootstrap a fresh Parallax instance
python bootstrap.py /tmp/my-parallax

# 5. Run the test suite
pytest
```

To also install the FastAPI server:

```bash
pip install -e ".[dev,server]"
```

## Docker

For containerised deployment, see [Deploy](deploy.md). The Dockerfile at the repo root builds a self-contained image:

```bash
docker build -t parallax .
docker run -e PARALLAX_TOKEN=<your-token> -p 8080:8080 parallax
```

Full instructions for Fly.io, Railway, and generic Docker are in [Deploy](deploy.md).

## Python version

Parallax requires **Python 3.11 or later**. GitHub Actions CI runs on Python 3.11.

## Extras

| Extra | Installs | Use when |
|---|---|---|
| `[dev]` | pytest, ruff, hypothesis, coverage | Development and testing |
| `[server]` | FastAPI, uvicorn | Running `parallax serve` |
| `[extract]` | httpx, anthropic | Shadow-write claim extraction |
| `[cloud]` | boto3 | S3-backed backup/restore |
| `[llm]` | google-genai | Calling Gemini models via `parallax.llm.call` / longmemeval eval |
