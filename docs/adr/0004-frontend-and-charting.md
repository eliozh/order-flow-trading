# ADR-0004: Frontend stack and charting

**Date**: 2026-11-06
**Status**: accepted
**Deciders**: architect (Stage 5); human approves at G3

## Context

The approved design (DESIGN.md, G2 preview) is a dark, dense, keyboard-operable operator terminal: 208px navigation, 40px persistent status bar, 32px table rows, a 360px inspector, provenance stamps (`CALC`/`LLM`/`STALE`/`N/A`), a footprint ladder, volume profile, CVD pane, opportunity zones, markers, and WCAG 2.2 AA requirements. It must support 1440/1280/1024/768/375 viewports, with the 375px surface read-only except the kill switch.

Research fixes the charting constraint: TradingView **Advanced Charts is not licensed for private/open-source personal use**; only **Lightweight Charts (Apache-2.0)** is viable. Lightweight Charts has no built-in footprint, volume profile, or DOM ladder; those must be custom series/primitives/overlays.

## Implementation note (slice 08)

The slice-08 report surface currently renders an accessible inline SVG chart with a semantic
data-table equivalent instead of Lightweight Charts. `lightweight-charts` remains pinned in
`web/package.json` for the planned migration, but is not imported yet. Migrating the chart is
follow-up work; the SVG implementation preserves keyboard navigation, the data-table fallback,
and the ADR's accessibility contract.

## Decision

Use a **React 19 + TypeScript + Vite** single-page application, served as static assets by the FastAPI daemon.

- **Routing**: React Router, hash-free history routing with the daemon's SPA fallback. Routes match the preview exactly (`/watchlist`, `/scans/:scanId`, `/reports/:reportId`, `/opportunities/:oppId`, `/triggers/:triggerId`, `/positions/:positionId`, `/journal/:tradeId`, `/settings/*`, `/paper`, `/telegram`, `/system`, `/cli`, `/onboarding/:step`).
- **Server state**: TanStack Query for REST caching, retries, and invalidation; one `EventSource` for `/api/v1/events` that invalidates/patches the relevant query caches. No global Redux store; local UI state via React state/context and Zustand only if a cross-route concern emerges.
- **Styling**: plain CSS with custom properties generated from `design/design-tokens.json`; CSS Modules per feature. No utility framework, no component library that imposes its own visual language. Headless primitives from Radix UI (dialog, tabs, tooltip, popover, switch) are used for accessibility behavior, styled locally.
- **Charts**: **TradingView Lightweight Charts** (pinned, Apache-2.0) as the base, with:
  - candlestick + volume series in the price pane;
  - a separate CVD chart instance (or a native pane if the pinned version supports it) with a synchronized time scale and crosshair;
  - a custom series/primitive or canvas overlay for the footprint ladder and volume profile;
  - price lines/markers for zones, entries, exits, stops, targets;
  - a canvas overlay for session shading (`NO_TRADING` hatch) and data-quality badges.
  - If the pinned version's custom-series API cannot express a required overlay, the fallback is a thin canvas overlay synchronized to the chart's time/price scales — not a second charting library.
- **No Advanced Charts, no TradingView widgets, no external charting service.** No browser calls to Binance or LLM providers.
- **Security**: LLM narrative is rendered as plain text or a restricted Markdown subset (no raw HTML, no `dangerouslySetInnerHTML`, link allowlist). Strict CSP (`default-src 'self'`, no `unsafe-inline`/`unsafe-eval`). All API calls are same-origin. Secrets never reach the bundle.
- **Accessibility**: semantic tables, `aria-sort`, `aria-selected`, meters, dialogs via Radix, focus management, keyboard chart navigation, reduced-motion support, and the data-table alternative for the chart. The design's accessibility contract is a build requirement, not a later pass.
- **Build**: Vite outputs `web/dist`; FastAPI serves it with immutable asset caching and `index.html` fallback. Development uses Vite's dev server with a `/api` proxy to `127.0.0.1:7611`; production runs only the Python daemon.
- **Testing**: Vitest + React Testing Library for components; Playwright for the F01–F12 flows, keyboard-only paths, 375/768/1440 viewports, and the chart data-table fallback; axe-core for automated WCAG checks.

### Chart scope for MVP

Per DESIGN §13.3: candles, volume, CVD pane, volume profile (3–7 day window), footprint for the focused/decision bar, opportunity zones/trigger lines, entry/exit/stop/target markers, crosshair/tooltip, 15m/1h/4h timeframe switch, session shading, data-quality badges, symbol search. Drawing tools, saved layouts, bar replay UI, indicator scripting, live DOM ladder/heatmap, and multi-chart grids are deferred to milestone 7.

### Live update behavior

- Mark prices and PnL patch in place via SSE; tables never reorder on a tick.
- Scan progress is a named stage list, not a fake percentage.
- A global and per-table "pause live updates" control freezes values and shows `PAUSED` (design A6).
- Price flash is 600ms and disabled under `prefers-reduced-motion`.

## Alternatives Considered

### Alternative 1: Next.js / SSR

- **Pros**: file-based routing, SSR, ecosystem.
- **Cons**: a Node server to run and secure on a single machine; SSR adds no value for an authenticated local terminal; two runtimes and two deployment artifacts; more supply-chain surface.
- **Why not**: a static SPA served by FastAPI is simpler and keeps one process.

### Alternative 2: Vue or Svelte

- **Pros**: smaller runtime, excellent reactivity, good chart integration.
- **Cons**: smaller ecosystem for the specific accessible primitives and chart wrappers; no material advantage for this UI; the team's TypeScript/React familiarity is assumed.
- **Why not**: React has the broadest library and testing support for a dense, accessible terminal; the choice is low-risk and reversible at the component level.

### Alternative 3: Tauri/Electron desktop shell

- **Pros**: native window, filesystem access, no browser origin concerns.
- **Cons**: packaging/signing per OS, auto-update complexity, larger attack surface, and the design explicitly targets a browser UI plus Telegram. The daemon already runs locally.
- **Why not**: the browser is sufficient; a desktop shell adds distribution work without product value.

### Alternative 4: Custom canvas chart engine

- **Pros**: exact control over footprint/VP rendering.
- **Cons**: months of work for axes, scaling, crosshair, touch, accessibility, and performance; reinvents a solved problem.
- **Why not**: Lightweight Charts gives the axes/interaction/performance; only the product-specific overlays are custom.

### Alternative 5: TradingView Advanced Charts

- **Pros**: full TradingView feature set, including footprint.
- **Cons**: license excludes private/personal use; not compatible with a local single-user product or the open-source plan; attribution and public-implementation requirements.
- **Why not**: legally excluded by research §E3.

## Consequences

### Positive

- One runtime in production (Python daemon) and one static bundle.
- Lightweight Charts is small, canvas-based, Apache-2.0, and fast enough for the decision cadence.
- The design system is implemented as tokens, not overridden framework styles.
- Accessibility and keyboard paths are testable with Playwright/axe.

### Negative

- Footprint, volume profile, and session shading require custom drawing code and synchronization with the chart scales. This is the main frontend risk.
- Lightweight Charts' API changes across majors; the version must be pinned and upgraded deliberately.
- No built-in drawing tools; milestone 7 remains substantial work.

### Risks

- **Risk**: custom overlays desynchronize from the chart during zoom/pan. **Mitigation**: subscribe to visible-range and size changes, draw in the chart's coordinate space, and add visual regression tests at multiple zoom levels.
- **Risk**: dense tables and live updates hurt screen-reader usability. **Mitigation**: opt-in announcements, pause controls, stable row identity, and the design's A6/A8 rules.
- **Risk**: XSS through LLM narrative or symbol metadata. **Mitigation**: render as text, no raw HTML, CSP, sanitize/validate API data, and contract tests for hostile strings.
- **Risk**: bundle or dependency supply chain. **Mitigation**: lockfile, pinned versions, `npm audit`, review new dependencies, no postinstall scripts from untrusted packages.

## Compliance checks

- No Advanced Charts or TradingView widget is imported.
- No browser request goes to Binance, an LLM provider, or a CDN other than the local origin.
- Every chart has a "查看数据表" accessible alternative.
- The 375px surface is read-only except the kill switch.
- The SPA renders the provenance stamps from API fields, never infers them.
