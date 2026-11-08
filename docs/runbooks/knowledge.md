# Knowledge runbook — private playbook build, review, activation

Covers ADR-0009's private knowledge artifact: compile a playbook from local or
synthetic skill markdown, review, activate, and inspect what scans inject.

## The private-artifact rule (read first)

`$OFT_DATA/knowledge/` is private and gitignored, as are `books/`, `skills/`, and
`playbook/` (0700 dirs, 0600 files). **Never commit book text, `book-to-skill`
output, or a compiled playbook.** The open-source tree ships only the compiler,
converter adapter, synthetic fixtures, and docs.

## Build, review, activate

```bash
uv run oft knowledge build --source path/to/skill.md          # one file
uv run oft knowledge build --source path/to/skills/ --json   # a directory
uv run oft knowledge review --reviewed-by "<your name>"
uv run oft knowledge activate --actor cli --reason "initial playbook"
uv run oft knowledge status            # add --json for the envelope
```

`--source` accepts markdown skill output, not raw book text; output defaults to
`$OFT_DATA/knowledge/playbook` (`--output` overrides). Build deterministically
writes `playbook.md` with stable `K-<topic>-<n>` IDs plus `topics.json`,
`rules.json`, and `manifest.json`. `activate` refuses an unreviewed artifact; a
missing, tampered, or inactive artifact is reported as absent and disables
exact replay rather than fabricating knowledge.

## Manifest, provenance, and safety

`manifest.json` records `artifact_id` (sha256 over playbook/topics/rules),
`compiler_version`, `source_skills[]` (slug + hash), `created_at`, `license_note`
("private; do not distribute"), `reviewed_by`, and `token_count_method`
(`chars_div_4`, a documented chars/4 approximation) with `token_count`, budget,
section/topic lists, and dropped/summarized entries.
Every decision records `knowledge_artifact_hash`; a missing hash disables exact
replay. Any export/share path must carry the CLI's private warning. Knowledge
text is untrusted data: injected inside `<knowledge>...</knowledge>`, it cannot
change the schema, add tools, or create an order, and is dropped when over budget.
