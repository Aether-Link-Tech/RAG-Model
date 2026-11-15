# Chat / Q&A API (Sprint 3, FR-028)

Ask a question about your documents and get an answer grounded in them, with numbered citations.
Schemas: `backend/app/schemas/chat.py`. Owner: Muhammad Ayyan (AL-51).

## `POST /api/v1/chat/ask`

Requires `Authorization: Bearer <access token>`.

### Request

```json
{
  "question": "How quickly must a data incident be reported?",
  "conversation_id": null,
  "include_public": true,
  "include_private": true
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `question` | string, 1-4000 chars | Required; whitespace-only is rejected (422) |
| `conversation_id` | uuid or null | Omit for a new conversation; the response returns the id to reuse |
| `include_public` / `include_private` | bool | Which documents may be used; default both |

### Response: `Accept: application/json`

```json
{
  "conversation_id": "5b1c…",
  "message_id": "9e0a…",
  "answer": "Employees must report any suspected data incident to the security desk within 24 hours [1].",
  "sources": [
    {
      "marker": 1,
      "document_id": "a91d…",
      "document_name": "HR Handbook 2026.pdf",
      "page": 7,
      "section": null,
      "source_type": "text",
      "text": "Data protection. Employees must report any suspected data incident…",
      "score": 0.6448
    }
  ],
  "insufficient": false,
  "uncertain": false,
  "uncertain_reason": null
}
```

* `sources` lists only the passages the answer cites; `marker` matches the `[n]` in `answer`.
  Render `[n]` as a clickable tag that opens the passage (`text`) in the evidence inspector (FR-045).
* `source_type` is `"ocr"` when the passage was read from an image.
* `score` is similarity 0-1 (1 - cosine distance).
* `insufficient: true`: no relevant passage, or the model found no answer in them. `answer` is
  "The uploaded documents do not contain enough information to answer this question." and `sources` is empty (FR-032).
* `uncertain: true`: weak or single-passage support; show the "Uncertain" badge with `uncertain_reason` (FR-046).
* Responses never contain `user_id` or other ownership metadata.

Hallucination defence (EP-09) adds three fields:

```json
"claims": [
  {"text": "Full-time staff receive 22 days of paid annual leave", "markers": [2],
   "supported": true, "score": 0.881, "chunk_id": "a91d…:4", "reason": null},
  {"text": "Contractors receive 40 days of leave", "markers": [2],
   "supported": false, "score": 0.62, "chunk_id": null,
   "reason": "A number in this statement does not appear in the cited source."}
],
"removed_claims": 1,
"injection_detected": false
```

* `claims`: each statement of the answer, whether its cited passage supports it, and the chunk it is
  linked to (FR-049, FR-044). Unsupported statements are **removed from `answer`**, counted in
  `removed_claims`, and the answer is marked `uncertain`. If nothing is supported, the response is `insufficient`.
* `injection_detected`: a retrieved passage contained instructions aimed at the assistant; they were
  removed before the model saw the text (FR-051). If the model output itself looks injected, the answer is withheld.
* Before calling the model, at least `QA_MIN_EVIDENCE_CHUNKS` (2) relevant passages, or one strong
  passage, are required (FR-048).

### Response: `Accept: text/event-stream` (streaming)

```
event: token
data: {"text": "Employees must report "}

event: token
data: {"text": "within 24 hours [1]."}

event: done
data: { …the same object as the JSON response… }
```

Append `token` texts as they arrive, then **replace** the shown text with `done.answer`
(the final answer has invalid markers removed). On failure the stream ends with
`event: error` / `data: {"detail": "…"}`.

### Errors

| Status | When | `detail` |
| --- | --- | --- |
| 401 | Missing or invalid token | `Not authenticated` |
| 422 | Empty or too long question | validation message |
| 503 | Search (embeddings / vector store) unavailable | `Search is unavailable right now. Please try again in a moment.` |
| 503 | Answer model unavailable | `The answer service is unavailable right now. Please try again in a moment.` |

## How it works

1. The question is embedded and searched with `query_authorized_chunks` (only the caller's private
   documents and public documents; every hit re-checked against PostgreSQL RLS).
2. Passages below the relevance cut (`QA_MAX_DISTANCE`) are dropped; if none remain the model is not called.
3. The most relevant passages are fitted into `QA_MAX_CONTEXT_TOKENS` (6000), cut at sentence ends (FR-030).
4. The local model (`LLM_MODEL`, default `qwen2.5:3b` via Ollama) answers only from the passages
   inside `<context>`, citing `[n]`; passage text is treated as data, never instructions (FR-031, FR-051).
5. Fabricated markers are removed, cited passages become `sources`, and confidence is assessed (FR-046).

Settings: `LLM_MODEL`, `LLM_TIMEOUT_SECONDS`, `LLM_TEMPERATURE`, `QA_RETRIEVAL_K`, `QA_MAX_CONTEXT_TOKENS`,
`QA_MAX_DISTANCE`, `QA_UNCERTAIN_MAX_DISTANCE`, `QA_UNCERTAIN_MIN_SOURCES` (see `backend/app/core/config.py`).

### Model choice

Measured on this project's CPU-only Docker host with a grounded prompt:
`qwen2.5:3b` about 15 s cold / 1-8 s warm; `qwen3:4b` about 5 minutes, because it reasons at length and
ignores the no-thinking switch. Any leaked reasoning (`<think>…</think>`) is stripped before it reaches users.

### Not in this endpoint yet

Conversation memory and saving turns (FR-037, Musaab: `save_turn()`), question rewriting (FR-029),
stored citations (FR-043, Ahmed), hybrid search and the tuned relevance threshold (FR-024 / FR-027, Usman).
