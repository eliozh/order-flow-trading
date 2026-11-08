# Knowledge runbook — private playbook build, review, activation

Covers ADR-0009's private knowledge artifact: compile a playbook from local or
synthetic skill markdown, review, activate, and inspect what scans inject.

## The private-artifact rule (read first)

`$OFT_DATA/knowledge/` is private and gitignored, as are `books/`, `skills/`, and
`playbook/`. Directories and files written by the CLI are created with `0700`
and `0600` permissions; anything you place in the tree yourself keeps whatever
permissions it already had. **Never commit book text, `book-to-skill` output, or
a compiled playbook.** The open-source tree ships only the compiler, synthetic
fixtures, and docs.

## Build, review, activate

```bash
uv run oft knowledge build --source path/to/skill.md          # one file
uv run oft knowledge build --source path/to/skills/ --json   # a directory
uv run oft knowledge review --reviewed-by "<your name>"
uv run oft knowledge activate --actor cli --reason "initial playbook"
uv run oft knowledge status            # add --json for the envelope
```

`--source` accepts markdown skill output, not raw book text; output defaults to
`$OFT_DATA/knowledge/playbook`. `--output` overrides that directory and the
choice is persisted in `location.json`, so the later `review`, `activate`, and
`status` commands read the same artifact instead of looking in an empty default
directory. A `location.json` that is unreadable, malformed, or points at a
missing/non-directory is reported as `KNOWLEDGE_LOCATION`; it is never silently
ignored in favour of a stale default artifact. Build deterministically writes
`playbook.md` with stable `K-<topic>-<n>` IDs plus `topics.json`, `rules.json`,
and `manifest.json`, and clears any previous `active.json` in the output
directory because the new manifest is a draft.

`review` prints the compiled playbook and role rules it is approving (rules are
shown sanitized, because a skill pack is untrusted input); `--json` keeps the
private warning alongside the exported playbook. `activate` refuses an artifact
that is not `APPROVED` by a named reviewer.

## Manifest, provenance, and safety

`manifest.json` records `artifact_id` (sha256 over playbook/topics/rules),
`compiler_version`, `source_skills[]` (slug + hash), `created_at`, `license_note`
("private; do not distribute"), `reviewed_by`, and `token_count_method`
(`chars_div_4`, a documented chars/4 approximation) with `token_count`, budget,
section/topic lists, and dropped/summarized entries. On load the `token_count`
and `sections[]` values are recomputed from `playbook.md` and must agree, so a
hand-edited manifest is rejected.

Content tampering (playbook/topics/rules) is detected because those files feed
`artifact_id`. Review fields are audit-only: `reviewed_by`, `reviewed_at`, and
`review_status` are not covered by `artifact_id`, so an `APPROVED` manifest is
required to carry a non-empty reviewer and timestamp, and an artifact is only
active (and injected) when the active pointer matches an approved manifest.

Every decision records `knowledge_artifact_hash`; a missing hash disables exact
replay for the knowledge block. An inactive artifact reports
`exact_replay=false` in the knowledge state and is not injected, rather than
fabricating knowledge. Any export/share path must carry the CLI's private
warning. Knowledge text is untrusted data: injected inside
`<knowledge>...</knowledge>`, it cannot change the schema, add tools, or create
an order, and is dropped (with the untrusted header preserved and any dropped
sections recorded) when over budget.
