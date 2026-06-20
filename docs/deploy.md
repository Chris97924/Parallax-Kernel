# Parallax Deploy Guide

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed and running
- One of: [flyctl](https://fly.io/docs/hands-on/install-flyctl/) or [Railway CLI](https://docs.railway.app/develop/cli)

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `PARALLAX_TOKEN` | **Critical** | Bearer token for API authentication. Set before exposing publicly. |
| `PARALLAX_MULTI_USER` | Multi-tenant only | Set to `1` to enable per-user token isolation (see "Auth modes & tenancy"). Off by default. |
| `PARALLAX_DB_PATH` | Recommended | Path to SQLite database file (default: in-memory / cwd). |
| `PARALLAX_VAULT_PATH` | Recommended | Path to vault directory for file storage. |

**Never expose the server publicly without setting `PARALLAX_TOKEN`.**
When unset, the server runs in open mode (localhost dev only).

### Auth modes & tenancy

Parallax has three auth modes, and **only multi-user mode is a tenant boundary**:

| Mode | Config | `?user_id` handling | Isolation guarantee |
|---|---|---|---|
| **Open** | no token | client-supplied value used as-is | none — localhost dev only |
| **Single-token** | `PARALLAX_TOKEN` set, `PARALLAX_MULTI_USER` off | client-supplied value used as-is | **single trust domain** — any holder of the shared token can address any `user_id` |
| **Multi-user** | `PARALLAX_TOKEN` set, `PARALLAX_MULTI_USER=1` | ignored; principal is bound from the token's `api_tokens` row | per-user — a token can only ever read/write its own `user_id` |

In **single-token mode** (the default once `PARALLAX_TOKEN` is set), the
`?user_id` query param / body field is **not** an access-control boundary: it
selects which user's data to read or write, and the single shared token
authorizes all of them. This is correct for a single-tenant deployment (one
person / one trust domain). **Do not treat a single shared token as separation
between distinct tenants.** For genuine per-user isolation — distinct tenants who
must not see each other's data — issue one token per tenant in the `api_tokens`
table and run with `PARALLAX_MULTI_USER=1`.

---

## Fly.io

### First deploy

```bash
# 1. Launch — copies config from deploy/fly.toml, uses Dockerfile at repo root
fly launch --copy-config --dockerfile Dockerfile

# 2. Create persistent volume (1 GB)
fly volumes create parallax_data --size 1

# 3. Set the auth token as a secret
fly secrets set PARALLAX_TOKEN=<your-token>

# 4. Deploy
fly deploy
```

### Subsequent deploys

```bash
fly deploy
```

### Useful commands

```bash
fly logs          # tail live logs
fly status        # machine health
fly ssh console   # shell into the running machine
```

---

## Railway

### Deploy

```bash
# 1. Initialise project (run from repo root)
railway init

# 2. Set the auth token
railway variables set PARALLAX_TOKEN=<your-token>

# 3. Also set data paths if you have a volume/mount configured
railway variables set PARALLAX_DB_PATH=/data/parallax.sqlite
railway variables set PARALLAX_VAULT_PATH=/data/vault

# 4. Deploy
railway up
```

Railway auto-detects the `Dockerfile` at the repo root. The `deploy/railway.json`
template provides the healthcheck path and start command.

---

## Generic Docker

```bash
# Build the image
docker build -t parallax .

# Create a named volume for persistent data
docker volume create parallax_data

# Run
docker run \
  -e PARALLAX_TOKEN=<your-token> \
  -e PARALLAX_DB_PATH=/data/parallax.sqlite \
  -e PARALLAX_VAULT_PATH=/data/vault \
  -v parallax_data:/data \
  -p 8080:8080 \
  parallax
```

The server will be available at `http://localhost:8080`.
Liveness probe: `GET /healthz` — returns `{"status": "ok"}` when healthy.

---

## Cloud Backup / Restore (S3-compatible)

Parallax supports uploading backups to any S3-compatible object store (AWS S3,
Backblaze B2, Cloudflare R2, MinIO, etc.).

### Install the cloud extra

```bash
pip install 'parallax-kernel[cloud]'
```

This adds `boto3` as a dependency. Without it, using an `s3://` destination
produces a clear error rather than a silent failure.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | Yes (S3) | Access key ID for your S3-compatible provider. |
| `AWS_SECRET_ACCESS_KEY` | Yes (S3) | Secret access key for your S3-compatible provider. |
| `AWS_DEFAULT_REGION` | Recommended | AWS region (e.g. `us-east-1`). Defaults to boto3 default. |
| `AWS_ENDPOINT_URL` | Optional | Custom endpoint URL for non-AWS providers (see below). |

### Usage

```bash
# Back up and upload to S3 (local archive removed after upload)
parallax backup /tmp/parallax-backup.tar.gz --to s3://my-bucket/backups/latest.tar.gz

# Restore directly from S3 (downloads to local path first, then restores)
parallax restore /tmp/parallax-restore.tar.gz --from s3://my-bucket/backups/latest.tar.gz
```

### Custom endpoint (Backblaze B2, Cloudflare R2, MinIO)

Set `AWS_ENDPOINT_URL` to point boto3 at your provider's S3-compatible API:

```bash
# Backblaze B2
export AWS_ENDPOINT_URL=https://s3.us-west-004.backblazeb2.com

# Cloudflare R2
export AWS_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com

# Local MinIO
export AWS_ENDPOINT_URL=http://localhost:9000
```

Then run `parallax backup` / `parallax restore` with an `s3://bucket/key` URI as normal.
