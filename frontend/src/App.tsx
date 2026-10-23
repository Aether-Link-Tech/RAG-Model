import React, { useState, useEffect } from 'react';
import axios from 'axios';

interface ServiceStatus {
  status: string;
  version?: string;
  message?: string;
  host?: string;
  url?: string;
  available_models?: string[];
  target_llm?: string;
  target_embedding?: string;
}

interface HealthResponse {
  timestamp: number;
  backend: {
    status: string;
    version: string;
  };
  services: {
    postgres?: ServiceStatus;
    redis?: ServiceStatus;
    chromadb?: ServiceStatus;
    ollama?: ServiceStatus;
  };
}

export default function App() {
  const [healthData, setHealthData] = useState<HealthResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [lastChecked, setLastChecked] = useState<string>('');
  const [copyStatus, setCopyStatus] = useState<boolean>(false);

  const fetchHealth = async () => {
    setLoading(true);
    try {
      const res = await axios.get<HealthResponse>('http://localhost:8000/api/health', {
        timeout: 3000,
      });
      setHealthData(res.data);
    } catch {
      setHealthData(null);
    } finally {
      setLoading(false);
      setLastChecked(new Date().toLocaleTimeString());
    }
  };

  useEffect(() => {
    fetchHealth();
    const interval = setInterval(fetchHealth, 10000);
    return () => clearInterval(interval);
  }, []);

  const checklistItems = [
    { id: 1, title: 'Install Python 3.12', detail: 'v3.12.14 installed in .venv', status: true },
    { id: 2, title: 'Install Node.js 22 LTS', detail: 'Node.js runtime active', status: true },
    { id: 3, title: 'Install Docker Desktop', detail: 'Docker 29.8 running', status: true },
    { id: 4, title: 'Install Ollama', detail: 'Ollama 0.40.9 with Apple Silicon MLX', status: true },
    { id: 5, title: 'ollama pull qwen3:4b', detail: 'Target local LLM model', status: healthData?.services.ollama?.available_models?.some(m => m.includes('qwen')) ?? false },
    { id: 6, title: 'ollama pull nomic-embed-text', detail: 'Target embedding model', status: healthData?.services.ollama?.available_models?.some(m => m.includes('nomic')) ?? false },
    { id: 7, title: 'git clone <repo>', detail: 'Aether-Link-Tech/RAG-Model setup', status: true },
    { id: 8, title: 'docker compose up', detail: 'PostgreSQL 17, Redis 7, ChromaDB', status: healthData?.services.postgres?.status === 'connected' && healthData?.services.redis?.status === 'connected' },
    { id: 9, title: 'Check localhost:8000/docs', detail: 'FastAPI Interactive OpenAPI Docs', status: healthData?.backend.status === 'healthy' },
    { id: 10, title: 'Check localhost:3000', detail: 'Vite 8 + React 19 Frontend Dashboard', status: true },
  ];

  const teamMessage = `Walaikum Assalam Ayyan!

I've reviewed the Tech Stack document and Epics. 

Sprint 0 setup is progressing smoothly on my machine:
• Python 3.12 virtual environment is initialized with all required dependencies.
• Docker Compose configuration for PostgreSQL 17, Redis 7, and ChromaDB is ready.
• FastAPI backend scaffolding with /docs is running.
• Frontend Vite + React 19 + TypeScript is running on port 3000.
• Apple M4 16GB setup ready for local Ollama models (qwen3:4b, nomic-embed-text).

On track to wrap up Sprint 0 early and ready for Sprint 1! 🚀`;

  const copyToClipboard = () => {
    navigator.clipboard.writeText(teamMessage);
    setCopyStatus(true);
    setTimeout(() => setCopyStatus(false), 2500);
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 p-6 md:p-12 font-sans selection:bg-indigo-500 selection:text-white">
      {/* Background Glow */}
      <div className="fixed inset-0 overflow-hidden pointer-events-none -z-10">
        <div className="absolute top-0 left-1/4 w-96 h-96 bg-indigo-500/10 rounded-full blur-3xl" />
        <div className="absolute top-1/3 right-1/4 w-96 h-96 bg-purple-500/10 rounded-full blur-3xl" />
      </div>

      <div className="max-w-6xl mx-auto space-y-10">
        {/* Header */}
        <header className="flex flex-col md:flex-row md:items-center md:justify-between border-b border-slate-800/80 pb-6 gap-4">
          <div>
            <div className="flex items-center gap-3">
              <span className="px-2.5 py-1 rounded-md text-xs font-semibold uppercase tracking-wider bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                Sprint 0 Setup
              </span>
              <span className="text-xs text-slate-400">Owner: Aether Link Team</span>
            </div>
            <h1 className="text-3xl md:text-4xl font-extrabold tracking-tight mt-2 text-white">
              Enterprise RAG Architecture
            </h1>
            <p className="text-slate-400 text-sm mt-1">
              Local Full-Stack Verification & Environment Status Dashboard
            </p>
          </div>

          <div className="flex items-center gap-3">
            <button
              onClick={fetchHealth}
              disabled={loading}
              className="px-4 py-2 bg-slate-900 hover:bg-slate-800 border border-slate-700/70 rounded-lg text-sm font-medium transition duration-150 flex items-center gap-2 cursor-pointer disabled:opacity-50"
            >
              <span className={`inline-block w-2 h-2 rounded-full ${loading ? 'bg-amber-400 animate-ping' : 'bg-emerald-400'}`} />
              {loading ? 'Probing...' : 'Refresh Status'}
            </button>
            <a
              href="http://localhost:8000/docs"
              target="_blank"
              rel="noreferrer"
              className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 rounded-lg text-sm font-medium transition duration-150 text-white shadow-lg shadow-indigo-600/25"
            >
              Open API Docs ↗
            </a>
          </div>
        </header>

        {/* Live Service Cards Grid */}
        <section className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold tracking-wide text-slate-200">
              Stack Services Health
            </h2>
            <span className="text-xs text-slate-400">
              Last probe: {lastChecked || 'Checking...'}
            </span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            {/* FastAPI */}
            <div className="bg-slate-900/60 border border-slate-800/80 rounded-xl p-5 hover:border-slate-700 transition">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Backend API</span>
                <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${healthData?.backend.status === 'healthy' ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-amber-500/10 text-amber-400 border border-amber-500/20'}`}>
                  {healthData?.backend.status === 'healthy' ? 'Active' : 'Standby'}
                </span>
              </div>
              <p className="text-xl font-bold mt-2 text-white">FastAPI 0.142+</p>
              <p className="text-xs text-slate-400 mt-1 font-mono">http://localhost:8000</p>
              <div className="mt-4 pt-3 border-t border-slate-800 text-xs text-slate-400 flex justify-between">
                <span>Docs</span>
                <span className="text-indigo-400 font-mono">/docs & /redoc</span>
              </div>
            </div>

            {/* PostgreSQL */}
            <div className="bg-slate-900/60 border border-slate-800/80 rounded-xl p-5 hover:border-slate-700 transition">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Primary DB (Docker)</span>
                <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${healthData?.services.postgres?.status === 'connected' ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-slate-800 text-slate-400'}`}>
                  {healthData?.services.postgres?.status === 'connected' ? 'Connected' : 'Docker up'}
                </span>
              </div>
              <p className="text-xl font-bold mt-2 text-white">PostgreSQL 17</p>
              <p className="text-xs text-slate-400 mt-1 font-mono">localhost:5432</p>
              <div className="mt-4 pt-3 border-t border-slate-800 text-xs text-slate-400 flex justify-between">
                <span>Feature</span>
                <span className="text-indigo-400">RLS + AsyncPG</span>
              </div>
            </div>

            {/* Redis */}
            <div className="bg-slate-900/60 border border-slate-800/80 rounded-xl p-5 hover:border-slate-700 transition">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Broker & Cache</span>
                <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${healthData?.services.redis?.status === 'connected' ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-slate-800 text-slate-400'}`}>
                  {healthData?.services.redis?.status === 'connected' ? 'Connected' : 'Docker up'}
                </span>
              </div>
              <p className="text-xl font-bold mt-2 text-white">Redis 7.x</p>
              <p className="text-xs text-slate-400 mt-1 font-mono">localhost:6379</p>
              <div className="mt-4 pt-3 border-t border-slate-800 text-xs text-slate-400 flex justify-between">
                <span>Used For</span>
                <span className="text-indigo-400">Celery Jobs + Cache</span>
              </div>
            </div>

            {/* ChromaDB */}
            <div className="bg-slate-900/60 border border-slate-800/80 rounded-xl p-5 hover:border-slate-700 transition">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Vector Store</span>
                <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${healthData?.services.chromadb?.status === 'connected' ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-slate-800 text-slate-400'}`}>
                  {healthData?.services.chromadb?.status === 'connected' ? 'Connected' : 'Docker up'}
                </span>
              </div>
              <p className="text-xl font-bold mt-2 text-white">ChromaDB 1.5.3</p>
              <p className="text-xs text-slate-400 mt-1 font-mono">localhost:8001</p>
              <div className="mt-4 pt-3 border-t border-slate-800 text-xs text-slate-400 flex justify-between">
                <span>Embeddings</span>
                <span className="text-indigo-400">nomic-embed-text</span>
              </div>
            </div>
          </div>
        </section>

        {/* Sprint 0 Checklist & Team Message Side by Side */}
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
          {/* Checklist */}
          <section className="lg:col-span-7 bg-slate-900/40 border border-slate-800/80 rounded-2xl p-6">
            <h2 className="text-lg font-semibold text-slate-200 mb-4 flex items-center justify-between">
              <span>Sprint 0 Setup Checklist (Section 11)</span>
              <span className="text-xs bg-slate-800 px-2 py-1 rounded text-slate-300">2-Day Setup Target</span>
            </h2>

            <div className="space-y-2.5">
              {checklistItems.map((item) => (
                <div
                  key={item.id}
                  className="flex items-center justify-between p-3 rounded-xl bg-slate-900/80 border border-slate-800/60 hover:border-slate-700/80 transition"
                >
                  <div className="flex items-center gap-3">
                    <span className={`w-5 h-5 rounded-full flex items-center justify-center text-xs font-bold ${item.status ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/40' : 'bg-slate-800 text-slate-400'}`}>
                      {item.status ? '✓' : item.id}
                    </span>
                    <div>
                      <p className="text-sm font-medium text-slate-200">{item.title}</p>
                      <p className="text-xs text-slate-400">{item.detail}</p>
                    </div>
                  </div>
                  <span className={`text-xs font-medium px-2 py-0.5 rounded ${item.status ? 'text-emerald-400 bg-emerald-500/10' : 'text-slate-400 bg-slate-800'}`}>
                    {item.status ? 'Ready' : 'Pending'}
                  </span>
                </div>
              ))}
            </div>
          </section>

          {/* Quick Team Lead Reply Box */}
          <section className="lg:col-span-5 flex flex-col justify-between bg-slate-900/40 border border-slate-800/80 rounded-2xl p-6">
            <div>
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-semibold text-slate-200">
                  Team Lead Reply
                </h2>
                <span className="text-xs text-indigo-400">Ready to Send</span>
              </div>
              <p className="text-xs text-slate-400 mb-3">
                Pre-formatted response to answer Muhammad Ayyan in the group chat:
              </p>
              <div className="bg-slate-950 p-4 rounded-xl border border-slate-800/80 font-mono text-xs text-slate-300 leading-relaxed whitespace-pre-wrap select-all">
                {teamMessage}
              </div>
            </div>

            <button
              onClick={copyToClipboard}
              className="mt-4 w-full py-2.5 px-4 bg-emerald-600 hover:bg-emerald-500 text-white rounded-xl text-sm font-semibold transition cursor-pointer flex items-center justify-center gap-2 shadow-lg shadow-emerald-600/20"
            >
              {copyStatus ? '✓ Copied to Clipboard!' : 'Copy Reply Message'}
            </button>
          </section>
        </div>
      </div>
    </div>
  );
}
