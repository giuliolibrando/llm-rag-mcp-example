# README
This repository contains the code used in the Medium article  
"Harness the Power of RAG and MCP: How On-Premise AI Manages Your Entire IT Infrastructure":  
https://medium.com/p/5d204649aeee


## Overview

This stack deploys a local LLM platform with RAG (Retrieval Augmented Generation) and MCP capalbilities. It consists of:

- **AnythingLLM**: Web UI for multi-user chat
- **Ollama (3 replicas)**: LLM inference (CPU-only in this setup)
- **Nginx Router**: load balancing across Ollama replicas
- **Qdrant**: Vector database to store embeddings
- **RAG Ingestor**: Python scripts that extract data from Redmine and Wiki.js, embed it, and push it into Qdrant
- **Nginx Proxy Manager (NPM)**: simple reverse proxy and TLS termination

## Directory Structure

```
.
├─ docker-compose.yml
├─ .env
├─ router/
│   └─ nginx.conf
├─ npm/
│   ├─ data/              (created by NPM)
│   └─ letsencrypt/       (created by NPM)
└─ rag-ingestor/
    ├─ requirements.txt
    ├─ run_cron.sh
    ├─ rag_common.py
    ├─ ingest_redmine.py
    └─ ingest_wikijs.py
```

## Environment Variables

The `.env` file defines configuration. Example:

```
TZ=Europe/Rome
ALLM_JWT_SECRET=replace_with_long_random_string

# Qdrant
QDRANT_API_KEY=

# Redmine
REDMINE_URL=https://redmine.local
REDMINE_TOKEN=xxxxxxxx
REDMINE_PROJECTS=infra,platform
REDMINE_LOOKBACK_DAYS=14
REDMINE_COLLECTION=redmine

# Wiki.js
WIKIJS_URL=https://wiki.local
WIKIJS_TOKEN=yyyyyyyy
WIKIJS_SPACES=*
WIKIJS_COLLECTION=wikijs

# Embeddings
EMBEDDING_MODEL=intfloat/e5-large-v2

# RAG Ingestor
STATE_DIR=/app/state
UPSERT_BATCH=64
HTTP_TIMEOUT=40
DEFAULT_SCHEDULE=*/30 * * * *
```

### Notes
- `ALLM_JWT_SECRET`: required. Generate a long random string.
- `REDMINE_TOKEN`: API key from Redmine.
- `WIKIJS_TOKEN`: API token from Wiki.js.
- `REDMINE_PROJECTS`: comma-separated list of projects to index.
- `*_COLLECTION`: collection names in Qdrant, can be changed.
- `QDRANT_API_KEY`: leave empty unless you enable API key protection in Qdrant.

## First Run

1. Create `.env` with the proper values.
2. Launch the stack:
   ```
   docker compose up -d
   ```
3. Wait until containers are up (`docker ps`).

## Pull Models in Ollama

Inside `ollama1` container, pull models:
```
docker exec -it ollama1 bash -lc "ollama pull llama3.1:8b-instruct-q4_K_M"
docker exec -it ollama1 bash -lc "ollama pull qwen2.5:7b-instruct-q4_K_M"
```
Replicas `ollama2` and `ollama3` will download models when first used.

## Configure Nginx Proxy Manager (NPM)

1. Access dashboard at `http://<VM_IP>:81`.
2. Default credentials:  
   - Email: `admin@example.com`  
   - Password: `changeme` (you will be asked to reset).
3. Add Proxy Hosts:
   - **AnythingLLM**:  
     - Domain: `llm.local`  
     - Forward Host: `anythingllm`  
     - Forward Port: `3001`
   - **Qdrant**:  
     - Domain: `qdrant.local`  
     - Forward Host: `qdrant`  
     - Forward Port: `6333`
   - **Ollama Router** (optional):  
     - Domain: `ollama.local`  
     - Forward Host: `llm-router`  
     - Forward Port: `80`
4. Enable Websockets and "Block Common Exploits" in each Proxy Host.
5. Configure SSL (either Let's Encrypt if domains are resolvable externally, or import your own certificates).

If you only have the VM IP and no DNS, you can configure **Custom Locations** in NPM and access via `https://<IP>/ui`, `https://<IP>/qdrant`, etc.

## Using the System

- Access AnythingLLM via the domain or IP path you configured.
- Log in and create a Workspace.
- Ensure Vector Database is set to Qdrant (`http://qdrant:6333` inside Docker network).
- The RAG Ingestor will run periodically (by default every 30 minutes, or according to `# SCHEDULE=` directives in the scripts) and populate Qdrant with Redmine and Wiki.js content.

## Verifying

Check Qdrant collections:
```
curl http://<VM_IP>:6333/collections
```

Check logs of ingestor:
```
docker logs -f rag-ingestor
```

Check AnythingLLM API:
```
curl http://<VM_IP>:3001/ --head
```

Check Ollama router:
```
curl http://<VM_IP>:11434/api/tags
```

## Data Persistence

- `./npm/data` and `./npm/letsencrypt`: Nginx Proxy Manager config and certs
- `./ollama*`: Ollama models and cache
- `./qdrant_storage`: Qdrant database
- `./anythingllm_data`: AnythingLLM workspace storage
- `./rag-ingestor/state`: incremental state for ETL jobs

## Security

- Change all default credentials (NPM, AnythingLLM).
- Protect NPM dashboard (`:81`) with firewall or VPN.
- Expose only ports 80/443/81 from the VM.
- Use internal DNS names and certificates from your corporate CA if possible.
