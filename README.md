# Enterprise RAG Model Platform

> **Status:** Sprint 0 Verified & Ready for Sprint 1  
> **Team:** Aether Link Tech  
> **Repository:** [Aether-Link-Tech/RAG-Model](https://github.com/Aether-Link-Tech/RAG-Model)

---

## 🏛️ Architecture Overview

| Component | Technology | Version | Deployment | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| **Backend API** | FastAPI + Uvicorn | `0.142+` / `0.161` | Local Host (Port `8000`) | Core REST APIs, OpenAPI docs |
| **Database** | PostgreSQL | `17-alpine` | Docker Compose (Port `5432`) | Primary transactional database with RLS |
| **Broker & Cache** | Redis | `7-alpine` | Docker Compose (Port `6379`) | Task queue broker for Celery and caching |
| **Vector Database** | ChromaDB | `1.5.3` / `v1.0.0` | Docker Compose (Port `8001`) | Vector search & embeddings storage |
| **Frontend UI** | React 19 + TypeScript | `19.2+` | Local Host (Port `3000`) | User interface & Admin dashboard |
| **Styling & Build** | Tailwind CSS 4 + Vite 8 | `4.x` / `8.x` | Local Host | High-performance bundling & styling |
| **Local LLM** | Ollama (`qwen3:4b`) | `0.40.9` | Local Host (Port `11434`) | Offline generation (CPU / Metal GPU) |
| **Local Embeddings**| Ollama (`nomic-embed-text`) | `768-dim` | Local Host | Dense vector document embeddings |

---

## ✅ Sprint 0 Setup Checklist (Section 11)

- [x] **1. Install Python 3.12** — Verified (`v3.12.14` in `.venv`)
- [x] **2. Install Node.js 22 LTS** — Verified (`npm` and `node` active)
- [x] **3. Install Docker Desktop** — Verified (`Docker 29.8` running)
- [x] **4. Install Ollama** — Verified (`0.40.9` installed with Metal acceleration)
- [x] **5. ollama pull qwen3:4b** — Target local LLM (or API fallback)
- [x] **6. ollama pull nomic-embed-text** — Target embedding model
- [x] **7. git clone <repo>** — Local repository workspace initialized
- [x] **8. docker compose up** — Postgres 17, Redis 7, ChromaDB up & healthy
- [x] **9. Check localhost:8000/docs** — FastAPI Swagger documentation live
- [x] **10. Check localhost:3000** — React 19 + Vite 8 dashboard live

---

## 🚀 Quickstart Guide

### 1. Start Infrastructure (Docker)
Postgres 17, Redis 7, and ChromaDB must be run via Docker:
```bash
docker compose up -d
```
Check health:
```bash
docker compose ps
```

### 2. Run Backend (FastAPI)
Activate the virtual environment and start the uvicorn development server:
```bash
# Using existing .venv
source .venv/bin/activate
uvicorn main:app --reload --port 8000 --host 0.0.0.0
```
Open **[http://localhost:8000/docs](http://localhost:8000/docs)** to test interactive API documentation.  
Open **[http://localhost:8000/api/health](http://localhost:8000/api/health)** to verify real-time status of all connected services.

### 3. Run Frontend (React + Vite)
```bash
cd frontend
npm install
npm run dev
```
Open **[http://localhost:3000](http://localhost:3000)** to view the live Sprint 0 System Status Dashboard.

### 4. Local AI Models (Ollama)
```bash
# Start local Ollama server (if not already running)
ollama serve

# Pull required models
ollama pull nomic-embed-text
ollama pull qwen3:4b
```

---

## ⚙️ Environment Variables (`.env`)

Copy `.env.example` to `.env` to configure ports and credentials:
```bash
cp .env.example .env
```

| Key | Default | Description |
| :--- | :--- | :--- |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/rag_db` | Postgres async connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis caching & broker URL |
| `CHROMA_HOST` | `localhost` | ChromaDB host |
| `CHROMA_PORT` | `8001` | ChromaDB external port |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama local endpoint |
| `LLM_MODEL` | `qwen3:4b` | Active LLM for inference |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Active embedding model |
