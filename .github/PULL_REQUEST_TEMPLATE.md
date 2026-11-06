## Summary
<!-- One paragraph: what changed and why it is useful. -->

## Why / linked work
Closes #<issue>. Slice `<slice-id>` of milestone 1 in
`.claude/prds/order-flow-trading.prd.md`; implementation plan in
`.claude/plans/order-flow-trading.plan.md`.

## Test plan
- [ ] Tests written first and passing locally
- [ ] Python: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest --cov=oft --cov-fail-under=80`
- [ ] Web: `npm --prefix web run lint`, `npm --prefix web run typecheck`, `npm --prefix web run test -- --run`, `npm --prefix web run build`
- [ ] Coverage of new code is 80% or higher
- [ ] E2E run for critical journeys (if any)
- [ ] Manual check of the affected CLI/API/web flow

## UI changes
<!-- Screenshots at 375 / 768 / 1440 px next to the matching preview screen, or "no UI change". -->

## Risk and rollback
- Risk:
- Rollback: revert this PR. No data migrations / no production deploy in this milestone.

## Review findings
- CRITICAL: 0
- HIGH: <0, or each accepted one with the human's written reason>
- MEDIUM: <list, or issues filed>

## Checklist
- [ ] CI `verify` job is green
- [ ] No secrets, keys, or tokens in the diff
- [ ] User input validated; queries parameterized
- [ ] Security review run if this slice touches auth, input, queries, files, external APIs, crypto, payments, or secrets
- [ ] Documentation / ADRs updated where behavior or architecture changed
