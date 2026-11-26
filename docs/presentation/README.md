# Aether Link: presentation slides

1920×1080 (16:9) images for the class presentation. Numbers match `backend/app/core/config.py` and
`docs/EVALUATION.md` (develop, 26 Nov 2026).

| Slide | What it shows |
| --- | --- |
| [01_system_architecture](01_system_architecture.png) | Every component and how they connect (browser → Cloudflare → Caddy → nginx → FastAPI → PostgreSQL / Redis / Celery worker / ChromaDB / Ollama, plus ClamAV, parser sandbox, OCR, email) |
| [02_upload_pipeline](02_upload_pipeline.png) | Upload → checks → virus scan → queue → sandbox parsing → OCR + near-duplicates → chunking (1,000 / 200 chars) → embeddings (nomic-embed-text, 768) → storage |
| [03_question_pipeline](03_question_pipeline.png) | Question → embedding → authorised search (RLS) → relevance filter → clean + prompt → qwen2.5:3b → verification → cited, highlighted answer |
| [04_thresholds_and_decisions](04_thresholds_and_decisions.png) | Every threshold (0.55, +0.12, evidence rule, 0.78, 0.65, 0.68 / 0.83, 50%) and what it decides |
| [05_ai_models](05_ai_models.png) | The four local models and the evaluation results (94% correct, 96% citations, 0% invented, 2.8 s) |
| [06_security_layers](06_security_layers.png) | Eight independent protections (accounts, upload safety, sandbox, RLS, vector isolation, AI safety, sharing rules, audit and limits) |
| [07_public_link_cloudflare](07_public_link_cloudflare.png) | How the public demo link works with Cloudflare Tunnel, and its limits |

Glossary of the terms used: distance = how different two meanings are (0 = identical); similarity = 1 − distance;
RLS = PostgreSQL row-level security; OCR = reading text from images.
