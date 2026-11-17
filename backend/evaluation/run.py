"""RAG evaluation harness (EP-10: FR-052 – FR-056).

    python -m backend.evaluation.run                 # all suites, writes docs/EVALUATION.md
    python -m backend.evaluation.run --quick         # 15 questions, for a fast check

Uses the production pieces: services.chunking (live chunker), the real embedding model
(nomic-embed-text through Ollama), cosine ChromaDB collections, and qa.prepare_from_passages /
complete_answer with the real local LLM (settings.LLM_MODEL). Only the per-user database
authorization is skipped: every document here belongs to one evaluation user.

Pipelines compared (FR-055):
  context-aware  relevance cut, evidence sufficiency, injection cleaning, multi-document
                 handling, claim verification (the production defaults)
  standard       top-5 passages straight into the same prompt; no checks after the model
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import chromadb

from backend.app.core.config import settings
from backend.app.services import answer_verification, grounded_prompt, llm, qa
from backend.app.services.chunking import split_text
from backend.app.services.context_window import Passage, select_passages
from backend.app.services.embedding import embed_query, embed_texts
from backend.evaluation.dataset import COMPARISONS, DOCUMENTS, FOLLOW_UPS, QUESTIONS, UNANSWERABLE

REPORT = Path(__file__).resolve().parents[2] / "docs" / "EVALUATION.md"


# ------------------------------------------------------------------ index + retrieve
class Index:
    def __init__(self, name: str):
        self.client = chromadb.EphemeralClient()
        self.col = self.client.create_collection(f"{name}-{uuid.uuid4().hex[:8]}", metadata={"hnsw:space": "cosine"})
        self.processing_seconds: list[float] = []

    async def add(self, docs: dict[str, str]) -> None:
        for name, text in docs.items():
            start = time.perf_counter()
            chunks = split_text(text)
            vectors = await embed_texts(chunks)
            self.col.add(ids=[f"{name}:{i}" for i in range(len(chunks))], embeddings=vectors, documents=chunks,
                         metadatas=[{"document_id": name, "filename": name, "chunk_index": i} for i in range(len(chunks))])
            self.processing_seconds.append(time.perf_counter() - start)

    async def search(self, question: str, k: int) -> list[Passage]:
        vector = await embed_query(question)
        res = self.col.query(query_embeddings=[vector], n_results=min(k, self.col.count()))
        return [Passage(chunk_id=cid, document_id=meta["document_id"], document_name=meta["filename"],
                        text=doc, distance=float(dist))
                for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0])]


def _has(text: str, phrase: str) -> bool:
    return phrase.lower() in text.lower()


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


# ------------------------------------------------------------------ FR-052 retrieval metrics
async def retrieval_metrics(index: Index, questions) -> dict:
    p5, p10, r5, rr, hit1, latency = [], [], [], [], [], []
    total_relevant = {}
    all_chunks = index.col.get()
    for question, doc, expect in questions:
        relevant = {cid for cid, text, meta in zip(all_chunks["ids"], all_chunks["documents"], all_chunks["metadatas"])
                    if meta["document_id"] == doc and _has(text, expect[0])}
        total_relevant[question] = len(relevant)
        start = time.perf_counter()
        ranked = [p.chunk_id for p in await index.search(question, 10)]
        latency.append(time.perf_counter() - start)
        flags = [cid in relevant for cid in ranked]
        p5.append(sum(flags[:5]) / 5)
        p10.append(sum(flags[:10]) / 10)
        r5.append(sum(flags[:5]) / max(1, len(relevant)))
        rr.append(next((1 / (i + 1) for i, f in enumerate(flags) if f), 0.0))
        hit1.append(1.0 if flags[:1] == [True] else 0.0)
    n = len(questions)
    return {"questions": n, "P@5": sum(p5) / n, "P@10": sum(p10) / n, "R@5": sum(r5) / n, "MRR": sum(rr) / n,
            "Hit@1": sum(hit1) / n, "avg_relevant_chunks": sum(total_relevant.values()) / n,
            "retrieval_p50_s": _pct(latency, 0.5), "retrieval_p95_s": _pct(latency, 0.95)}


# ------------------------------------------------------------------ answering (both pipelines)
@dataclass
class Outcome:
    answer: str
    insufficient: bool
    sources: list[dict]
    seconds: float
    unsupported_left: int
    comparison: dict | None = None
    error: str | None = None


async def answer_context_aware(index: Index, question: str) -> Outcome:
    start = time.perf_counter()
    k = settings.QA_MULTIDOC_FETCH if qa.multi_doc.is_comparison(question) else settings.QA_RETRIEVAL_K
    found = [p for p in await index.search(question, k) if p.distance < settings.QA_MAX_DISTANCE]
    prepared = qa.prepare_from_passages(question, found)
    result = await qa.answer(prepared)
    return Outcome(result.answer, result.insufficient, [s.model_dump() for s in result.sources],
                   time.perf_counter() - start, 0,
                   result.comparison.model_dump() if result.comparison else None)


async def answer_standard(index: Index, question: str) -> Outcome:
    start = time.perf_counter()
    passages = select_passages(await index.search(question, 5), settings.QA_MAX_CONTEXT_TOKENS)
    prepared = qa.PreparedQuestion(question, uuid.uuid4(), uuid.uuid4(), passages,
                                   grounded_prompt.build_messages(question, passages, settings.LLM_MODEL))
    raw = await llm.complete(prepared.messages)
    result = qa.finalize(prepared, raw)
    seconds = time.perf_counter() - start
    # Diagnostic only (not part of the standard pipeline): how many statements lack support?
    check = await answer_verification.verify(result.answer, passages) if not result.insufficient else None
    unsupported = sum(1 for c in check.claims if not c.supported) if check else 0
    return Outcome(result.answer, result.insufficient, [s.model_dump() for s in result.sources], seconds, unsupported)


async def _safe(fn, index, question) -> Outcome:
    try:
        return await fn(index, question)
    except Exception as exc:  # counted as failures (FR-054)
        return Outcome("", False, [], 0.0, 0, error=f"{type(exc).__name__}: {exc}"[:200])


async def answer_quality(index: Index, questions, mode: str) -> dict:
    fn = answer_context_aware if mode == "context-aware" else answer_standard
    rows, times, failures = [], [], 0
    for question, doc, expect in questions:
        out = await _safe(fn, index, question)
        failures += out.error is not None
        times.append(out.seconds)
        correct = not out.insufficient and all(_has(out.answer, e) for e in expect)
        cited_ok = any(s["document_name"] == doc and _has(s["text"], expect[0]) for s in out.sources)
        rows.append({"question": question, "correct": correct, "cited_correct": cited_ok,
                     "refused": out.insufficient, "unsupported_left": out.unsupported_left,
                     "seconds": round(out.seconds, 2), "answer": out.answer[:300], "error": out.error})
    unanswerable = []
    for question in UNANSWERABLE:
        out = await _safe(fn, index, question)
        failures += out.error is not None
        times.append(out.seconds)
        unanswerable.append({"question": question, "refused": out.insufficient, "answer": out.answer[:200]})
    n = len(rows)
    answered = [r for r in rows if not r["refused"]]
    return {
        "mode": mode,
        "accuracy": sum(r["correct"] for r in rows) / n,
        "citation_accuracy": sum(r["cited_correct"] for r in rows) / n,
        "false_refusals": sum(r["refused"] for r in rows) / n,
        "unsupported_statements_in_answers": sum(r["unsupported_left"] for r in rows),
        "answers_with_unsupported": sum(1 for r in answered if r["unsupported_left"]) / max(1, len(answered)),
        "correct_refusal_unanswerable": sum(u["refused"] for u in unanswerable) / len(unanswerable),
        "hallucinated_unanswerable": sum(not u["refused"] for u in unanswerable) / len(unanswerable),
        "answer_p50_s": _pct(times, 0.5), "answer_p95_s": _pct(times, 0.95),
        "failures": failures, "failure_rate": failures / (n + len(unanswerable)),
        "rows": rows, "unanswerable": unanswerable,
    }


# ------------------------------------------------------------------ FR-056 multi-doc + follow-ups
async def multi_doc_eval() -> dict:
    rows = []
    for case in COMPARISONS:
        index = Index("cmp")
        await index.add(case["docs"])
        out = await _safe(answer_context_aware, index, case["question"])
        relation = (out.comparison or {}).get("relation")
        docs_cited = {s["document_name"] for s in out.sources}
        rows.append({"question": case["question"], "expected": case["relation"], "got": relation,
                     "correct": relation == case["relation"], "both_documents_used": len(docs_cited) >= 2
                     or len((out.comparison or {}).get("documents", [])) >= 2,
                     "seconds": round(out.seconds, 2), "answer": out.answer[:300], "error": out.error})
    n = len(rows)
    return {"scenarios": n, "relation_accuracy": sum(r["correct"] for r in rows) / n,
            "both_documents_used": sum(r["both_documents_used"] for r in rows) / n, "rows": rows}


async def follow_up_eval(index: Index) -> dict:
    rows = []
    for first, follow, expect in FOLLOW_UPS:
        alone = await _safe(answer_context_aware, index, follow)
        with_history = await _safe(answer_context_aware, index, f"{first} {follow}")
        rows.append({"first": first, "follow_up": follow, "expect": expect,
                     "alone_correct": not alone.insufficient and _has(alone.answer, expect),
                     "with_history_correct": not with_history.insufficient and _has(with_history.answer, expect)})
    n = len(rows)
    return {"scenarios": n, "follow_up_alone": sum(r["alone_correct"] for r in rows) / n,
            "follow_up_with_history": sum(r["with_history_correct"] for r in rows) / n, "rows": rows}


# ------------------------------------------------------------------ report
def _p(x: float) -> str:
    return f"{x * 100:.0f}%"


def write_report(res: dict) -> None:
    r, ca, st, md, fu, perf = res["retrieval"], res["context_aware"], res["standard"], res["multi_doc"], res["follow_up"], res["performance"]
    lines = [
        "# RAG Evaluation Report (EP-10: FR-052 – FR-056)",
        "",
        f"Generated {res['generated']} by `python -m backend.evaluation.run`. Models: LLM `{res['llm']}`, "
        f"embeddings `{res['embedding']}`, both local through Ollama on a CPU-only host. "
        f"Corpus: {len(DOCUMENTS)} IT-policy documents ({perf['chunks']} chunks), {r['questions']} answerable "
        f"questions with known answers, {len(UNANSWERABLE)} unanswerable questions, {md['scenarios']} comparison "
        f"scenarios, {fu['scenarios']} follow-up scenarios (`backend/evaluation/dataset.py`).",
        "",
        "## Retrieval quality (FR-052)",
        "",
        "A chunk is relevant when it belongs to the right document and contains the answer phrase. "
        f"Each question has {r['avg_relevant_chunks']:.1f} relevant chunk(s) on average, so P@5 cannot exceed about "
        f"{min(1, r['avg_relevant_chunks'] / 5):.2f}; recall and MRR are the meaningful numbers here.",
        "",
        "| Metric | Value |", "| --- | --- |",
        f"| Hit@1 (best chunk is relevant) | {r['Hit@1']:.2f} |",
        f"| MRR | {r['MRR']:.2f} |",
        f"| R@5 | {r['R@5']:.2f} |",
        f"| P@5 | {r['P@5']:.2f} |",
        f"| P@10 | {r['P@10']:.2f} |",
        "",
        "## Answer quality: standard vs context-aware RAG (FR-053, FR-055)",
        "",
        "Same questions, same retrieval and the same model. *Standard* sends the top 5 passages to the prompt and "
        "returns the model's answer. *Context-aware* is the production pipeline: relevance cut, evidence "
        "sufficiency, injection cleaning, multi-document handling and claim verification.",
        "",
        "| Metric | Standard | Context-aware | Better |", "| --- | --- | --- | --- |",
    ]
    rows = [
        ("Correct answer (contains the expected fact)", "accuracy", True),
        ("Citation points to the right passage", "citation_accuracy", True),
        ("Correct refusal on unanswerable questions", "correct_refusal_unanswerable", True),
        ("Hallucinated answer to an unanswerable question", "hallucinated_unanswerable", False),
        ("Answers containing unsupported statements", "answers_with_unsupported", False),
        ("False refusal on answerable questions", "false_refusals", False),
    ]
    wins = 0
    for label, key, higher in rows:
        a, b = st[key], ca[key]
        better = "context-aware" if (b > a if higher else b < a) else ("same" if a == b else "standard")
        wins += better == "context-aware"
        lines.append(f"| {label} | {_p(a)} | {_p(b)} | {better} |")
    lines += [
        f"| Answer latency P50 / P95 | {st['answer_p50_s']:.1f} s / {st['answer_p95_s']:.1f} s | "
        f"{ca['answer_p50_s']:.1f} s / {ca['answer_p95_s']:.1f} s | {'context-aware' if ca['answer_p95_s'] < st['answer_p95_s'] else 'standard'} |",
        "",
        f"Context-aware RAG is better on **{wins} of {len(rows)}** quality metrics "
        f"(story target: at least 3).",
        "",
        "## Performance (FR-054)",
        "",
        "| Measure | Value | Target |", "| --- | --- | --- |",
        f"| Query latency P50 / P95 (full answer, context-aware) | {ca['answer_p50_s']:.1f} s / {ca['answer_p95_s']:.1f} s | P95 < 5 s |",
        f"| Retrieval latency P50 / P95 | {r['retrieval_p50_s'] * 1000:.0f} ms / {r['retrieval_p95_s'] * 1000:.0f} ms | |",
        f"| Document processing (chunk + embed), mean / max | {perf['processing_mean_s']:.2f} s / {perf['processing_max_s']:.2f} s | < 2 min per document |",
        f"| Failure rate | {_p(ca['failure_rate'])} | < 2% |",
        "",
        "## Multi-document reasoning and follow-ups (FR-056)",
        "",
        "| Measure | Value |", "| --- | --- |",
        f"| Comparison relation correct (agree / contradict) | {md['relation_accuracy'] * md['scenarios']:.0f} / {md['scenarios']} |",
        f"| Both documents used in the comparison | {md['both_documents_used'] * md['scenarios']:.0f} / {md['scenarios']} |",
        f"| Follow-up answered when asked alone (no memory yet) | {fu['follow_up_alone'] * fu['scenarios']:.0f} / {fu['scenarios']} |",
        f"| Follow-up answered with the previous question included | {fu['follow_up_with_history'] * fu['scenarios']:.0f} / {fu['scenarios']} |",
        "",
        "Follow-ups such as \"And elsewhere?\" need conversation memory and question rewriting "
        "(FR-037 / FR-038, Sprint 3, Musaab). The second row shows the gain to expect once they land; this "
        "suite will measure them unchanged.",
        "",
        "## Details",
        "",
        "Wrong or refused answers (context-aware):",
        "",
    ]
    misses = [x for x in ca["rows"] if not x["correct"]]
    lines += [f"- {x['question']} → {'refused' if x['refused'] else x['answer'][:160]!r}" for x in misses] or ["- none"]
    lines += ["", "Comparison scenarios:", ""]
    lines += [f"- {x['question']} → expected **{x['expected']}**, got **{x['got']}**" for x in md["rows"]]
    quoted = [x for x in ca["rows"] if x["answer"].startswith("According to") and not x["correct"]]
    lines += [
        "",
        "## Known limitations",
        "",
        f"- **P95 latency {ca['answer_p95_s']:.1f} s vs the 5 s target.** The model runs on CPU only; the median is "
        f"{ca['answer_p50_s']:.1f} s. A GPU (or a smaller model) is needed to meet the target for every question.",
        "- **Follow-up questions** depend on conversation memory and question rewriting (FR-037 / FR-038), "
        "which are not built yet; the follow-up rows above measure the baseline.",
        f"- **Quoted fallback** (used when the model declines although a source sentence matches) quoted the wrong "
        f"sentence {len(quoted)} time(s): " + ("; ".join(repr(x["question"]) for x in quoted) or "none") +
        ". Such answers are marked uncertain and quote the document verbatim, so the error is visible to the reader.",
        "- The corpus is small (12 documents, one chunk each); results on long multi-chunk documents may differ.",
        "",
        "Raw results: `docs/evaluation_results.json`. Regenerate this report without re-running: "
        "`python -m backend.evaluation.run --report-only`.",
        "",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")


async def main(quick: bool) -> dict:
    questions = QUESTIONS[:15] if quick else QUESTIONS
    index = Index("eval")
    await index.add(DOCUMENTS)
    res = {"generated": time.strftime("%Y-%m-%d %H:%M"), "llm": settings.LLM_MODEL, "embedding": settings.EMBEDDING_MODEL}
    res["performance"] = {"chunks": index.col.count(), "processing_mean_s": statistics.mean(index.processing_seconds),
                          "processing_max_s": max(index.processing_seconds)}
    print("retrieval...", flush=True)
    res["retrieval"] = await retrieval_metrics(index, questions)
    print("answers (context-aware)...", flush=True)
    res["context_aware"] = await answer_quality(index, questions, "context-aware")
    print("answers (standard)...", flush=True)
    res["standard"] = await answer_quality(index, questions, "standard")
    print("multi-document...", flush=True)
    res["multi_doc"] = await multi_doc_eval()
    print("follow-ups...", flush=True)
    res["follow_up"] = await follow_up_eval(index)
    (REPORT.parent / "evaluation_results.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    write_report(res)
    return res


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true", help="15 questions only")
    parser.add_argument("--report-only", action="store_true", help="rewrite the report from docs/evaluation_results.json")
    args = parser.parse_args()
    if args.report_only:
        out = json.loads((REPORT.parent / "evaluation_results.json").read_text(encoding="utf-8"))
        write_report(out)
    else:
        out = asyncio.run(main(args.quick))
    ca, st = out["context_aware"], out["standard"]
    print(f"accuracy standard {st['accuracy']:.2f} vs context-aware {ca['accuracy']:.2f}; "
          f"hallucinated unanswerable {st['hallucinated_unanswerable']:.2f} vs {ca['hallucinated_unanswerable']:.2f}; "
          f"report: {REPORT}")
