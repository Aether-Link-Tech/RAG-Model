# Enterprise RAG Model Platform

> **Architecture Status:** Sprint 0 Verified & Ready for Sprint 1  
> **Team:** Aether Link Tech  
> **Repository:** [Aether-Link-Tech/RAG-Model](https://github.com/Aether-Link-Tech/RAG-Model)

> [!NOTE]
> **Backend Architecture Update:** The Python environment and backend configuration have been consolidated under `backend/`. Python virtual environments now reside in `backend/.venv/`, dependencies in `backend/requirements.txt`, and database migrations in `backend/alembic.ini`. The obsolete root launcher `main.py` has been removed in favor of direct execution via `uvicorn backend.app.main:app`. Please review the [Teammate Onboarding & Migration Guide](#-teammate-onboarding--migration-guide) if pulling existing branches.

---

## 🎯 Project Goal & Mission

The **Enterprise RAG Model Platform** is a privacy-first, fully offline-capable Retrieval-Augmented Generation (RAG) system engineered for high-accuracy document intelligence and question answering over sensitive internal data.

### Key Objectives:

1. **100% Offline & Private AI**: Run all inference locally using **Ollama** (`qwen3:4b` / `qwen3:8b`) and local dense embeddings (`nomic-embed-text`) without data leaking to external cloud LLM APIs.
2. **Enterprise Multi-Tenancy & Security**: Implement strict user role management, document access control, and **Row-Level Security (RLS)** in PostgreSQL 17.
3. **Advanced Retrieval Pipeline**: Combine dense vector similarity search (**ChromaDB**) with cross-encoder re-ranking (**bge-reranker-v2-m3**) to eliminate hallucinations.
4. **Resilient Multimodal Ingestion**: Asynchronously ingest, validate (ClamAV / MIME check), and parse complex documents (PDFs, DOCX, scans with Florence-2 OCR) via **Celery & Redis**.
5. **Accurate Citations & Traceability**: Every generated answer links directly back to specific document chunks, page numbers, and relevance confidence scores.

---

## 📂 Project Directory Structure

```text
RAG-Model/
├── docker-compose.yml             # Postgres 17, Redis 7, ChromaDB
├── .env / .env.example            # Environment variables & secrets
├── .gitignore                     # Git ignore rules (includes backend/.venv/)
│
├── backend/                       # Python 3.12 + FastAPI backend
│   ├── .venv/                     # Dedicated backend virtual environment (gitignored)
│   ├── requirements.txt           # Backend dependencies (FastAPI, SQLAlchemy[asyncio], greenlet, etc.)
│   ├── alembic.ini                # Alembic migration configuration (script_location = %(here)s/alembic)
│   ├── alembic/                   # Database migrations (PostgreSQL schemas)
│   │   ├── env.py                 # Async migration runner (imports Base & Settings)
│   │   ├── script.py.mako         # Migration script template
│   │   └── versions/              # Migration versions (Current head: 24737d3d7b01)
│   └── app/
│       ├── core/                  # App configuration, security, JWT, hashing
│       │   ├── config.py          # Pydantic BaseSettings & environment loader
│       │   └── security.py        # Token generation & encryption utilities
│       ├── db/                    # Database connections & session factories
│       │   ├── session.py         # SQLAlchemy async engine & async_sessionmaker
│       │   ├── base.py            # Declarative Base metadata
│       │   ├── chroma.py          # ChromaDB client & collection management
│       │   └── redis.py           # Redis client & caching helpers
│       ├── models/                # SQLAlchemy ORM database models
│       │   ├── user.py            # User model with role constraints
│       │   ├── document.py        # Document & Chunk models with SHA-256 file_hash
│       │   └── chat.py            # ChatSession, Message, Citation models
│       ├── schemas/               # Pydantic schemas (Request/Response validation)
│       │   ├── auth.py
│       │   ├── document.py
│       │   └── chat.py
│       ├── services/              # Core business logic
│       │   ├── auth_service.py    # Login, signup, password hashing
│       │   ├── hash_service.py    # Streaming SHA-256 file_hash computation
│       │   ├── document_parser.py # PDF/DOCX extract, OCR (Florence-2)
│       │   ├── chunking.py        # Text splitting & tokenization
│       │   ├── embedding.py       # nomic-embed-text via Ollama
│       │   ├── vector_store.py    # ChromaDB indexing & vector search
│       │   ├── reranker.py        # BGE-Reranker-v2-m3
│       │   └── rag_pipeline.py    # Context assembly + Qwen3 generation
│       ├── workers/               # Celery async background worker tasks
│       │   ├── celery_app.py
│       │   └── tasks.py           # Background file parsing & embedding
│       ├── api/                   # REST API route endpoints
│       │   ├── v1/
│       │   │   ├── auth.py        # /api/v1/auth
│       │   │   ├── documents.py   # /api/v1/documents (Upload, ingest, list)
│       │   │   └── chat.py        # /api/v1/chat (Prompt, stream, citations)
│       │   └── deps.py            # FastAPI dependency injection (get_db, current_user)
│       └── main.py                # FastAPI entry point (launched via backend.app.main:app)
│
└── frontend/                      # React 19 + TypeScript + Vite 8
    ├── src/
    │   ├── api/                   # Axios HTTP client & API hooks
    │   ├── components/            # UI components (ChatWindow, FileUpload, etc.)
    │   ├── pages/                 # Login, Dashboard, Chat, DocumentManager
    │   └── App.tsx
    └── package.json
```

---

## 🏛️ Architecture Overview

| Component            | Technology                  | Version            | Deployment                   | Purpose                                  |
| :------------------- | :-------------------------- | :----------------- | :--------------------------- | :--------------------------------------- |
| **Backend API**      | FastAPI + Uvicorn           | `0.142+` / `0.161` | Local Host (Port `8000`)     | Core REST APIs, OpenAPI docs             |
| **Database**         | PostgreSQL                  | `17-alpine`        | Docker Compose (Port `5432`) | Primary transactional database with RLS  |
| **Broker & Cache**   | Redis                       | `7-alpine`         | Docker Compose (Port `6379`) | Task queue broker for Celery and caching |
| **Vector Database**  | ChromaDB                    | `1.5.3` / `v1.0.0` | Docker Compose (Port `8001`) | Vector search & embeddings storage       |
| **Frontend UI**      | React 19 + TypeScript       | `19.2+`            | Local Host (Port `3000`)     | User interface & Admin dashboard         |
| **Styling & Build**  | Tailwind CSS 4 + Vite 8     | `4.x` / `8.x`      | Local Host                   | High-performance bundling & styling      |
| **Local LLM**        | Ollama (`qwen3:4b` / `8b`)  | `0.40.7+`          | Local Host (Port `11434`)    | Offline generation (CPU / Metal GPU)     |
| **Local Embeddings** | Ollama (`nomic-embed-text`) | `768-dim`          | Local Host                   | Dense vector document embeddings         |
| **Re-ranker**        | `bge-reranker-v2-m3`        | Latest             | Local Host                   | Cross-encoder re-ranking                 |
| **OCR Engine**       | `Florence-2-base`           | Latest             | Local Host                   | Vision & text bounding box extraction    |

---

## 🔄 End-to-End Data Flow

```mermaid
flowchart TD
    subgraph Client [Frontend UI - React 19]
        User([User]) -->|1. Upload File / Query| WebUI[React + Tailwind UI]
    end

    subgraph API [FastAPI Backend]
        WebUI -->|HTTP POST Multipart| DocAPI["/api/v1/documents"]
        WebUI -->|HTTP POST JSON| ChatAPI["/api/v1/chat"]
        AuthCheck{JWT & RLS Policy}
        DocAPI --> AuthCheck
        ChatAPI --> AuthCheck
    end

    subgraph Ingestion [Document Ingestion Pipeline]
        AuthCheck -->|Dispatch Task| CeleryQueue[(Redis Queue)]
        CeleryQueue --> CeleryWorker[Celery Background Worker]
        CeleryWorker --> ClamAV[ClamAV Scan & Magic Check]
        CeleryWorker --> Parser[Text Extraction / Florence-2 OCR]
        CeleryWorker --> Chunking[Chunking Engine]
        Chunking --> OllamaEmbed[Ollama nomic-embed-text]
        OllamaEmbed -->|Store Vectors & Metadata| Chroma[(ChromaDB Vector Store)]
        CeleryWorker -->|Store Doc Metadata & Chunks| PG_Docs[(PostgreSQL 17)]
    end

    subgraph Retrieval [RAG Query Pipeline]
        AuthCheck -->|1. Embed Query| OllamaQuery[Ollama nomic-embed-text]
        OllamaQuery -->|2. Top-K Vector Search| Chroma
        Chroma -->|Candidate Chunks| Reranker[BGE-Reranker-v2-m3]
        Reranker -->|Top-N Reranked Chunks| PromptEngine[Augmented Context Builder]
        PromptEngine -->|Context + Prompt| OllamaLLM[Ollama Qwen3:4b / 8b]
        OllamaLLM -->|Streamed Response & Citations| WebUI
        ChatAPI -->|Save Chat History & Tokens| PG_Chat[(PostgreSQL 17)]
    end
```

---

## 📊 Entity Relationship Diagram (ERD)

```mermaid
erDiagram
    USERS ||--o{ DOCUMENTS : "owns"
    USERS ||--o{ CHAT_SESSIONS : "creates"
    USERS ||--o{ REFRESH_TOKENS : "holds"
    DOCUMENTS ||--o{ DOCUMENT_CHUNKS : "contains"
    CHAT_SESSIONS ||--o{ MESSAGES : "contains"
    MESSAGES ||--o{ CITATIONS : "references"
    DOCUMENT_CHUNKS ||--o{ CITATIONS : "sourced from"

    USERS {
        uuid id PK
        string email UK
        string hashed_password
        string role "admin | user, CHECK constraint"
        timestamp created_at
    }

    REFRESH_TOKENS {
        uuid id PK
        uuid user_id FK
        string token_hash UK
        timestamp expires_at
        timestamp revoked_at "nullable"
    }

    DOCUMENTS {
        uuid id PK
        uuid user_id FK
        string filename
        string file_path
        string mime_type "validated by magic bytes"
        integer file_size
        string file_hash UK "SHA-256, duplicate detection"
        string status "pending | processing | indexed | failed"
        text error_message "nullable, set when failed"
        timestamp created_at
        timestamp updated_at
    }

    DOCUMENT_CHUNKS {
        uuid id PK
        uuid document_id FK
        integer chunk_index "UNIQUE with document_id"
        text content
        tsvector search_vector "GIN index for hybrid retrieval"
        integer page_number
        integer token_count
        string chroma_id UK "external: ChromaDB vector ID"
    }

    CHAT_SESSIONS {
        uuid id PK
        uuid user_id FK
        string title
        timestamp created_at
        timestamp last_used
        boolean is_pinned
        boolean is_deleted
    }

    MESSAGES {
        uuid id PK
        uuid session_id FK
        string sender "user | assistant, CHECK constraint"
        text content
        string model_name "nullable, assistant only"
        integer prompt_tokens
        integer completion_tokens
        integer latency_ms
        json retrieved_chunk_ids "nullable"
        timestamp created_at
    }

    CITATIONS {
        uuid id PK
        uuid message_id FK
        uuid chunk_id FK
        timestamp created_at
    }
```

---

## 🚀 Quickstart Guide

### 1. Configure Environment Variables

Create your local `.env` configuration from the provided template:

```bash
cp .env.example .env
```

### 2. Start Infrastructure Services (Docker)

PostgreSQL 17, Redis 7, and ChromaDB run in isolated Docker containers:

```bash
# Start all infrastructure containers in background
docker compose up -d

# Verify container health
docker compose ps
```

### 3. Setup Backend Virtual Environment & Dependencies

The backend virtual environment is encapsulated under `backend/.venv/` and dependencies are located in `backend/requirements.txt`.

#### Option A: Working from Project Root (Recommended)

```bash
# 1. Create the virtual environment in backend/.venv
python3 -m venv backend/.venv

# 2. Activate the virtual environment
source backend/.venv/bin/activate

# 3. Upgrade pip and install pinned backend dependencies
pip install --upgrade pip
pip install -r backend/requirements.txt
```

#### Option B: Working from `backend/` Directory

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cd ..
```

> [!TIP]
> The dependencies include `sqlalchemy[asyncio]>=2.0.0` and `greenlet>=3.0.0` to provide robust asynchronous SQLAlchemy support with PostgreSQL via `asyncpg`.

### 4. Apply Database Migrations (Alembic)

The active Alembic configuration is located at `backend/alembic.ini` with `script_location = %(here)s/alembic`. The current head revision is `24737d3d7b01 (head)`.

```bash
# Run migrations up to head (from project root)
alembic -c backend/alembic.ini upgrade head

# Verify the current migration revision
alembic -c backend/alembic.ini current
# Output: 24737d3d7b01 (head)
```

_(Alternatively, from inside the `backend/` directory: `cd backend && alembic upgrade head && alembic current`)_

### 5. Run Backend (FastAPI)

FastAPI is launched directly via Uvicorn targeting `backend.app.main:app` from the project root (the legacy root launcher `main.py` is no longer required):

```bash
# Ensure the backend virtual environment is active
source backend/.venv/bin/activate

# Launch the FastAPI development server
uvicorn backend.app.main:app --reload --port 8000 --host 0.0.0.0
```

- **API Root**: [http://localhost:8000/](http://localhost:8000/)
- **Swagger UI Interactive Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc Interactive Docs**: [http://localhost:8000/redoc](http://localhost:8000/redoc)
- **Infrastructure Health Check**: [http://localhost:8000/api/health](http://localhost:8000/api/health)

### 6. Run Frontend (React 19 + Vite 8)

In a separate terminal:

```bash
cd frontend
npm install
npm run dev
```

Open **[http://localhost:3000](http://localhost:3000)** in your browser.

### 7. Pull Local AI Models (Ollama)

Ensure Ollama is installed and running, then pull the models:

```bash
# Dense vector embeddings (768 dimensions)
ollama pull nomic-embed-text

# Local reasoning LLM (4-bit quantized)
ollama pull qwen3:4b
```

---

## 🗄️ Database Migrations & Schema Management

All database schema evolutions are managed by Alembic in `backend/alembic/` using asynchronous sessions against PostgreSQL 17:

- **Active Config File**: `backend/alembic.ini`
- **Migration Environment**: `backend/alembic/env.py`
- **Versions Directory**: `backend/alembic/versions/`
- **Initial Schema Revision**: `24737d3d7b01` (creates `users` and `documents` tables with unique SHA-256 `file_hash`)

### Common Alembic Commands

```bash
# Check current migration revision
alembic -c backend/alembic.ini current

# Apply all pending migrations
alembic -c backend/alembic.ini upgrade head

# Generate a new autogenerated migration after updating SQLAlchemy models in backend/app/models/
alembic -c backend/alembic.ini revision --autogenerate -m "describe_changes_here"

# Inspect migration history
alembic -c backend/alembic.ini history --verbose

# Roll back the most recent migration (downgrade 1 step)
alembic -c backend/alembic.ini downgrade -1
```

---

## 👥 Teammate Onboarding & Migration Guide

If you are pulling changes into an existing local clone that previously used the root-level virtual environment, follow these steps to cleanly transition:

### Step 1: Clean Up Obsolete Root Artifacts

```bash
# Deactivate old virtual environment if active
deactivate 2>/dev/null || true

# Remove obsolete root virtual environment
rm -rf .venv

# Note: root alembic.ini, main.py, and requirements.txt are deleted in git
```

### Step 2: Set Up New Backend Virtual Environment

```bash
# Create fresh venv in backend/
python3 -m venv backend/.venv
source backend/.venv/bin/activate

# Install updated dependencies
pip install --upgrade pip
pip install -r backend/requirements.txt
```

### Step 3: Configure Your IDE Python Interpreter

- **VS Code**: Press `Cmd+Shift+P` (Mac) or `Ctrl+Shift+P` (Linux/Windows), type `Python: Select Interpreter`, and choose `./backend/.venv/bin/python`.
- **PyCharm**: Navigate to `Settings` / `Preferences` -> `Project: RAG-Model` -> `Python Interpreter` -> `Add Interpreter` -> `Existing Environment` -> choose `backend/.venv/bin/python`.

### Step 4: Verify Database & Migrations

```bash
# Start database container
docker compose up -d

# Check migration status (should match head: 24737d3d7b01)
alembic -c backend/alembic.ini current
```

### Step 5: Test Backend & Health Check

```bash
# Start server
uvicorn backend.app.main:app --reload

# Verify health endpoint in another shell
curl http://localhost:8000/api/health
```

---

## 🛠️ Troubleshooting & FAQ

<details>
<summary><b>1. <code>ModuleNotFoundError: No module named 'backend'</code></b></summary>

Ensure you are executing `uvicorn` or `python` commands from the **project root** directory where `backend` is a discoverable package, with `backend/.venv` activated:

```bash
cd /path/to/RAG-Model
source backend/.venv/bin/activate
uvicorn backend.app.main:app --reload
```

</details>

<details>
<summary><b>2. <code>greenlet_spawn has not been called; can't call await_only() here</code> or missing greenlet</b></summary>

SQLAlchemy 2.0+ requires `greenlet` when using async engines like `asyncpg`. Ensure `greenlet>=3.0.0` is installed inside your virtual environment:

```bash
pip install "sqlalchemy[asyncio]>=2.0.0" "greenlet>=3.0.0"
```

Both are specified in `backend/requirements.txt`.

</details>

<details>
<summary><b>3. Alembic error: <code>Config file 'alembic.ini' not found</code></b></summary>

The configuration file now resides at `backend/alembic.ini`. Always supply the `-c` flag when running from project root:

```bash
alembic -c backend/alembic.ini upgrade head
```

Or switch to the backend directory first: `cd backend && alembic upgrade head`.

</details>

<details>
<summary><b>4. Database connection refused on port 5432</b></summary>

Ensure Docker Compose is running:

```bash
docker compose up -d postgres
docker compose ps
```

If port 5432 conflicts with a local PostgreSQL installation, adjust `POSTGRES_PORT` in your `.env` file.

</details>

---

## ⚙️ Environment Variables (`.env`)

| Key               | Default                                                        | Description                      |
| :---------------- | :------------------------------------------------------------- | :------------------------------- |
| `DATABASE_URL`    | `postgresql+asyncpg://postgres:postgres@localhost:5432/rag_db` | Postgres async connection string |
| `REDIS_URL`       | `redis://localhost:6379/0`                                     | Redis caching & broker URL       |
| `CHROMA_HOST`     | `localhost`                                                    | ChromaDB host                    |
| `CHROMA_PORT`     | `8001`                                                         | ChromaDB external port           |
| `OLLAMA_BASE_URL` | `http://localhost:11434`                                       | Ollama local endpoint            |
| `LLM_MODEL`       | `qwen3:4b`                                                     | Active LLM for inference         |
| `EMBEDDING_MODEL` | `nomic-embed-text`                                             | Active embedding model           |
