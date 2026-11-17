# RAG Evaluation Report (EP-10: FR-052 – FR-056)

Generated 2026-11-17 04:31 by `python -m backend.evaluation.run`. Models: LLM `qwen2.5:3b`, embeddings `nomic-embed-text`, both local through Ollama on a CPU-only host. Corpus: 12 IT-policy documents (12 chunks), 53 answerable questions with known answers, 8 unanswerable questions, 5 comparison scenarios, 10 follow-up scenarios (`backend/evaluation/dataset.py`).

## Retrieval quality (FR-052)

A chunk is relevant when it belongs to the right document and contains the answer phrase. Each question has 1.0 relevant chunk(s) on average, so P@5 cannot exceed about 0.20; recall and MRR are the meaningful numbers here.

| Metric | Value |
| --- | --- |
| Hit@1 (best chunk is relevant) | 0.91 |
| MRR | 0.94 |
| R@5 | 1.00 |
| P@5 | 0.20 |
| P@10 | 0.10 |

## Answer quality: standard vs context-aware RAG (FR-053, FR-055)

Same questions, same retrieval and the same model. *Standard* sends the top 5 passages to the prompt and returns the model's answer. *Context-aware* is the production pipeline: relevance cut, evidence sufficiency, injection cleaning, multi-document handling and claim verification.

| Metric | Standard | Context-aware | Better |
| --- | --- | --- | --- |
| Correct answer (contains the expected fact) | 91% | 94% | context-aware |
| Citation points to the right passage | 83% | 96% | context-aware |
| Correct refusal on unanswerable questions | 100% | 100% | same |
| Hallucinated answer to an unanswerable question | 0% | 0% | same |
| Answers containing unsupported statements | 0% | 0% | same |
| False refusal on answerable questions | 9% | 2% | context-aware |
| Answer latency P50 / P95 | 6.4 s / 8.5 s | 2.8 s / 6.7 s | context-aware |

Context-aware RAG is better on **3 of 6** quality metrics (story target: at least 3).

## Performance (FR-054)

| Measure | Value | Target |
| --- | --- | --- |
| Query latency P50 / P95 (full answer, context-aware) | 2.8 s / 6.7 s | P95 < 5 s |
| Retrieval latency P50 / P95 | 45 ms / 57 ms | |
| Document processing (chunk + embed), mean / max | 0.44 s / 3.72 s | < 2 min per document |
| Failure rate | 0% | < 2% |

## Multi-document reasoning and follow-ups (FR-056)

| Measure | Value |
| --- | --- |
| Comparison relation correct (agree / contradict) | 5 / 5 |
| Both documents used in the comparison | 5 / 5 |
| Follow-up answered when asked alone (no memory yet) | 8 / 10 |
| Follow-up answered with the previous question included | 9 / 10 |

Follow-ups such as "And elsewhere?" need conversation memory and question rewriting (FR-037 / FR-038, Sprint 3, Musaab). The second row shows the gain to expect once they land; this suite will measure them unchanged.

## Details

Wrong or refused answers (context-aware):

- What is the name of the university VPN? → 'According to Network and WiFi Access.txt: "Staff and students connect to the Wi-Fi network named FIU-Secure using their university account" [1].'
- When do I need a medical report for sick leave? → 'Staff must inform their line manager before 09:30 on the first day of sickness [1].'
- Can I connect my own router to the network? → 'refused'

Comparison scenarios:

- Compare the paid annual leave in the 2025 and 2026 leave policies. → expected **contradict**, got **contradict**
- Do the 2025 and 2026 remote work policies agree? → expected **contradict**, got **contradict**
- Compare the password length rules in standard A and standard B. → expected **agree**, got **agree**
- What is the difference in backup retention between the old and new backup policy? → expected **contradict**, got **contradict**
- Do the HQ and campus travel rules agree on flight class? → expected **agree**, got **agree**

## Known limitations

- **P95 latency 6.7 s vs the 5 s target.** The model runs on CPU only; the median is 2.8 s. A GPU (or a smaller model) is needed to meet the target for every question.
- **Follow-up questions** depend on conversation memory and question rewriting (FR-037 / FR-038), which are not built yet; the follow-up rows above measure the baseline.
- **Quoted fallback** (used when the model declines although a source sentence matches) quoted the wrong sentence 1 time(s): 'What is the name of the university VPN?'. Such answers are marked uncertain and quote the document verbatim, so the error is visible to the reader.
- The corpus is small (12 documents, one chunk each); results on long multi-chunk documents may differ.

Raw results: `docs/evaluation_results.json`. Regenerate this report without re-running: `python -m backend.evaluation.run --report-only`.
