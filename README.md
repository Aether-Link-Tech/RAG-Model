# Enterprise RAG Model Platform

> **Architecture Status:** Sprint 0 Verified & Ready for Sprint 1  
> **Team:** Aether Link Tech  
> **Repository:** [Aether-Link-Tech/RAG-Model](https://github.com/Aether-Link-Tech/RAG-Model)

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
├── requirements.txt               # Pinned Python dependencies
│
├── backend/                       # Python 3.12 + FastAPI backend
│   ├── alembic/                   # Database migrations (PostgreSQL schemas)
│   │   ├── env.py
│   │   └── versions/
│   ├── alembic.ini
│   └── app/
│       ├── core/                  # App configuration, security, JWT, hashing
│       │   ├── config.py
│       │   └── security.py
│       ├── db/                    # Database connections & session factories
│       │   ├── postgres.py        # SQLAlchemy async engine & session maker
│       │   ├── chroma.py          # ChromaDB client & collection management
│       │   └── redis.py           # Redis client & caching helpers
│       ├── models/                # SQLAlchemy ORM database models
│       │   ├── user.py            # User, Role, Organization
│       │   ├── document.py        # Document, Chunk, IngestionJob
│       │   └── chat.py            # ChatSession, Message, Citation
│       ├── schemas/               # Pydantic schemas (Request/Response validation)
│       │   ├── auth.py
│       │   ├── document.py
│       │   └── chat.py
│       ├── services/              # Core business logic
│       │   ├── auth_service.py    # Login, signup, password hashing
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
│       └── main.py                # FastAPI entry point
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
        string checksum "SHA-256, duplicate detection"
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
source .venv/bin/activate
uvicorn backend.app.main:app --reload --port 8000 --host 0.0.0.0
```

- OpenAPI Documentation: **[http://localhost:8000/docs](http://localhost:8000/docs)**
- Service Health Check: **[http://localhost:8000/api/health](http://localhost:8000/api/health)**

### 3. Run Frontend (React + Vite)

```bash
cd frontend
npm install
npm run dev
```

Open **[http://localhost:3000](http://localhost:3000)** to view the application UI.

### 4. Local AI Models (Ollama)

```bash
# Pull required models
ollama pull nomic-embed-text
ollama pull qwen3:4b
```

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
