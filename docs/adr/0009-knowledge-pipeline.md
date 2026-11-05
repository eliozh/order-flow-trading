# ADR-0009: Knowledge pipeline — book-to-skill compilation, prompt versioning, injection, retrieval

**Date**: 2026-11-06
**Status**: accepted
**Deciders**: architect (Stage 5); human approves at G3

## Context

The PRD requires `./books/` to be converted by `/book-to-skill` into a skill used as the LLM's knowledge source (MVP 4). Research (02-research.md §C) establishes:

- `book-to-skill` emits a ~4K-token `SKILL.md`, per-chapter summaries (0.8–3K each), a glossary, patterns, and a cheatsheet; eight books ≈ 200–320K generated tokens.
- Loading all eight full skills per scan would cost ~32K tokens per turn; the practical target is one compiled playbook at ~6–10K tokens plus optional chapter retrieval (2–6K per scan).
- Copyright: generated skills of copyrighted books must stay private; ship no book text or derived skill in the open-source repo; `books/` is gitignored.
- Prompt templates must be editable, restorable to default, and versioned; every decision records the template version used (MVP 12).

The open question from the PRD is the exact organization and injection granularity of `book-to-skill` output. This ADR answers it.

## Decision

Use a **compiled project-owned playbook** as the default injection, with **curated chapter retrieval** as an optional supplement. Never inject the full corpus.

### Artifact model

```
$OFT_DATA/knowledge/                  # gitignored, 0700
  books/                              # user-supplied; never committed
  skills/<book-slug>/                 # book-to-skill output, private
    SKILL.md
    chapters/chNN-slug.md
    glossary.md
    patterns.md
    cheatsheet.md
  playbook/
    manifest.json                     # artifact hash, source list, compiler version, created_at
    playbook.md                       # compiled 6–10K-token order-flow playbook
    topics.json                       # topic index: concept -> sections/chapters
    rules.json                        # decision rules, anti-patterns, checklists
```

`playbook/manifest.json` records: `artifact_id` (content hash), `compiler_version`, `source_skills[]` (slug + content hash), `created_at`, `license_note` ("private; do not distribute"), and a `reviewed_by` field for the human's sign-off. The artifact hash is stored on every prompt version and decision.

### Compilation

A setup command (`oft knowledge build`, also invoked from onboarding/settings) runs:

1. **Ingest** the book-to-skill outputs (never raw book text).
2. **Normalize** headings, terminology, and chapter metadata.
3. **Select** the product-relevant taxonomy:
   - market structure and auction theory;
   - absorption, exhaustion, trapped traders;
   - delta/CVD interpretation;
   - footprint reading (imbalance, stacked imbalance, absorption at levels);
   - volume profile (POC, value area, naked POC, composite);
   - open interest and funding context;
   - liquidation/stop-run context;
   - entry, invalidation, and trade management;
   - risk and psychology;
   - anti-patterns and review checklists.
4. **Compile** a single `playbook.md` with stable section IDs, plus `topics.json` and `rules.json`. Sections are labeled `K-<topic>-<n>` so prompts can cite them.
5. **Budget**: playbook body ≤ 10K tokens; glossary ≤ 1.5K; rules ≤ 2K. If the source corpus exceeds it, the compiler selects and summarizes; it does not concatenate.
6. **Review**: the human approves the playbook in settings (diff view, versioned). Approval is recorded in `audit_events`.

### Injection and retrieval

Two modes, both explicit in the prompt version:

- **Mode A (default): compiled playbook.** Inject `playbook.md` + the role's relevant `rules.json` sections. Target 6–10K tokens. This is the cheapest and most consistent option.
- **Mode B: topic retrieval.** The scan's CALC context (regime, footprint imbalance, value-area location, funding/OI state) maps to 2–5 topics via a deterministic rule table in `topics.json`; the corresponding chapter sections are retrieved (target 2–6K tokens). No embeddings, no vector database, no external service. Optional FTS5 over chapter text if the topic index is insufficient.

Retrieval is deterministic given the same CALC context and topic index; the selected section IDs are recorded on the decision (so a replay can reproduce the prompt). The prompt builder never includes more than the configured knowledge budget; if the budget is exceeded, it drops optional chapters and records a warning.

### Prompt template versioning

- `prompt_templates` identifies a role (`ANALYSIS`, `ENTRY_RECHECK`, `MANAGEMENT`, `REVIEW`); `prompt_versions` stores immutable versions with body, body hash, required variables, `knowledge_artifact_hash`, status, author, and reason.
- Editing creates a **new version**; it never edits in place. Activation is explicit. Restore-default creates a new version that reverts the body (it does not delete history).
- Required variables (`{{symbol}}`, `{{timeframe}}`, `{{metrics}}`, `{{schema}}`, `{{knowledge}}`, `{{trigger}}`, `{{position}}`) are validated at save time; a missing variable blocks activation with a named error (F10b).
- Every decision records `prompt_id`, `prompt_version`, `prompt_hash`, `knowledge_artifact_hash`, and the schema version. A missing version referenced by a historical decision is shown as `missing` and disables exact replay (F10b partial state).
- The system prompt (role, boundary rules, untrusted-data warning) is versioned separately from the user template; both are recorded.

### Safety and copyright

- Knowledge text is treated as **data**, delimited and never as instructions. The system prompt states precedence; injected text containing imperative language cannot change the output schema or trigger tools.
- No external news/social text in MVP (PRD scope), so the untrusted-text surface is limited to market data, book-derived content, and the model's own output.
- `books/`, `skills/`, and `playbook/` are gitignored and excluded from distribution. The open-source repository ships only the compiler workflow, a converter adapter, an empty `books/` directory, and documentation. No book text or derived skill is committed.
- The playbook manifest records the copyright constraint; export/share commands warn that the knowledge artifact is private.

## Alternatives Considered

### Alternative 1: Invoke one full book skill per analysis

- **Pros**: maximal fidelity to a single book; simple selection.
- **Cons**: ~4K + chapters per scan, 4–8K+ each; multiple books blow the budget; inconsistent knowledge across scans; harder to version.
- **Why not**: cost and consistency. The compiled playbook is cheaper and reviewable.

### Alternative 2: Inject all eight `SKILL.md` bodies

- **Pros**: no compilation step.
- **Cons**: ~32K tokens per turn, high cost, noisy context, no curation, and copyright exposure if shipped.
- **Why not**: research explicitly advises against it.

### Alternative 3: Embeddings + vector database RAG

- **Pros**: flexible retrieval over large corpora.
- **Cons**: another dependency/service, non-deterministic retrieval, index/version management, and no clear benefit for a curated 6–10K playbook. It also makes replay harder.
- **Why not**: deterministic topic mapping and a compiled artifact are sufficient and auditable. A vector store can be added later behind the same retrieval port.

### Alternative 4: Fine-tune a model on the books

- **Pros**: knowledge baked into weights.
- **Cons**: cost, reproducibility, provider lock-in, copyright risk, and no way to inspect what the model learned.
- **Why not**: out of scope and incompatible with provider neutrality.

## Consequences

### Positive

- A single, reviewable, versioned playbook makes LLM behavior more consistent and cheaper.
- Prompt versions and knowledge hashes make decisions reproducible (or explicitly non-reproducible when a version is missing).
- Copyright risk is contained by keeping artifacts private and shipping only the converter.
- Retrieval is deterministic and recorded.

### Negative

- Compilation is a real setup step and needs human review.
- The playbook is a bottleneck for knowledge updates; a new artifact is a new version and must be re-approved.
- Topic retrieval requires maintaining `topics.json`.

### Risks

- **Risk**: the compiled playbook loses nuance or introduces errors. **Mitigation**: human review, section IDs, diff view, and the ability to fall back to a chapter.
- **Risk**: a book contains prompt-injection-like text. **Mitigation**: treat knowledge as data, delimit it, no tools, output schema, and a review step.
- **Risk**: the user distributes a generated skill. **Mitigation**: gitignore, manifest license note, export warnings, and open-source checklist.
- **Risk**: prompt version drift makes replay impossible. **Mitigation**: immutable versions, hash storage, `missing` state, and a replay report that says so.

## Compliance checks

- No full-corpus injection; knowledge budget is enforced and recorded.
- Every decision records prompt and knowledge version hashes.
- Prompt edits create a new version; restore-default is non-destructive.
- `books/`, `skills/`, and `playbook/` are gitignored; no derived content is committed.
- Knowledge text cannot alter the schema or trigger tools.
