# Design Direction — Order Flow LLM Trading System

**Stage:** 3 — design direction
**Status:** complete
**Date:** 2026-11-06
**Inputs:** `.claude/prds/order-flow-trading.prd.md`; `.claude/ecc-workflow/handoffs/02-research.md`
**Companion files:** `design/design-tokens.json`, `design/flows.md`, `design/design-preview.html`
**Scope:** direction, tokens, CLI surface, Web UI flows, chart anatomy. No framework choice, no application code.

---

## 1. Purpose

This is a **single-operator market terminal** for one trader who cannot watch the screen continuously. The interface has one job: turn a completed scan into a defensible entry decision, then keep open risk visible until it is closed.

The design must make three things unmistakable at all times:

1. **What the system knows** — deterministic order-flow metrics, with their data quality.
2. **What the model thinks** — LLM narrative and judgment, clearly separated from the metrics.
3. **What needs a human now** — triggered opportunities, entry re-checks, risk alerts, budget warnings.

The product is not a dashboard for browsing. It is a decision queue with a chart attached.

---

## 2. Audience

**Primary:** the project author — an individual trader with order-flow literacy, limited time, and 4h/8h decision cadence. Repeats the workflow on desktop during work breaks and on Telegram while away. Values density, keyboard speed, and trustworthy state over visual comfort.

**Secondary (post open-source):** self-hosting individual traders who bring their own LLM and Binance API keys. They will read the CLI help first and the UI second.

**Not the audience:** order-flow beginners, teams, institutions, HFT/market-making operators, native-mobile users. The design should not soften the interface for them.

**Audience consequences:**

- Dense tables and small-but-legible data type are correct; spacious marketing cards are wrong.
- Every number needs provenance and units; the audience will notice fabricated precision.
- CLI parity matters as much as the Web UI; the CLI is the reference surface for automation and scripting.
- Mobile is a read-and-alert surface. Telegram is the primary mobile product; the 375px Web UI is a compact fallback, not a full editor.

---

## 3. Tone

**Operator terminal, not a chatbot.** The system reports, the human decides. Copy is terse, factual, and specific. No exclamation marks, no encouragement, no anthropomorphic AI language, no decorative personality.

**Geek, but functional geek.** The aesthetic references terminals and professional trading software, not "cyberpunk" decoration. No neon glow, no scanlines, no fake CRT effects, no ASCII art in product surfaces. The geek quality comes from density, monospace numerals, precise alignment, visible grid, keyboard shortcuts, and honest data labels.

**High contrast, low ornament.** Near-black canvas, bright foreground, color reserved for meaning. Every border earns its place by separating data regions.

---

## 4. Primary action and hierarchy

### Primary action

**Review a triggered opportunity and accept or reject the entry.**

This is the moment the product exists for: the scan found a zone, price reached it, the LLM re-checked the entry, and a bounded decision is waiting. The UI must make that decision the most salient thing on screen without hiding current risk.

### Secondary actions

- Monitor open positions and risk utilization.
- Trigger or inspect a scan and read the report.
- Review closed trades against the signals that produced them.

### Global hierarchy

| Rank | Layer | Why it ranks here | Where it lives |
|---|---|---|---|
| 1 | Environment, kill switch, system health | A wrong environment or a dead daemon invalidates everything below. | Persistent 40px status bar, always visible |
| 2 | Pending human decisions and risk alerts | Time-sensitive, human-blocking, affects money. | Dashboard action queue; amber/red banners; badge counts |
| 3 | Open positions, exposure, PnL | Current risk is the next-most urgent fact. | Positions route; dashboard position strip |
| 4 | Latest scan/report and opportunity ledger | The input to the next decision. | Scans, Reports, Opportunities |
| 5 | Trade journal and review | Learning loop; not time-sensitive. | Journal |
| 6 | Settings and system | Rarely visited, high consequence. | Settings, System |

### Per-screen primary action

| Screen | Primary action | Must be visually dominant over |
|---|---|---|
| Dashboard | Review next pending decision | Recent reports, budget charts |
| Scan detail | Follow per-symbol progress and open the report | Scan history |
| Report | Read LLM narrative, verify against CALC metrics, open linked opportunities | Export/share |
| Opportunity detail | Inspect trigger conditions and wait state | Notes |
| Trigger review | Accept or reject entry | Model rationale (read it, but the action is the exit) |
| Positions | Request a management decision / inspect risk | History |
| Settings | Save a scoped change | Navigation |
| Journal | Compare signal vs outcome | Filters, export |

**Anti-pattern rejected:** a "hero" dashboard with large tiles and a chart as wallpaper. The operator needs row-level scanning, not a landing page.

---

## 5. Target viewports

| Viewport | Role | Behavior summary |
|---|---|---|
| **1440 × 900** | **Primary reference** | 208px nav, 40px status bar, content grid, 360px inspector when a row is open. Tables show 12–18 rows. Full chart (price + volume + CVD + footprint). |
| 1920 × 1080 | Large desktop | Content capped at 1600px; extra width goes to the chart, not to wider prose. Inspector stays 360px. |
| 1280 × 800 | Compact desktop | Nav collapses to 56px icon rail at <1280px; inspector becomes an overlay sheet. Tables lose low-priority columns. |
| 1024 × 768 | Small laptop | Icon rail; chart footprint moves below the price pane; settings use single-column forms. |
| 768 × 1024 | Tablet | Icon rail + 56px; tables keep horizontal scroll with a sticky symbol column; detail is a full-height sheet; touch targets 44px. |
| 375 × 812 | Phone | **Read-only compact fallback.** Single column, bottom tab bar, price + CVD chart only, no settings editing. Persistent line: "移动端主入口为 Telegram". Kill switch remains available. |

The 375px surface deliberately does not attempt full CLI/Web parity. Telegram covers push, queries, and alerts; the web page exists for a quick read when Telegram is inconvenient.

---

## 6. Visual direction

### 6.1 Direction statement

**A dark, high-contrast order-flow terminal with a visible grid, monospace numerals, sharp corners, and a strict three-color market language.** It should look closer to a Bloomberg panel or Sierra Chart Numbers Bars window than to a SaaS analytics product. Color is a data channel, not decoration.

### 6.2 Principles

1. **Dark-first, near-black, not pure black.** `#0A0B0D` keeps 1px grid lines visible; pure `#000` absorbs them. Text is `#E8EDF2`, not `#FFFFFF`, to reduce halation on dense 13px numerals while still reaching 16.7:1.
2. **Color means something or it is removed.** Green = up/buy, red = down/sell, amber = attention/warning, cyan = interactive/focus/LLM provenance, blue = informational series, gray = neutral or disabled. No accent used decoratively.
3. **Numbers are monospaced and aligned.** Prices, sizes, CVD, delta, funding, PnL, timestamps, IDs, and confidence values use the numeric font stack with `tnum` and `zero`. This prevents width jitter during live updates and lets the eye scan columns.
4. **Grid over shadow.** Regions are separated by 1px borders and four background steps. Cards-in-cards, floating rounded panels, and ambient drop shadows are prohibited.
5. **Density is the default.** 32px table rows, 28px compact rows, 13px data type, 4px spacing base. Generous whitespace is reserved for the narrative column, not for the operational surfaces.
6. **Sharp geometry.** Panels, tables, cells, and chart frames use 0 radius. Inputs, buttons, and chips use 2px. This aligns to the grid and avoids the "rounded SaaS card" read.
7. **Two provenances, never blended.** Deterministic metrics carry a `CALC` stamp; model output carries an `LLM` stamp and a cyan left rule. No LLM sentence is styled as a measurement.
8. **Partial data is normal, not an error.** TradFi history starts at listing, USDⓈ-M liquidation history does not exist, TradFi metrics are irregularly sampled. The UI labels these conditions rather than hiding them.
9. **Motion only for state change.** Color fades, focus, pane open, and a 600ms price flash. No scroll reveals, no parallax, no looping decoration.
10. **One memorable detail: the provenance spine.** A 2px left rule plus a monospace stamp runs through reports, opportunity cards, and decisions: `CALC` (gray) for code-computed facts, `LLM` (cyan) for model judgment, `STALE` (amber), `N/A` (red). It makes the PRD's "deterministic belongs to code, subjective belongs to LLM" boundary visible at a glance.

### 6.3 Reference ledger

Every major choice has a named reference. "Because it looks good" is not a reason.

| Choice | Reference | Why it fits this product |
|---|---|---|
| Near-black canvas + high-contrast off-white text | Bloomberg Terminal; Sierra Chart; TradingView dark theme | Supports long sessions; keeps green/red/amber maximally discriminable; matches the operator's existing mental model. |
| Visible 1px grid and hard panel edges | Bloomberg panels; k9s; btop | Data regions stay legible at high density; no shadow haze between live-updating values. |
| Sharp 0px corners on panels and cells | Bloomberg grid; terminal TUI | Aligns columns and rows precisely; signals "instrument", not "marketing card". |
| Monospace tabular numerals with slashed zero | Sierra Chart Numbers Bars; ATAS; Bloomberg | Prevents digit-width jitter and column drift during live ticks; slashed zero distinguishes 0/O in IDs and prices. |
| Green/red used only as marks and short labels | TradingView dark theme candles | Large saturated fills would dominate the screen and compete with opportunity zones; small marks retain meaning without color flooding. |
| Cyan as the interactive accent | Classic terminal selection color; k9s highlight | Green, red, and amber are already reserved for market semantics; cyan is the remaining high-contrast hue that does not imply direction. |
| Amber for opportunity zones and human-attention states | Bookmap zone overlays; ATAS highlight bands | Amber reads as "watch/decide" without implying long or short, and stays distinct from entry/exit markers. |
| Footprint as discrete bid/ask heat steps | Sierra Chart Numbers Bars; ATAS footprint | Discrete steps are readable as values; a smooth continuous gradient becomes decorative noise. Fill is capped for text contrast. |
| Separate CVD pane under price | Sierra Chart; ATAS; TradingView CVD studies | CVD is a derived cumulative series with its own scale; overlaying it on price creates two y-axes and misreads. |
| Volume profile anchored to the plot edge | Sierra Chart volume profile; TradingView fixed-range VP | Keeps the price axis readable and separates "structure" from "time series". |
| Persistent status bar with environment and kill switch | Bloomberg terminal status line; k9s header | Environment errors are catastrophic; the operator must never wonder whether this is paper or live. |
| CLI box-drawing tables with right-aligned numerics | btop; k9s; lazygit | Gives scripts and humans the same data with professional alignment; avoids decorative CLI spinners and emoji. |
| Chinese UI with English technical terms | Operator's own vocabulary; Chinese trading communities | CVD, footprint, POC, VAH/VAL, OI, funding, trigger, paper/live are used in English by the audience; translating them reduces precision. |
| Telegram-first mobile | PRD decision: no native app; read-only bot | Push notifications are the actual mobile job; a phone web editor adds risk without adding value. |

### 6.4 Explicit anti-references

Do **not** use: purple-to-blue gradients, gradient text, glassmorphism, blurred translucent panels, floating rounded cards, neumorphism, ambient glow, decorative scanlines, animated backgrounds, stock-photo hero sections, oversized rounded stat tiles, emoji as status icons, or "AI magic" sparkle motifs.

---

## 7. Color

### 7.1 Palette

| Role | Token | Value | Use |
|---|---|---|---|
| Canvas | `semantic.background.canvas` | `#0A0B0D` | App background |
| Panel | `semantic.background.panel` | `#0E1014` | Primary panels, tables |
| Raised | `semantic.background.raised` | `#12151B` | Popovers, inspector, tooltips |
| Inset | `semantic.background.inset` | `#07080A` | Chart plot, code blocks |
| Hover | `semantic.background.hover` | `#171B22` | Row/control hover |
| Selected | `semantic.background.selected` | `#16242B` | Selected row/nav, paired with a 2px cyan left rule |
| Primary text | `semantic.text.primary` | `#E8EDF2` | Body, values, headings |
| Secondary text | `semantic.text.secondary` | `#9AA6B2` | Labels, prose support |
| Muted text | `semantic.text.muted` | `#828E9E` | Un-tinted metadata, axis labels, stamps. Never on selected rows, `accent.mutedSurface`, or any chip |
| Disabled text | `semantic.text.disabled` | `#56616F` | Inactive only; never values, status, errors, timestamps, or axis labels |
| Border subtle | `semantic.border.subtle` | `#1D222B` | Internal table rules |
| Border default | `semantic.border.default` | `#262C36` | Panel edges and table dividers only (decorative) |
| Border strong | `semantic.border.strong` | `#3A424F` | Focused panel / overlay edge only (decorative) |
| Border control | `semantic.border.control` | `#6B7785` | Inputs, selects, checkboxes, radios, switches, segmented controls, search |
| Accent | `semantic.accent.primary` | `#4DD8E6` | Focus, links, active tab, LLM provenance |
| Up / long | `semantic.market.up` | `#35D07F` | Positive delta, long, candle up |
| Down / short | `semantic.market.down` | `#FF5D6A` | Negative delta, short, candle down, live environment |
| Flat | `semantic.market.flat` | `#8B98A8` | Zero change, neutral state |
| Warn | `semantic.status.warn` | `#FFB020` | Budget threshold, stale data, pending attention |
| OK | `semantic.status.ok` | `#4FD1A5` | Paper environment, healthy check |
| Info | `semantic.status.info` | `#62B6FF` | Informational badges and secondary series |
| Danger | `semantic.status.danger` | `#FF6B6B` | Destructive action, failed job |

### 7.2 Contrast (WCAG 2.2, sRGB)

Measured with the standard relative-luminance formula. Ratios below are minimums against the three main surfaces.

| Foreground | On canvas `#0A0B0D` | On panel `#0E1014` | On raised `#12151B` | AA (4.5:1) |
|---|---:|---:|---:|---|
| Primary text `#E8EDF2` | 16.71 | 16.16 | 15.52 | Pass (AAA) |
| Secondary text `#9AA6B2` | 7.94 | 7.68 | 7.37 | Pass (AAA) |
| Muted text `#828E9E` | 5.92 | 5.72 | 5.49 | Pass |
| Accent `#4DD8E6` | 11.51 | 11.13 | 10.69 | Pass (AAA) |
| Up `#35D07F` | 9.83 | 9.50 | 9.12 | Pass (AAA) |
| Down `#FF5D6A` | 6.59 | 6.37 | 6.12 | Pass (AAA) |
| Flat `#8B98A8` | 6.71 | 6.49 | 6.23 | Pass |
| Warn `#FFB020` | 10.77 | 10.41 | 10.00 | Pass (AAA) |
| OK `#4FD1A5` | 10.31 | 9.97 | 9.57 | Pass (AAA) |
| Info `#62B6FF` | 9.05 | 8.75 | 8.40 | Pass (AAA) |
| Danger `#FF6B6B` | 7.09 | 6.86 | 6.59 | Pass |
| Disabled `#56616F` | 3.13 | 3.02 | 2.90 | Fail — inactive only |

Additional measured pairs:

- Selected background `#16242B`: primary text 13.49:1, secondary 6.41:1, accent 9.29:1.
- Status chips (text on a 14%-tint surface): up 7.50:1, down 5.39:1, warn 8.12:1, info 6.97:1, accent 8.52:1. All pass AA for normal text.
- Live banner `#2A0E12`: white 17.12:1, down 6.01:1, warn 9.81:1.
- Footprint heat is capped at 42% fill so `#E8EDF2` text stays at ≥6.6:1 (green) and ≥8.4:1 (red). Levels 4–5 add a 2px saturated heat bar instead of more fill, preserving text contrast.

Disabled text is exempt under WCAG 1.4.3 only because it is inactive. It must never carry values, status, errors, timestamps, or axis labels.

#### Tinted surfaces, controls, and chart graphics

`text.muted` was raised from `#7C8898` to `#828E9E`:

| Pair | Ratio | Verdict |
|---|---:|---|
| `text.muted` on selected `#16242B` | 4.78 | Pass, but still banned by the hard rule below |
| `text.muted` on `accent.mutedSurface` `#172C31` | 4.38 | **Prohibited** — use `text.secondary` |
| `text.muted` on chip neutral `#1D222B` | 4.80 | Prohibited — chips always use the chip's own text token |
| `text.muted` on chip ok `#132B23` | 4.52 | Prohibited |
| `text.muted` on chip info `#1A2735` | 4.56 | Prohibited |
| `text.muted` on chip warn `#302616` | 4.46 | **Prohibited** — use `text.secondary` |
| `text.muted` on chip danger `#301B20` | 4.84 | Prohibited |

**Hard rule:** `text.muted` is for un-tinted panel/canvas metadata only. It must not be used on selected rows, `accent.mutedSurface`, or inside any chip; use `text.secondary` there (6.4:1+ measured on selected and equivalent surfaces).

Control boundaries (WCAG 1.4.11, 3:1):

| Token | On canvas | On panel | On raised | On selected |
|---|---:|---:|---:|---:|
| `border.control` `#6B7785` | 4.32 | 4.17 | 4.01 | 3.48 |

`border.default` (1.36 on panel) and `border.strong` (1.80) are decorative panel edges and table dividers only; they must not be the sole boundary of an interactive control. Inputs, selects, checkboxes, radios, switches, segmented controls, and search fields use `border.control`.

Chart graphics (non-text, 3:1):

| Mark | Value | Ratio on `#0A0B0D` |
|---|---|---:|
| Volume bar, up | `#35D07F` @ 0.65 | 4.61 |
| Volume bar, down | `#FF5D6A` @ 0.65 | 3.30 |
| Volume-profile bar | `#8B98A8` @ 0.65 | 3.49 |

Volume must not rely on hue alone: up bars carry a 1px full-strength top cap; down bars carry a 1px full-strength outline. Direction is encoded by shape as well as color.

### 7.3 Semantic rules

- **Up/down never used for UI state** (success, delete, warning). Success uses `status.ok`; destructive uses `status.danger`. They are visually close to market colors by design, but the labels and context must disambiguate.
- **Green and red are not used as large background fills.** Use a short label, a 2px mark, a candle body, or a wash at ≤14% opacity. Opportunity zones own the larger translucent bands.
- **Amber is attention, not error.** Error is red. Amber covers budget/risk warnings, stale data, and an open opportunity zone waiting for price. A **triggered** opportunity is actionable and uses the cyan accent; a failed schema is red.
- **Cyan is interaction and LLM provenance.** A cyan border means "clickable" or "model-generated", never "positive".
- **Only one accent per row.** A row can have a market color for the value and a status chip, but not a third competing accent.
- **Environment color is persistent.** Paper = teal `#4FD1A5`; Live = red `#FF5D6A`; degraded = amber; offline = gray. The environment badge is never hidden or scrolled away.
- **Muted is never a chip/selected color.** `text.muted` is banned on selected rows, `accent.mutedSurface`, and all chip backgrounds; use `text.secondary`.
- **Disabled never carries information.** It is inactive-only: no values, status, errors, timestamps, or axis labels.
- **Control boundaries are explicit.** Interactive controls use `border.control`; `border.default` and `border.strong` are decorative edges/dividers and are exempt from the 3:1 rule.
- **Volume direction is not color-only.** Up bars carry a 1px full-strength top cap; down bars carry a 1px full-strength outline.

---

## 8. Typography

### Families

- **UI sans** for Chinese prose, navigation, labels, forms, settings: `system-ui`, `Noto Sans SC`, `PingFang SC`, `Microsoft YaHei`.
- **Mono** for all numeric and machine data: prices, quantities, CVD/delta, OI, funding, PnL, timestamps, IDs, symbol codes, JSON, CLI output, chart axes.
- **Numeric stack** equals mono, with `font-variant-numeric: tabular-nums slashed-zero` and `"tnum" 1, "zero" 1`.

### Scale

| Token | Size | Line height | Use |
|---|---:|---:|---|
| `micro` | 11px | 1.2 | Latin/numeric stamps only (`CALC`, `LLM`, `POC`, time-axis labels). Never Chinese sentences. |
| `caption` | 12px | 1.35 | Table headers, metadata, field hints |
| `data` | 13px | 1.35 | Table cells, chart values, dense rows, CLI-like panels |
| `body` | 14px | 1.6 | Chinese body copy, forms, narrative reading |
| `title3` | 16px | 1.3 | Panel titles, section headers |
| `title2` | 20px | 1.25 | Page titles |
| `title1` | 24px | 1.2 | Major detail headings (rare) |
| `display` | 32px | 1.1 | Only key PnL/equity summaries; never a marketing hero |

### Rules

- **Chinese body is never smaller than 14px.** 13px is for numerals, tickers, and Latin technical terms; 11px is for stamps and axis labels only.
- **Numeric right alignment** in table columns, with fixed decimal precision per symbol tick size. Do not strip trailing zeros.
- **No locale thousands separators in dense tables** (e.g. `123456.78`); use separators in summary cards and prose (`123,456.78`). This keeps columns narrow and copyable.
- **Weights:** 400 for prose, 500 for numeric values, 600 for labels/titles, 700 for page titles and key totals. Avoid 800+.
- **Uppercase only for Latin stamps/labels**, with `0.04em`–`0.08em` letter spacing. Never uppercase Chinese.
- **Prose measure** is 68–72 characters (about 720–780px at 14px). Report narrative does not stretch to 1600px.
- **Mixed-language spacing:** keep one space between Chinese and English technical terms in copy; do not insert spaces inside identifiers.

### Why monospace for numbers

Trading values update in place. Proportional numerals change width, causing columns to shimmer and the eye to lose its place. Tabular mono numerals also align decimal points across rows, which is the primary comparison task in a positions or opportunity table.

---

## 9. Density, spacing, and grid

- **Spacing base:** 4px, with 2px and 6px steps for terminal packing. Scale: 0, 2, 4, 6, 8, 12, 16, 20, 24, 32, 40, 48, 64, 80.
- **Table rows:** 32px default; 28px compact for watchlist/opportunity tables; 40px only when a row contains a chart sparkline or two-line label.
- **Control heights:** 28px default, 32px for primary actions, 24px for inline icon buttons. Touch targets on ≤768px are 44px.
- **Page padding:** 24px at ≥1024px, 16px below. Panel internal padding: 12px/16px. Never nest a panel inside a panel with another border.
- **Grid:** 12 columns, 12px gutter, max content 1600px. The chart is allowed to take the full remaining width; prose is capped.
- **Rules:** 1px borders. Table header uses a 1px bottom border and a dim uppercase mono label; row separators use `border.subtle`. No zebra striping — use hover and selection instead, because zebra bands compete with heatmaps.
- **Information per viewport:** at 1440×900, the primary tables should show 12–18 rows without scrolling. If a design cannot, it is too spacious.

---

## 10. Surfaces, borders, and elevation

- Four background steps (`inset`, `canvas`, `panel`, `raised`) establish depth. Borders, not shadows, define regions.
- **Radius:** 0 for panels, tables, chart frames, and cells; 2px for inputs, buttons, and chips; 3–4px only for modal corners if a browser requires rounding for the focus ring. No fully rounded cards.
- **Elevation:** `e0` is the default (border only). `e1` for popovers and the inspector sheet, `e2` for modals, `e3` for the live-arming confirmation. Shadows are hard, dark, and short; no soft ambient glow.
- **Dividers:** use 1px lines; never use gradient fades as dividers.
- **Selected state:** background step + 2px cyan left rule. Do not use a full cyan border on table rows; it fights the grid.
- **Control boundaries:** inputs, selects, checkboxes, radios, switches, segmented controls, and search fields use the 1px `border.control` (`#6B7785`, 4.17:1 on panel). `border.default` and `border.strong` remain for panel edges and table dividers only.
- **Focus:** 2px cyan ring, 2px offset; inset on dense table rows. Visible on every interactive element.

---

## 11. Motion

| Token | Duration | Use |
|---|---:|---|
| `fast` | 70ms | Hover, chip state |
| `base` | 120ms | Button press, tab underline, focus |
| `medium` | 180ms | Inspector sheet, dropdown, banner reveal |
| `slow` | 240ms | Modal, large pane change |
| `priceFlash` | 600ms | Value flash on live update, then settle |
| `skeletonPulse` | 1400ms | Opacity pulse only, no shimmer gradient |

- Easing: `cubic-bezier(0.2, 0, 0, 1)` for enter/standard; `cubic-bezier(0.4, 0, 1, 1)` for exit.
- Animate color/opacity only. Never animate table row height, chart geometry, or numeric counters.
- Respect `prefers-reduced-motion`: all durations collapse to 0.01ms; price flash and skeleton pulse stop.
- No decorative animation. A spinner is allowed only when there is no measurable progress; scans use named per-symbol stages instead.

---

## 12. Layout and responsive behavior

### Desktop shell (1440)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ STATUS  OFT · PAPER · daemon ● · next scan 02:14:07 · budget $2.18/$10 │
├──────────┬──────────────────────────────────────────────┬───────────────┤
│ NAV 208  │ PRIMARY WORK AREA                            │ INSPECTOR 360 │
│          │ (table / report / chart)                     │ (selected row)│
│ Dashboard│                                              │               │
│ Watchlist│                                              │               │
│ Scans    │                                              │               │
│ Opportun.│                                              │               │
│ Positions│                                              │               │
│ Paper    │                                              │               │
│ Journal  │                                              │               │
│ Settings │                                              │               │
└──────────┴──────────────────────────────────────────────┴───────────────┘
```

- The status bar is sticky and is the only element with `z-index` above modals except toasts.
- The nav shows environment color via a 2px left rail on the active item and a persistent environment chip in the status bar.
- The inspector is a detail pane, not a modal. On <1280px it becomes a full-height sheet from the right with a 1px border and no backdrop blur.

### Responsive rules by surface

| Surface | ≥1280 | 768–1279 | <768 |
|---|---|---|---|
| Nav | 208px expanded | 56px icon rail, labels in tooltips | Bottom tab bar (Dashboard, Opportunities, Positions, Journal, More) |
| Tables | All columns | Drop secondary columns; horizontal scroll with sticky symbol | Row list: symbol + primary value + state chip; tap expands a key-value sheet |
| Chart | Price + volume + CVD + right footprint ladder | Price + volume + CVD; footprint below, collapsed | Price + CVD toggle; volume profile and footprint hidden behind an explicit "需要桌面端" label |
| Inspector | Fixed 360px | Overlay sheet | Full-screen sheet |
| Settings | Two-column form + preview | Single column | Read-only with a "在桌面端或 CLI 修改" note |
| Status bar | Full metrics | Environment + kill switch + next scan | Environment + kill switch only |
| Touch targets | 28–32px | 44px for controls | 44px minimum |

**Mobile stance:** Telegram is the mobile product. The 375px Web UI is read-only by design, except for the kill switch (safety must never be blocked). A persistent one-line banner explains this and links to the Telegram channel. Edit actions are disabled, not hidden without explanation; tapping them shows "此操作请在桌面端或 CLI 完成".

### Table behavior details

- Numeric columns are right-aligned and fixed-width by data type, not by content.
- The symbol column is sticky during horizontal scroll at 768–1279.
- Sort is indicated by a small triangle in the header and a 2px bottom rule on the active column; no background fill.
- Row hover changes background only; row selection adds the left cyan rule.
- Empty state occupies the table body with a single explanatory row, not a floating illustration.

---

## 13. Chart panel anatomy

### 13.1 Constraint

TradingView **Advanced Charts is not licensed for private/personal use** and is excluded. **Lightweight Charts (Apache-2.0)** is the viable base. Footprint, volume profile, opportunity zones, and markers are implemented as custom series/overlays. The design must not promise Advanced-Charts-only features.

### 13.2 Anatomy

```
┌───────────────────────────────────────────────────────────────┐
│ SYMBOL  BTCUSDT · 4h · CALC 98.2% · LIQ since 11-06 · [modes] │  chart header / provenance
├───────────────────────────────────────────────────────────┬───┤
│ Price pane (62%)                                          │ F │
│  · candles (up/down)                                      │ P │
│  · opportunity zone band (amber dashed, label + confidence)│   │
│  · trigger line (amber dashed)                            │ l │
│  · entry/exit/stop/target markers                         │ a │
│  · POC (solid), VAH/VAL (dotted), value-area fill        │ d │
│  · volume profile anchored left (18%)                     │ d │
│  · session shading: regular / pre / after / noTrading     │ e │
│  · crosshair + linked tooltip                             │ r │
├───────────────────────────────────────────────────────────┤   │
│ Volume pane (14%): histogram, up/down at 65% + cap/outline │   │
├───────────────────────────────────────────────────────────┤   │
│ CVD pane (24%): cyan line, zero line, divergence marker   │   │
└───────────────────────────────────────────────────────────┴───┘
  Right dock: footprint ladder for the focused bar
  (price rows × bid/ask heat, delta, imbalance marker)
```

- **Header strip:** symbol, timeframe, data-quality stamp, liquidation-coverage stamp, overlay toggles. Always visible, 32px.
- **Footprint dock:** one row per price bucket, two heat cells (bid sell-aggressor / ask buy-aggressor) and a delta column; max 40 rows; the focused bar is selectable. Cells cap fill at 42% and add a 2px heat bar at the top two levels.
- **Opportunity zones:** translucent amber band with a dashed top/bottom edge, a label (`OPP-20261106-014 · LONG · 0.72`), and a state chip. Once triggered, the band turns cyan. Expired = gray; invalidated = red.
- **Markers:** entry triangle (cyan, direction-aware), exit square (off-white), stop × (red), target ○ (green). Markers are 10–12px and sit at the price coordinate, never floating in the middle of a candle body.
- **Volume encoding:** bars use 0.65 fill alpha (up 4.61:1, down 3.30:1 on canvas) plus a non-color channel — up bars have a 1px full-strength top cap, down bars a 1px full-strength outline. Volume-profile bars use 0.65 alpha (3.49:1). Direction is never hue-only.
- **POC / value area:** POC solid off-white, VAH/VAL dotted muted, value-area fill at 6% gray. These are structure lines and must not compete with opportunity bands.
- **Session shading:** regular = canvas; pre/after = slightly lifted; overnight = another step; `NO_TRADING` = subtle hatch. The underlying cash-market session is a regime flag, not a trading halt (research §A2).
- **Data-quality badges:** `COMPLETE`, `PARTIAL`, `STALE`, `MISSING`, `ESTIMATED`. They sit in the header and, for affected series, in the legend. Never silently draw an estimated series as if it were measured.
- **Tooltip:** time (UTC), O/H/L/C, volume, delta, CVD, OI Δ, funding, session, data-quality flag, and provenance stamps. Max width 320px; 120ms delay.

### 13.3 MVP vs deferred ("TradingView-like")

| Capability | Status | Note |
|---|---|---|
| Candles, volume, CVD pane | **MVP** | Lightweight Charts base + one custom CVD series. |
| Volume profile (POC/VAH/VAL), fixed 3–7 day window | **MVP** | Computed by code; profile is a custom overlay. |
| Footprint for the focused/decision bar | **MVP** | Custom drawing; one bar at a time, not continuous footprint history. |
| Opportunity zones + trigger line | **MVP** | Custom price-range overlay. |
| Entry/exit/stop/target markers | **MVP** | Series markers. |
| Crosshair, linked tooltip, 15m/1h/4h switch | **MVP** | No drawing tools. |
| Session shading / `NO_TRADING` flag | **MVP** | Important for TradFi perps. |
| Data-quality and provenance badges | **MVP** | Product-specific. |
| Multi-pane link, symbol search | **MVP** | Jump from watchlist/report. |
| Drawing tools, trendlines, Fibonacci | Deferred (milestone 7) | Months of work; not needed for the closed loop. |
| Saved layouts, multi-chart grids, templates | Deferred (milestone 7) | |
| Bar replay / market replay | Deferred (milestone 7) | Event replay belongs to research tooling first. |
| Indicator scripting / plugin marketplace | Deferred | Conflicts with the deterministic-metrics boundary. |
| Live DOM ladder and L2 heatmap (Bookmap-like) | Deferred | Full historical L2 is unavailable; live ladder adds cost and scope. |
| Footprint variants (imbalance filters, multiple modes) | Deferred | One readable footprint mode first. |
| Comparison overlays, spread charts | Deferred | |
| Multi-timeframe candle overlay | Deferred | |

---

## 14. Provenance system (the memorable detail)

The PRD's hardest boundary is **deterministic belongs to code, subjective belongs to LLM**. The design makes that boundary visible everywhere:

| Stamp | Visual | Meaning |
|---|---|---|
| `CALC` | Gray mono micro label; 1px gray rule; no fill | Computed by code from Binance data. Reproducible. |
| `LLM` | Cyan mono micro label; 2px cyan left rule; 6% cyan wash | Model-generated narrative or judgment. Not reproducible across models; always paired with model, prompt version, and confidence. |
| `STALE` | Amber stamp + amber left rule | Data older than its freshness budget. |
| `N/A` | Red stamp + red left rule | Required data does not exist (e.g. historical USDⓈ-M liquidations before deployment). |

Rules:

- Every report is split into a **CYPHER/CALC band** (metrics table, charts) and an **LLM band** (narrative, proposed zones, confidence). They never share a borderless paragraph.
- Every trigger decision card shows: `LLM` decision + rationale + confidence, then `CALC` risk validation result (which cap bound the size), then the human action.
- In the CLI, LLM output blocks begin with `LLM ›`; deterministic output has no prefix. `--format json` carries `"provenance": "calc" | "llm"` on every block.
- Confidence is a number 0–1 plus a band: `≥0.70 高`, `0.40–0.69 中`, `<0.40 低`. The UI never says "the AI is confident".
- The provenance stamp is the only place cyan is used inside body content; it is a functional marker, not decoration.

---

## 15. CLI presentation

The CLI is a first-class product surface, not a debug console.

### Grammar

```
oft <group> <verb> [arguments] [--flags]
oft scan run --period 4h --format json
oft opp list --state triggered --format table
```

Global flags: `--format table|json|ndjson|csv`, `--no-color`, `--color=always`, `--ascii`, `--quiet`, `--verbose`, `--local`, `--yes` (never valid for Live arming).

### Visual rules

- Output uses the same semantic colors as the Web UI when stdout is a TTY; otherwise plain text.
- Tables use box-drawing separators between header and rows, not a full outer box; numeric columns are right-aligned; header labels are uppercase mono 11px equivalent (terminal default); timestamps are ISO-8601 UTC unless `--local`.
- No emoji, no ASCII art, no decorative spinners. Progress is a stage list: `BTCUSDT  data ✓  metrics ✓  llm …`.
- LLM blocks start with `LLM ›`; CALC blocks are unprefixed. JSON output includes a stable envelope: `{"ok":true,"data":...,"error":null,"meta":{...}}`.
- Human-readable errors include a reference ID and the next command to run. Example:

```
扫描未完成：BTCUSDT 数据缺口 2026-11-05 08:00–10:00 UTC。
已跳过该标的；其余 9 个标的已生成报告。[scan 20261106-0400]
下一步：oft scan show 20261106-0400 --partial
```

### Confirmations

| Action | Prompt | Rule |
|---|---|---|
| Live enable | `Type LIVE to enable live trading:` | Typed token; `--yes` is ignored; requires the paper-duration gate to be satisfied. |
| Paper reset | `Type RESET to reset the paper account:` | Typed token; irreversible. |
| Kill switch on | none | Stopping is always safe and immediate. Print `KILL SWITCH ON — no new orders will be sent.` |
| Kill switch off / resume live | `Type RESUME to re-enable order sending:` | Typed token; shows current risk state and open positions. |
| Restore prompt default | `[y/N]` | Default No; shows the version being replaced. |
| Remove starred symbol | `[y/N]` | Default No; shows the next scan impact. |
| Large backfill | `[y/N]` + size estimate | Shows estimated GB and symbol count before starting. |

Exit codes: `0` success, `1` runtime error, `2` usage error, `3` partial success (some symbols failed), `4` blocked by risk/guard, `5` auth/config error. Scripts can branch without parsing prose.

Full command tree and per-command states are in `design/flows.md` (F12).

---

## 16. Copy, language, and i18n

### Language

- **zh-CN is the default UI language**, with English technical terms kept verbatim: `CVD`, `Delta`, `Footprint`, `POC`, `VAH/VAL`, `OI`, `Funding`, `Liquidation`, `Trigger`, `Paper`, `Live`, `LLM`, `Prompt`, `Token`, `API Key`, `Kill Switch`.
- Do not translate ticker symbols, provider/model IDs, command names, JSON keys, or metric acronyms.
- English is a supported future locale. Structure copy as keys, never concatenated strings; use `Intl.NumberFormat` and `Intl.DateTimeFormat`; keep number precision from the symbol tick size, not from the locale.
- Chinese and English are separated by a single space in prose. No space inside identifiers.
- Timestamps: store and display UTC in operational surfaces; show local time only when the user opts in. Label the zone explicitly (`UTC` or `本地`).

### Voice

- **Terse, factual, operator-to-operator.** Subject → event → consequence → next action.
- No exclamation marks, no "oops", no "great job", no anthropomorphic model voice.
- The model is reported as `LLM 判断`, never as "AI 认为" or "它觉得".
- Confidence is numeric plus band. Never state certainty the system does not have.
- Numbers always include units (`USDT`, `%`, `bps`, `UTC`) and precision.

### Error-message style

Template: **what failed → why (if known) → impact → safe next action → reference ID.**

| Bad | Good |
|---|---|
| `出错了！` | `入场复核未执行：LLM 输出缺少 risk.stop_price，schema 校验失败。未发送任何订单。可重试或手动拒绝。[decision D-1042]` |
| `Network error` | `无法连接 Binance Futures API（超时 10s）。本次扫描已使用缓存数据，CVD 标记为 STALE。重试：oft scan run --retry 20261106-0400` |
| `No data` | `XAUUSDT 无 4h 历史：该合约上市于 2025-12-11。已跳过该标的；可用 1h 数据不足，不生成报告。[scan 20261106-0400]` |
| `AI says buy` | `LLM 判断：入场（多头），置信度 0.72。理由：吸收后 CVD 转正、POC 上方失衡。CALC 风控：单笔风险 0.8%，未触及上限。` |

### Success-message style

Confirm the action, its scope, and the next action: `扫描完成：10/10 标的，2 个新机会，已推送 Telegram。[scan 20261106-0400]` — not `完成！`.

### Empty-state style

Explain why it is empty and the one useful next step: `暂无触发的机会。下一次扫描 02:14:07（4h 周期）。` Do not use decorative illustrations.

---

## 17. Screen state conventions

Every screen and CLI command declares these states. Detailed per-screen triggers are in `design/flows.md`.

| State | Trigger | Visible feedback | Safe next action |
|---|---|---|---|
| **Default** | Normal data present | Complete layout, last-updated stamp | Primary action |
| **Empty** | Valid query, zero records | One explanatory row/panel; why empty; CTA | Create/run/seed or adjust filter |
| **Loading** | Request in flight | Stable skeleton geometry; named progress for long jobs; cancel where supported | Wait; navigate away safely |
| **Error** | Job/request failed | Specific cause, scope, reference ID, retry | Retry, open log, or fall back to last good data |
| **Partial-data** | Some series/symbols unavailable | Show available data; mark missing parts `N/A`/`STALE`; never blank the screen | Open data-quality detail; exclude symbol |
| **Success** | Action completed | Confirmation with scope, timestamp, next action or undo where supported | Continue to the resulting object |
| **Permission** | Action not allowed (live off, read-only Telegram, mobile edit) | Explain the boundary and the safe path | Request on desktop/CLI, or stay read-only |

**Partial-data is a first-class state**, not an error. The research establishes permanent gaps (no USDⓈ-M historical liquidations, TradFi history from listing, irregular TradFi metrics, no TradFi bookTicker). The UI must make these gaps legible on every surface that uses the affected data.

**Kill-switch exception:** stopping is always available in every state, including error, partial, permission, and mobile read-only. It is the one action that ignores other restrictions.

---

## 18. PRD traceability

| PRD requirement | Where it is satisfied |
|---|---|
| MVP 1 data ingestion / backfill | F03 (scan data stages), F10d/F12 (`oft backfill`), chart data-quality badges |
| MVP 2 deterministic order-flow metrics | F04 report CALC band, chart anatomy, provenance system |
| MVP 3 watchlist + scheduling | F02, F03, F10d |
| MVP 4 knowledge injection | F04 narrative provenance, F10b (prompt/template context label) |
| MVP 5 periodic analysis | F03, F04 |
| MVP 6 opportunity ledger + code trigger | F05, F06 |
| MVP 7 LLM entry re-check | F06 |
| MVP 8 position management | F07 |
| MVP 9 paper trading | F01, F08, F09 |
| MVP 10 dual entry CLI + Web UI | F12 and every flow's entry points |
| MVP 11 LLM provider abstraction | F10a, F12 (`oft llm`) |
| MVP 12 prompt templates + versioning | F10b |
| MVP 13 trade log and review | F09 |
| MVP 14 Telegram read-only | F11, F10f |
| MVP 15 LLM budget thresholds | F10e, chart/status budget strip |
| Decisions: module boundary (code vs LLM) | Provenance system (§14), F04/F06 |
| Decisions: risk caps take the stricter side | F06 risk panel, F07 position caps, F10c |
| Risks: Live default off, kill switch, audit trail | F10g, global status bar, F06/F07 timelines |
| Risks: prompt injection / untrusted data | UI labels LLM output as untrusted narrative; no external text in MVP |
| Delivery milestone 4 dual entry | F12 + all Web routes |
| Delivery milestone 5 Telegram | F11 |
| Delivery milestone 6 live capability | F10g (toggle + kill switch, default off) |
| Delivery milestone 7 chart polish | Deferred list in §13.3 |

---

## 19. Non-blocking open questions

These do not block Stage 3; they should be resolved during implementation or Stage 4 preview.

1. **Dense-table grouping:** whether positions should show unrealized PnL in USDT, %, or both in the default column. Decision for MVP: both (`USDT` primary, `%` secondary in the same cell, 13px/11px). Revisit after 2 weeks of use.
2. **Chart focus mode:** whether the footprint dock should be pinned or follow hover. MVP: pinned to the selected bar, with a "follow last" toggle.
3. **Telegram receipt surface:** whether delivery receipts live only in `/system` or also on each pushed event. MVP: `/system` plus a per-message `delivered/retrying/failed` chip in the `Telegram` settings tab.
4. **Narrative streaming:** whether the report narrative streams token-by-token or appears complete. MVP: skeleton with staged status, then complete text; streaming is deferred because partial LLM prose is not actionable and complicates provenance labels.

---

## Accessibility

**Conformance target:** WCAG 2.2 Level AA for the Web UI and the CLI; AA where the Telegram client permits it. Several AAA behaviors are adopted because the terminal aesthetic already exceeds them (focus appearance, and 7:1 secondary text on the three primary surfaces).

**Scope note.** This is a single-user local install, so multi-tenant permissions, session timeouts and account authentication do not arise. The requirements that actually bite here are: market direction encoded in color, a continuously updating data table, a canvas chart, sub-24px dense controls, destructive financial confirmations, and a bilingual zh-CN/en surface.

---

### A1. Contrast audit (measured)

**Method.** Ratios are computed with the WCAG 2.2 relative-luminance formula on sRGB values from `design/design-tokens.json`: channel `c <= 0.03928` maps to `c/12.92`, otherwise `((c+0.055)/1.055)^2.4`; `L = 0.2126R + 0.7152G + 0.0722B`; ratio `= (L_light + 0.05) / (L_dark + 0.05)`. Results agree with §7.2 within ±0.05 (rounding of the linearization threshold). Alpha-composited surfaces are computed as `a*fg + (1-a)*bg` in sRGB, matching browser compositing.

**Thresholds used:** 4.5:1 normal text (SC 1.4.3), 3:1 large text (>= 24px, or >= 18.66px bold), 3:1 UI components and meaningful graphics (SC 1.4.11), 3:1 focus indicator against adjacent colors (SC 2.4.11). `micro` (11px) and `caption` (12px) text is normal text and is judged at 4.5:1.

#### A1.1 Text on surfaces

| Foreground (token) | canvas `#0A0B0D` | panel `#0E1014` | raised `#12151B` | inset `#07080A` | hover `#171B22` | selected `#16242B` | AA |
|---|---:|---:|---:|---:|---:|---:|---|
| `text.primary` `#E8EDF2` | 16.73 | 16.18 | 15.53 | 17.02 | 14.67 | 13.50 | PASS |
| `text.secondary` `#9AA6B2` | 7.94 | 7.68 | 7.37 | 8.08 | 6.96 | 6.41 | PASS |
| `text.muted` `#7C8898` | 5.47 | 5.29 | 5.08 | 5.56 | 4.80 | **4.41** | **FAIL on selected** |
| `text.disabled` `#56616F` | 3.12 | 3.02 | 2.90 | 3.18 | 2.74 | 2.52 | FAIL — inactive only (see A1.7) |
| `accent.primary` `#4DD8E6` | 11.51 | 11.14 | 10.69 | 11.72 | 10.10 | 9.29 | PASS |
| `text.onLive` `#F7FAFC` on `liveWash` `#2A0E12` | 17.13 | — | — | — | — | — | PASS |

#### A1.2 Semantic colors on surfaces

| Foreground | canvas | panel | raised | selected | On its own wash | AA |
|---|---:|---:|---:|---:|---:|---|
| `market.up` `#35D07F` | 9.83 | 9.50 | 9.12 | 7.93 | 7.64 on `upWash` `#0F2A1F` | PASS |
| `market.down` `#FF5D6A` | 6.59 | 6.37 | 6.12 | 5.32 | 5.76 on `downWash` `#2A151A` | PASS |
| `market.flat` `#8B98A8` | 6.71 | 6.49 | 6.23 | 5.41 | — | PASS |
| `status.warn` `#FFB020` | 10.76 | 10.41 | 9.99 | 8.69 | 8.68 on `warnWash` `#2A2110` | PASS |
| `status.ok` `#4FD1A5` | 10.31 | 9.97 | 9.57 | 8.32 | 8.53 on `okWash` `#0C2420` | PASS |
| `status.info` `#62B6FF` | 9.05 | 8.75 | 8.40 | 7.30 | 7.68 on `infoWash` `#101F2E` | PASS |
| `status.danger` `#FF6B6B` | 7.10 | 6.86 | 6.59 | 5.73 | 6.25 on `dangerWash` `#2A1416` | PASS |
| `environment.paper` `#4FD1A5` on `paperSurface` `#0C1D19` | 9.12 | — | — | — | — | PASS |
| `environment.live` `#FF5D6A` on `liveSurface` `#2A0E12` | 6.01 | — | — | — | — | PASS |
| `environment.degraded` `#FFB020` | 10.76 | — | — | — | — | PASS |
| `environment.offline` `#7C8898` | 5.47 | — | — | — | — | PASS |

#### A1.3 Chips and tinted surfaces

| Chip | Text on background | Ratio | AA |
|---|---|---:|---|
| neutral | `#C7D2DF` on `#1D222B` | 10.42 | PASS |
| info | `#62B6FF` on `#1A2735` | 6.97 | PASS |
| ok (status) | `#4FD1A5` on `#132B23` | 7.87 | PASS |
| ok (market up chip) | `#35D07F` on `#132B23` | 7.50 | PASS |
| warn | `#FFB020` on `#302616` | 8.12 | PASS |
| danger (market down chip) | `#FF5D6A` on `#301B20` | 5.39 | PASS (lowest chip pair) |
| accent | `#4DD8E6` on `#172C31` | 8.52 | PASS |

`text.muted` on any tinted surface fails AA: `#132B23` 4.17, `#1A2735` 4.21, `#301B20` 4.48, `#302616` 4.12, `#1D222B` 4.43, `accent.mutedSurface` `#172C31` 4.05. **Rule: `text.secondary` is the minimum foreground for any chip, badge, wash or selected row.**

#### A1.4 Chart and data visualization

Plot background is `#0A0B0D` (identical to canvas). Graphical-object threshold is 3:1; label text threshold is 4.5:1.

| Element | Token | Ratio vs plot | AA |
|---|---|---:|---|
| Axis labels | `chart.axisText` `#7C8898` | 5.47 | PASS |
| Series 1–6 | `#4DD8E6` / `#FFB020` / `#62B6FF` / `#FF8A4C` / `#A3E635` / `#E879C9` | 11.51 / 10.76 / 9.05 / 8.43 / 13.06 / 7.51 | PASS |
| Candle up / down / doji | `#35D07F` / `#FF5D6A` / `#8B98A8` | 9.83 / 6.59 / 6.71 | PASS |
| Volume MA line | `#9AA6B2` | 7.94 | PASS |
| CVD line | `#4DD8E6` | 11.51 | PASS |
| OI / funding / liquidations / taker ratio | `#FFB020` / `#62B6FF` / `#FF8A4C` / `#E879C9` | 10.76 / 9.05 / 8.43 / 7.51 | PASS |
| POC | `#E8EDF2` | 16.73 | PASS |
| VAH / VAL | `#7C8898` | 5.47 | PASS |
| Crosshair | `#C7D2DF` | 12.85 | PASS |
| Tooltip text on `tooltipBg` `#12151B` | `#E8EDF2` | 15.53 | PASS |
| Opportunity stroke pending / triggered / expired / invalidated | `#FFB020` / `#4DD8E6` / `#7C8898` / `#FF5D6A` | 10.76 / 11.51 / 5.47 / 6.59 | PASS |
| Markers entry / exit / stop / target / trigger line | `#4DD8E6` / `#C7D2DF` / `#FF5D6A` / `#35D07F` / `#FFB020` | 11.51 / 12.85 / 6.59 / 9.83 / 10.76 | PASS |
| Data quality complete / partial / missing / estimated | `#4FD1A5` / `#FFB020` / `#FF5D6A` / `#62B6FF` | 10.31 / 10.76 / 6.59 / 9.05 | PASS |
| Volume histogram, 42% alpha (up) | `#35D07F` @ 0.42 | **2.55** | **FAIL (3:1)** |
| Volume histogram, 42% alpha (down) | `#FF5D6A` @ 0.42 | **2.00** | **FAIL (3:1)** |
| Volume profile bars, 28% alpha | `#8B98A8` @ 0.28 | **1.53** | **FAIL (3:1)** |
| Grid / axis line / watermark | `#1A1F27` / `#262C36` / `#1D222B` | 1.19 / 1.40 / 1.23 | Exempt — decorative, not the sole carrier of data |
| Zero reference lines | `#3A424F` | 1.86 | Advisory — pair with a visible `0` axis label |

The two histogram/profile failures are real: color is the only channel distinguishing buy-volume from sell-volume in the histogram, and the profile's shape carries no numeric labels. See A1.7 for the fix; the data table alternative (A5) is mandatory until the token alphas change.

#### A1.5 Footprint heat and cell text

Heat fills are `chart.footprint.bidFill` / `askFill` composited over the plot at the `heatSteps` alphas.

| Heat level (alpha) | `cellText` `#E8EDF2` on green | on red | `cellTextZero` `#7C8898` on green | on red |
|---|---:|---:|---:|---:|
| 0.10 | 14.53 | 15.17 | 4.75 | 4.96 |
| 0.20 | 10.44 | 13.09 | **3.41** | 4.28 |
| 0.30 | 9.13 | 10.74 | 2.99 | 3.39 |
| 0.42 (cap) | 6.57 | 8.39 | 2.55 | 2.74 |

Cell values PASS at every level (worst case 6.57:1). `cellTextZero` only ever renders on a 0% fill (a zero value produces no heat), where muted on `#07080A` is 5.56:1 — PASS. Rule: never render a non-zero value in `cellTextZero`.

#### A1.6 Non-text, focus and component boundaries

| Pair | Ratio | Requirement | Verdict |
|---|---:|---|---|
| `border.focus` `#4DD8E6` vs canvas / panel / raised / inset / selected | 11.51 / 11.14 / 10.69 / 11.72 / 9.29 | 3:1 (SC 2.4.11) | PASS |
| Selected-row 2px cyan rule vs `selected` | 9.29 | 3:1 | PASS |
| `border.danger` `#FF5D6A` vs panel | 6.37 | 3:1 | PASS |
| `border.default` `#262C36` vs panel (as a **control** boundary) | **1.36** | 3:1 (SC 1.4.11) | **FAIL** |
| `border.strong` `#3A424F` vs panel (as a control boundary) | **1.80** | 3:1 | **FAIL** |
| `border.subtle` `#1D222B` vs panel (table rules only) | 1.19 | Decorative | Exempt |
| `accent.onPrimary` `#07080A` on `#4DD8E6` / `#7BE4EE` / `#3CB9C6` | 11.72 / 13.53 / 8.54 | 4.5:1 | PASS |

#### A1.7 Known failures and required usage rules

These are resolved by *usage rules or suggested token additions*, not by editing the token file in Stage 3. They are carried forward to Stage 9 review and are itemized in the handoff.

1. `text.muted` on `background.selected` = **4.41:1**. Either use `text.secondary` (6.41) for any label on a selected row, or lighten muted to `#828E9E` (5.06 on selected). The default table selection rule (2px cyan left rule + background step) must not dim the row's labels.
2. `text.muted` on tinted surfaces (4.05–4.48) — **banned**. `text.secondary` is the floor (5.87–6.50 on the same surfaces).
3. Control boundaries `border.default` (1.36) and `border.strong` (1.80) fail 3:1. Inputs, selects, checkboxes, radios, switches, segmented controls and the search field need a boundary at >= 3:1 against the adjacent surface. Suggested additive token `border.control: #6B7785` (4.32 on canvas, 4.17 on panel, 4.01 on raised, 3.78 on hover, 3.48 on selected). Panel edges, table rules and dividers keep `border.subtle` / `border.default` and are exempt as decoration.
4. Volume histogram at 42% (2.55 up / 2.00 down) fails 3:1. Fix: alpha >= 0.65 for both, or keep 42% fill and add a 1px full-strength top stroke. At alpha 0.65 the ratios become 4.61 (up) and 3.30 (down).
5. Volume profile bars at 28% (1.53) fail 3:1. Fix: alpha >= 0.65 (3.49) or a 1px `#8B98A8` outline. POC/VAH/VAL already pass and remain the labelled anchors.
6. `text.disabled` `#56616F` fails AA everywhere. Permitted **only** for inactive controls, never for a value, status, error, stamp or axis label. Verify in Stage 9 that no disabled control is the only carrier of required information.
7. Focus indicator on an accent-filled control. A 2px cyan ring with a 2px offset sits on the page background, not on the button fill, so it measures 9.29–11.72:1 — PASS. Rule: never draw the focus ring directly against an `accent.primary` fill; keep the 2px offset gap, and on surfaces where cyan is already the background use the outset ring only (not the inset variant).

---

### A2. Focus and keyboard

#### A2.1 Focus style

| Property | Value |
|---|---|
| Indicator | `:focus-visible` only; `outline: none` is never applied without a replacement |
| Ring | `2px solid #4DD8E6`, `outline-offset: 2px` (outset) |
| Dense variant | Same color/width with `outline-offset: -2px` (inset) on table rows, footprint cells and any element inside an `overflow: hidden` container |
| Transition | `120ms` `cubic-bezier(0.2, 0, 0, 1)`; collapses to `0.01ms` under reduced motion |
| Contrast | 9.29–11.72:1 against every surface — exceeds SC 2.4.11 and SC 2.4.13's 2px-perimeter-equivalent area |
| Obscured focus | `scroll-margin-top: 48px` on every focusable element and `scroll-padding-top: 48px` on the scroll container, so the 40px sticky status bar never covers the focused item (SC 2.4.11) |
| Mouse | Focus ring is not shown on pointer activation unless the component also received keyboard focus |
| Non-text state | Focus is never the only cue for "selected": selection also carries the 2px cyan left rule and `aria-selected` |

#### A2.2 Global keyboard model

- Landmarks in DOM order: skip-link group -> `region` (系统状态栏) -> `nav` (主导航) -> `main` (工作区) -> `complementary` (检查器, >=1280px). The status bar is first in the DOM because environment and kill switch are the highest-priority facts (SC 1.3.2); it is made sticky with `position: sticky`, never with CSS `order`, so visual and reading order agree.
- First three tab stops on every route: **跳到主内容** -> **跳到导航** -> **Kill switch**. Skip links are visually hidden until focused and become 44px tall when visible.
- No global single-character shortcuts (SC 2.1.4). The optional kill-switch shortcut is `Alt+Shift+K`, declared with `aria-keyshortcuts`, remappable and disable-able; it does not fire while focus is in a text field. The "pause live updates" key is scoped to the table region.
- Every action available by mouse is available by keyboard (SC 2.1.1); no hover-only actions exist (row actions appear on focus as well as hover).
- No keyboard trap (SC 2.1.2): the only intentional traps are modal dialogs, and every dialog closes with `Escape`.

#### A2.3 Per-flow keyboard paths

Tab order is written in DOM order. "Skip" = the skip-link group above.

| Flow | Route | Tab order | Dialog / Escape | Notes |
|---|---|---|---|---|
| F01 Onboarding | `/onboarding/:step` | Skip -> step list (`aria-current="step"`, non-focusable) -> fields -> 测试连接 -> 上一步 / 下一步 -> 完成 | No modal. `Escape` does nothing; leaving with unsaved input prompts a confirm dialog (focus on 取消) | Error summary receives focus on submit failure; Live row is `aria-disabled` with a description, not removed |
| F02 Watchlist | `/watchlist` | Skip -> nav -> search field -> group headers -> table (row link, then star button) -> inspector (`complementary`) | Search overlay is a modal: focus in the combobox, `Escape` closes and returns focus to the search field; inspector becomes a modal sheet <1280px with the same return-focus rule | Star button is `aria-pressed`; sort header buttons inside `th[aria-sort]` |
| F03 Scans | `/scans`, `/scans/:scanId` | Skip -> nav -> schedule card -> 立即运行 -> history table -> per-symbol stage list | No modal. `Escape` stops progress announcements only while the progress region has focus | Progress region is `role="status"` with a 暂停播报 toggle; page never steals focus on update |
| F04 Report | `/reports`, `/reports/:reportId` | Skip -> nav -> report header -> CALC metrics table -> chart group -> chart toolbar (timeframe / overlays / 数据表) -> LLM band -> opportunity cards -> export | Chart `Escape` returns focus to the chart header. Export opens a modal (focus on 取消) | The narrative's inline references are real buttons that move focus to the referenced metric row |
| F05 Opportunities | `/opportunities`, `/opportunities/:oppId` | Skip -> nav -> tablist (open / triggered / expired / invalidated) -> table -> detail sections -> timeline | No modal | Tablist uses Left/Right + Home/End; `aria-selected` follows focus; panel is `role="tabpanel"` |
| F06 Trigger review | `/triggers`, `/triggers/:triggerId` | Skip -> nav -> queue table -> decision card sections in mandatory order (Trigger / LLM / Risk gate / Actions) -> 拒绝 -> 接受 | Accept opens a confirm modal: focus on the typed-token field (live) or on 取消 (paper); `Escape` aborts; focus returns to 接受 | Disabled 接受 uses `aria-disabled` + `aria-describedby` naming the binding cap; it is never `display:none` |
| F07 Positions | `/positions`, `/positions/:positionId` | Skip -> nav -> summary meters -> table scroll region -> row -> detail -> management actions | Management confirmation is modal; `Escape` aborts the decision, not the position | The horizontally scrollable table wrapper is `tabindex="0"` `role="region"` so it can be scrolled and panned by keyboard (SC 2.1.1) |
| F08 Paper | `/paper` | Skip -> nav -> equity meters -> orders table -> fills table -> reset | Reset modal requires typed `RESET`; `Escape` aborts | Meters expose `aria-valuetext` with units |
| F09 Journal | `/journal`, `/journal/:tradeId` | Skip -> nav -> filter form -> summary strip -> table -> detail -> timeline -> export | No modal | Filters apply on explicit submit, never on change (SC 3.2.2) |
| F10 Settings | `/settings/*` | Skip -> nav -> section nav -> form fields -> save -> unsaved-changes bar | F10b restore and F10c loosening open modals; F10g live-enable and kill-off use typed tokens; all close on `Escape` and return focus | Section nav uses `aria-current="page"`; save announces scope and next effective cycle |
| F11 Telegram | chat client | N/A (client-owned) | N/A | Message design specified in A9 |
| F12 CLI | terminal | N/A | N/A | Specified in A10 |

#### A2.4 Dialog rules

- Use native `<dialog>` with `showModal()` wherever possible: it provides inertness, `Escape`, and focus return for free. Otherwise `role="dialog"`, `aria-modal="true"`, `aria-labelledby` and `aria-describedby` are required, plus `inert` on the rest of the document.
- Focus enters at the least destructive control, except for typed-token confirmations where the token input is first. Cancel is the default action.
- `Tab` from the last control wraps to the first; `Shift+Tab` from the first wraps to the last. The **kill switch is appended as the final stop inside every modal's tab cycle** so stopping is never blocked by a dialog, and it is announced as "Kill switch, 弹窗之外仍可用".
- `Escape` closes and returns focus to the invoker; if the invoker no longer exists, focus moves to the nearest heading with `tabindex="-1"`.
- Background content must be inert (not merely covered). Scroll is locked, but the status bar stays visible.
- The e3 live-arming dialog repeats the environment and the final position size in its description; it never relies on the red border for meaning.

#### A2.5 Chart panel focus

The chart is a custom canvas widget; it is the one place `role="application"` is permitted. The rule is deliberately narrow.

- Wrapper: `<figure role="group" aria-labelledby="chart-title" aria-describedby="chart-summary">`. Inside it: a `tabindex="0"` element with `role="application"` and `aria-roledescription="交互式图表"`, then (outside the application role) the toolbar and a **查看数据表** button.
- Canvas: `role="img"` with `aria-label` naming symbol, timeframe, range and data-quality stamp. It is never the only carrier of any value.
- `chart-summary` (visually hidden, `aria-live="polite"`): last price, change, window high/low, latest CVD and delta, gap count, and the data-quality stamp. Updated at most once every 5s.
- Keyboard inside the chart: `Left`/`Right` move the focused bar; `Shift+Left`/`Shift+Right` move by a screen; `Home`/`End` jump to the visible range ends; `+`/`-` zoom; `PageUp`/`PageDown` pan; `Enter` opens the bar's tooltip as a persistent popover. The focused bar's OHLC, volume, delta, CVD, OI change, funding, session and quality flag are announced through a debounced (500ms) polite region so fast traversal does not flood the screen reader.
- `Escape` from the chart returns focus to the chart header strip; `Escape` from the tooltip popover closes only the popover.
- Footprint dock is a real `<table>` with a `<caption>` naming the focused bar; price is `<th scope="row">`; Bid / Ask / Delta are column headers. Heat bars are `aria-hidden="true"` because the numeric cell text is the value.
- CVD pane is not separately focusable; its summary is part of `chart-summary`, and the data-table toggle exposes the series.
- 查看数据表 opens a modal containing the visible bars as a table (time, O, H, L, C, volume, delta, CVD): the accessible alternative for the whole chart (SC 1.1.1) and the fallback when the chart library fails (flows F04 `error` state already requires a bordered "图表不可用" panel — that panel must include the data table).
- Pan/zoom by drag has button and keyboard equivalents (SC 2.5.7); no path-based or multipoint gesture is required (SC 2.5.1).

---

### A3. Target size

Minimum is 24x24 CSS px (SC 2.5.8); primary financial actions are 44px.

| Element | Size | Spacing | Result |
|---|---|---|---|
| Accept / Reject / Enable Live / Disable Live / Kill switch / Paper reset | 44px height, >= 88px width | 12px | PASS; also meets 2.5.5 (AAA) |
| Standard buttons, inputs, selects, save | 32px height | 8px | PASS |
| Inline icon buttons (row action, chevron, close) | 24x24, hit area 32x32 | >= 4px gutter, 28px pitch | PASS |
| Compact table row (whole row is the target) | 28px | — | PASS |
| Row-local control inside a 28px row (star, sort) | 24x24 | 24px centre-to-centre minimum | PASS by the spacing exception when pitch < 24px only if the row also opens on Enter |
| Timeframe and overlay toggles | 28x28 | 8px | PASS |
| Touch, <= 768px | 44x44 | 8px | PASS |
| Footprint rows | 18px cell height | — | Data cells, not targets — exempt. If row selection is added, raise the row to 24px or provide a 24px hit area |
| Inline links inside prose | Text height | — | SC 2.5.8 inline exception applies |

Dense-grid exceptions are allowed only where the control is redundant: the whole row is a target, so a smaller inner control never becomes the sole affordance. Markers on the chart are not interactive in MVP; if they become clickable they need a 24px hit radius.

---

### A4. Accessible names, roles and values

| Control | Role / element | Accessible name | State / value | Announced as (example) |
|---|---|---|---|---|
| Price chart | `figure` + `application` wrapper + `img` canvas | "BTCUSDT 4h 价格图，窗口 2026-11-01 至 11-06，CALC 98.2%，LIQ 自 11-06" | Focused-bar index exposed via polite region | "BTCUSDT 4h 价格图… 使用方向键浏览 K 线" |
| CVD pane | `img` (not focusable) | "CVD 累计成交量差，窗口内 -1,240 到 +3,880，1 个背离标记" | — | Same text via `chart-summary` |
| Footprint panel | `table` with `caption` | "BTCUSDT 4h 11-06 08:00 UTC 足迹" | Per-cell numeric text | "价格 98,420，买 12.4，卖 31.8，Delta -19.4" |
| Opportunity card | `li` + heading + single link | Heading text (ID · side · confidence) | Link label carries purpose | "OPP-20261106-014，LONG，置信度 0.72，已触发，查看详情" |
| Position table | `table` + `caption` | "未平仓持仓，3 个，Paper" | `aria-sort` on sortable headers | "BTCUSDT LONG，数量 0.42，未实现盈亏 +128.40 USDT，+1.8%，止损 97,100" |
| Risk meter | `role="meter"` | "单笔风险" | `aria-valuemin/max/now` + `aria-valuetext="0.8%，上限 1.0%"` | "单笔风险 0.8%，上限 1.0%" |
| Budget gauge | `role="meter"` | "本日 LLM 预算" | `aria-valuetext="2.18 / 10.00 USDT，22%"` | Same |
| Confirm dialog | `dialog` + `aria-modal` | `aria-labelledby` = dialog heading | `aria-describedby` = consequence text | "确认放行 Live 交易。将向 Binance 发送真实订单。输入 LIVE 继续。" |
| Live toggle | `button role="switch"` | "Live 交易" | `aria-checked`; `aria-describedby` = gate checklist | "Live 交易，开关，已关闭。需要 4 周 paper 记录。" |
| Kill switch | `button` (action, not a toggle) | Dynamic: "Kill switch 关闭，立即停止新订单" | `aria-pressed` is **not** used | "Kill switch 关闭，立即停止新订单，按钮" |
| Selected navigation item | `a` + `aria-current="page"` | Route name | — | "Dashboard，当前页面，链接" |
| Tab bar (opportunity states) | `tablist` / `tab` / `tabpanel` | State name + count | `aria-selected`, `aria-controls` | "已触发，3，标签页，已选中" |
| Symbol search | `combobox` + `listbox` | "搜索标的" | `aria-expanded`, `aria-activedescendant` | Result count announced via polite region |
| Scan progress | `status` (polite) + `ol aria-current="step"` | "扫描进度" | Per-symbol stage text | "BTCUSDT 指标完成，等待 LLM" |
| Star toggle (watchlist) | `button aria-pressed` | "BTCUSDT 加入扫描" | `aria-pressed` | "BTCUSDT 加入扫描，切换按钮，已按下" |
| Data-quality badge | text span, not icon | "PARTIAL" | — | Must carry the reason in `aria-describedby`, never color alone |

Every control has a name that contains its visible label (SC 2.5.3). Icon-only controls take `aria-label` from the same string used in the tooltip.

---

### A5. Color-vision deficiency

**Principle: hue is always the third channel, never the only one.**

1. **Glyphs.** Every up/down value is prefixed with `▲` / `▼` and every signed value keeps its explicit `+` / `-` sign. Delta cells read `▲ +19.4` / `▼ -19.4`, not a bare colored number.
2. **Words.** Tables carry a `SIDE` column with `LONG` / `SHORT`; environment is `PAPER` / `LIVE` in text; opportunity state is `open` / `triggered` / `expired` / `invalidated` in text. No state is conveyed by color alone (SC 1.4.1).
3. **Shape.** Candles switch to hollow body = up, filled body = down. Marker shapes are already distinct (triangle / square / cross / circle). Footprint uses column position (bid left, ask right) plus the delta sign, so direction survives with no hue at all.
4. **CVD-safe mapping** (opt-in via a settings toggle, persisted in `prefers-color-scheme`-independent config; applied as a CSS custom-property override, no token file change):

| Semantic | Default | CVD-safe | Ratio on canvas |
|---|---|---|---:|
| up / long / positive | `#35D07F` | `#62B6FF` (blue) | 9.05 |
| down / short / negative | `#FF5D6A` | `#FF8A4C` (orange) | 8.43 |
| flat | `#8B98A8` | `#8B98A8` | 6.71 |

Blue/orange is chosen for deuteranopia and protanopia (the common cases). Because the two have near-identical luminance (1.07:1 against each other), **monochromacy is covered by the glyph and hollow/filled channel, not by color.** Amber (`#FFB020`) and cyan (`#4DD8E6`) are never repurposed in this mode; status meanings stay unchanged.
5. **High-contrast-safe alternative mapping** for `prefers-contrast: more`: `text.muted` -> `text.secondary`; `text.secondary` -> `text.primary`; all control borders -> `border.control`; table rules -> `border.strong`; footprint `cellTextZero` -> `cellText`; volume/profile alphas raised to their passing values. This also satisfies the fixed failures in A1.7 without changing the default theme.
6. **Forced colors** (Windows High Contrast): do not set `forced-color-adjust: none` on text, icons or controls. The chart canvas may draw with system colors (`CanvasText`, `Canvas`, `Highlight`) and must keep the data-table alternative available.

---

### A6. Motion, zoom, reflow and text spacing

**Reduced motion.** `@media (prefers-reduced-motion: reduce)` sets every duration token to `0.01ms`, disables the 600ms price flash, stops the skeleton pulse (static block plus a `role="status"` "加载中" label), and disables all chart and pane transitions. The value change that the flash signalled is still perceivable because the value text updates and, if the operator enabled announcements, the polite live region reports it. No scroll-triggered, parallax or looping animation exists (SC 2.3.1 is not at risk: no flashing content).

**Auto-updating content (SC 2.2.2).** Live tables and the chart update automatically. A global **暂停实时更新** control sits in the status bar, and each table has its own pause. While paused, values freeze and a visible `PAUSED` stamp appears; the chart stops its scroll. Trigger decisions have no UI time limit (SC 2.2.1); opportunity expiry is a data rule displayed as a timestamp, not a countdown that changes context.

**Zoom and reflow (SC 1.4.4, 1.4.10).**
- Type scale is authored in `rem` (11px = 0.6875rem at a 16px root) so browser text-only zoom scales every size. Never set `html { font-size: 62.5% }` in a way that breaks user font overrides.
- At 400% zoom (320 CSS px effective) the layout must match the 375px rules: single column, bottom tab bar, key-value sheets instead of tables, chart reduced to price + CVD. No horizontal page scroll; horizontal scroll is permitted only inside the table and chart regions (the 1.4.10 exception for two-dimensional content), and those regions are keyboard-scrollable.
- The sticky 40px status bar must not cover focused content at high zoom: it becomes static below 768px and `scroll-padding-top` guards it above.
- Fixed heights are banned on text containers. Table rows use `min-height`; chart panes keep their percentage heights but may fall to `minPaneHeight` and let the page scroll.
- Prose max measure 68–72 characters is preserved at 320px by allowing wrap, not by horizontal scroll.

**Text spacing (SC 1.4.12).** With line-height 1.5, paragraph spacing 2em, letter-spacing 0.12em and word-spacing 0.16em, nothing may clip or overlap. Consequences: no `overflow: hidden` on cells containing text; `text-overflow: ellipsis` is forbidden on values (it is content loss); chip and stamp containers size to content (the 0.04–0.08em stamp tracking plus the override must not overflow a fixed-width badge); numeric columns may widen and the table scrolls horizontally instead of truncating.

---

### A7. Forms and error handling

| Form | Requirements |
|---|---|
| Provider keys (F01.2, F10a) | `<label>` per field; `type="password"`; paste is allowed and never blocked (SC 3.3.8); no CAPTCHA; masked last-4 shown after save; 401 error names the provider/model and states that nothing was saved; `aria-invalid="true"` + `aria-describedby` on the field |
| Binance keys (F01.3) | Same, plus permission read-back as text; "不使用提现权限" is a labelled note, not a red color |
| Risk caps (F10c) | Numeric input with `inputmode="decimal"`; the allowed range is in the label and in `aria-describedby` ("0.1–5.0%"); errors name the violated constraint and the permitted range (SC 3.3.3); loosening opens a diff modal with the old and new values |
| Prompt templates (F10b) | `<textarea spellcheck="false">`; required variables listed as a checklist; a missing variable is named verbatim ("缺少必需变量 {{metrics}}") with line/column for syntax errors; unsaved-changes bar is announced once |
| All forms | An error summary at the top, `role="alert"`, with links to each invalid field; focus moves to the summary on failed submit; errors use an icon plus the prefix "错误：" (never red alone); success messages state scope, timestamp and next effect |

**Redundant entry (SC 3.3.7).** Resuming onboarding prefills every non-secret value already saved; secret fields stay empty but are also not re-required (the stored key remains active). The risk, budget and schedule forms never ask for a value the user already provided in the same flow.

**Error prevention (SC 3.3.4).** Live enable, Live disable, kill-switch off, paper reset, risk-cap loosening and trigger accept are all financial or data-destructive and all require an explicit confirmation with the consequence stated. The typed-token pattern already satisfies the review-and-confirm requirement.

---

### A8. Dynamic content and status messages

- Toasts that confirm an action (`已加入下次扫描`, `已保存 × 3 项`) are `role="status"` (polite). Errors and the kill-switch state are `role="alert"` (assertive).
- The environment change (Paper <-> Live) and the kill switch are announced assertively and persist as a visible banner; the banner text is the announcement (no separate hidden copy).
- The status bar is a plain `region` with a label; only the environment and kill-switch sub-parts are live.
- Price and metric updates are **never** in a live region by default. An opt-in "播报行情变动" toggle enables a throttled polite region for the focused row only. This is what keeps a tick table usable with a screen reader.
- Scan progress is cumulative and polite; it does not interrupt. Budget thresholds alert once per crossing, not once per refresh.
- Loading states announce "加载中" once and the completion, not a percentage that changes every frame (no fake percentages per §17).

---

### A9. CLI accessibility

The CLI is a first-class surface and is where a screen reader or a motor-impaired operator is most likely to live.

| Requirement | Specification |
|---|---|
| Color | Honor `NO_COLOR`, `--no-color`, `--color=always`, and `TERM=dumb`. Every colored token also has a text label: `SIDE LONG`, `DIR UP`, `STATE PARTIAL`. Color never carries sole meaning. |
| Screen-reader output | When stdout is not a TTY, or `--no-color` is set, never use `\r`, ANSI cursor movement, or in-place line rewriting. Progress stages print one line each. No spinner glyphs. |
| Stable structure | Fixed column order per command, documented in `--help`; deterministic sort. `--format table|json|ndjson|csv` (existing) plus an additive `--format lines` that emits one `key: value` per line and one record per block — the most reliable shape for a screen reader. |
| ASCII mode | `--ascii` replaces box drawing with `+ - \|` and replaces `✓` / `…` with `OK` / `...`. Default to ASCII when `--no-color` is combined with a non-UTF-8 locale. |
| Width | Respect `$COLUMNS`; `--width N`. If the table cannot fit, emit `--format lines` rather than wrapping cells (wrapping destroys column/row reading). Never exceed 120 columns in table mode. |
| Non-interactive confirmations | `--non-interactive` plus `--confirm-token=<TOKEN>` (exact token: `LIVE`, `RESET`, `RESUME`). Non-interactive without a token exits `2` and never proceeds. `--yes` remains valid only for `[y/N]` prompts, exactly as today. This is the accessible path for automation and for operators who cannot drive a TTY prompt. |
| Prompts | Show the accepted answers and the default (`[y/N]，默认 N`); empty input takes the default; `y`/`Y`/`yes` accepted. Read from stdin only when it is a TTY or `--non-interactive` was supplied with a token. |
| Destructive commands | `oft kill` never prompts, exits 0, and prints one stable line (`KILL SWITCH ON — no new orders will be sent.`) plus open positions. |
| Numbers and time | ISO-8601 UTC with `Z`; every number carries units (`USDT`, `%`, `bps`). No locale-dependent ordering. |
| Help | `--help` per command, short, with one runnable example; `oft help --all` lists the tree. Help exits 0. |
| Errors | Message on stderr with a reference ID and the next command; the exit code is also stated in words when `--verbose` is set. |

---

### A10. Telegram surface

The Telegram client owns rendering, so the design constrains only the message shape.

- No ASCII tables and no box drawing; use one `key: value` per line, one blank line between blocks.
- No emoji. Status is a word (`TRIGGERED`, `PARTIAL`, `STALE`, `LLM`, `CALC`).
- First line states event and symbol — that is what a screen reader announces first: `机会触发 · BTCUSDT · LONG · 置信度 0.72`.
- Numbers carry units and ISO-8601 UTC timestamps; no ambiguous dates.
- `/help` returns a plain command list and the "MVP 只读" notice.
- Avoid combining diacritics, zero-width characters and unusual symbols; keep to CJK, Latin, digits and basic punctuation.

---

### A11. Language and structure

- `<html lang="zh-CN">`. English technical terms that a zh-CN voice would mispronounce are wrapped in `<span lang="en">` — at minimum `CVD`, `Delta`, `Footprint`, `POC`, `VAH`, `VAL`, `OI`, `Funding`, `Liquidation`, `Trigger`, `Paper`, `Live`, `LLM`, `Prompt`, `Token`, `API Key`, `Kill Switch`. A small `LangTerm` component keeps this consistent (SC 3.1.2).
- One `<h1>` per route; heading levels descend without skipping (SC 1.3.1, 2.4.6). Section headings in the decision card are headings, not styled divs.
- Link text is self-contained: never "点击这里". Table links name the object ("查看 OPP-20261106-014").
- Consistent navigation and identification across routes (SC 3.2.3, 3.2.4): the same action has the same name and position everywhere. The help affordance (error reference ID + next command) keeps a consistent relative position (SC 3.2.6).
- `prefers-reduced-transparency` and `prefers-color-scheme` do not change the dark theme; the overlay `#07080AB8` is only used over `raised`, never over text.

---

### A12. Stage 9 acceptance criteria

Each item is checkable without judgement calls.

**Contrast**
1. Every foreground on every surface used in the built UI is in the A1 tables, and no pair falls below 4.5:1 (text) or 3:1 (graphics/UI).
2. No `text.muted` appears on `selected`, on any chip, badge or wash, or on `accent.mutedSurface`.
3. Every input, select, checkbox, radio, switch and segmented control has a boundary of at least 3:1 against its adjacent surface.
4. Volume histogram and volume profile bars meet 3:1 against the plot (raised alpha or added stroke).
5. Focus indicators measure at least 3:1 against their adjacent colors in both themes and both focus variants.
6. No text uses `text.disabled` for information that is required.

**Keyboard**
7. From a cold page load, tab stops 1–3 are 跳到主内容, 跳到导航 and the kill switch; activating 跳到主内容 moves focus to the main region, and activating 跳到导航 moves focus to the navigation landmark.
8. Every mouse action in every F01–F10 flow has a keyboard equivalent; a scripted keyboard-only walkthrough of each flow completes without a mouse.
9. No focus is ever obscured by the status bar, the inspector sheet or a sticky header (`scroll-padding-top` verified at 100%, 200% and 400% zoom).
10. Every modal traps focus, includes the kill switch as its final stop, closes on `Escape`, and returns focus to its invoker.
11. The chart is operable with arrows, Home/End, +/-, PageUp/PageDown and Enter; the focused bar's data is announced; 查看数据表 returns an equivalent table.
12. No single-character global shortcut exists; the kill-switch shortcut is remappable and disabled inside text fields.

**Target size**
13. No interactive target is smaller than 24x24 CSS px, except inline text links.
14. Accept / Reject / Enable Live / Kill switch / Paper reset are at least 44px high.
15. On touch viewports (<= 768px) every target is at least 44x44 with 8px separation.

**Names and semantics**
16. Every custom control in A4 has the specified role, name and state, verified with axe-core and one manual screen-reader pass (NVDA + Firefox, VoiceOver + Safari).
17. No control has an empty accessible name; icon-only buttons are labelled.
18. Up/down direction is conveyed by glyph plus word in every table and metric; the CVD-safe mode is available and contains no color-only meaning.
19. Sortable headers expose `aria-sort`; tabs expose `aria-selected`; the Live toggle exposes `aria-checked`.

**Resilience**
20. With `prefers-reduced-motion: reduce`, all durations are 0.01ms, the price flash and skeleton pulse are gone, and loading is still announced.
21. Live updates can be paused globally and per table; a paused table shows `PAUSED`.
22. At 400% zoom the page reflows to a single column with no horizontal page scroll; the table and chart regions scroll internally and are keyboard-scrollable.
23. With text spacing overridden (1.5 / 2em / 0.12em / 0.16em) no text clips, overlaps or is ellipsized.
24. Every form error is identified in text, linked to its field, suggests a fix, and is summarised at the top with focus moved there.
25. Live enable, Live disable, kill-switch off, paper reset, cap loosening and trigger accept all require confirmation that states the consequence.
26. Status messages are announced exactly once per event; price ticks never enter a live region unless the operator opts in.

**CLI**
27. `--no-color`, `NO_COLOR` and `TERM=dumb` remove all color and all cursor movement; progress prints one line per stage.
28. `--ascii` produces only ASCII; `--format lines` emits one `key: value` per line with stable ordering.
29. `--non-interactive --confirm-token=LIVE|RESET|RESUME` performs the typed-token action; without a token it exits 2 and changes nothing.
30. `oft kill` works in every state, prompts for nothing, and exits 0.

**Telegram**
31. No message contains ASCII art, box drawing or emoji; the first line carries event and symbol; numbers carry units; timestamps are ISO-8601 UTC.
