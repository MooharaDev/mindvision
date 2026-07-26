# Design — Corpus

Durable visual and interaction rules for the Corpus web console
(`deliverable/templates/index.html` + `deliverable/static/*`). Product truth
lives in PRODUCT.md; air-gap rules live in CLAUDE.md and override anything here.

## Direction contract

- **THESIS** — a workbench that turns a document archive into a dataset. It
  refuses the dashboard-of-identical-cards arrangement: every corpus is a row
  of real facts you can compare, sort and link to.
- **OWN-WORLD** — engineering datasheet. Warm paper plane, hairline rules
  instead of card borders, no nested containers, tabular figures, monospace
  reserved for identifiers and paths, one blue accent for selection and
  primary action only, status as mark + word.
- **FORM** — ledger (browse) + intake sheet (build), one grammar across both.
- **MODE** — Operate. Familiarity and density outrank expression; the tool
  disappears into the task.

## Tokens

All tokens are CSS custom properties at the top of `static/app.css`, declared
once for light and once under `html[data-theme=dark]`. Never hardcode a colour
in markup or JS — reference the token.

| Role | Light | Dark | Notes |
|---|---|---|---|
| rail | `#edece7` | `#121211` | second neutral layer for the nav |
| plane | `#f6f5f1` | `#0d0d0d` | page ground |
| paper | `#fcfcfb` | `#1a1a19` | panels, tables, drawer |
| ink / ink-2 / ink-3 | `#0b0b0b` / `#52514e` / `#6f6e6a` | `#fff` / `#c3c2b7` / `#918f88` | ink-3 is the AA floor for muted text |
| rule / rule-2 | `#e1e0d9` / `#c9c8c0` | `#2c2c2a` / `#3d3d3a` | decorative hairlines only |
| field-line | `#8f8e87` | `#767670` | control borders — held at ≥3:1 |
| accent / accent-ink | `#2a78d6` / `#1c5cab` | `#3987e5` / `#86b6ef` | selection + primary action only |
| status good/warn/serious/crit | `#0ca30c` / `#fab219` / `#ec835a` / `#d03b3b` | same | fixed, never themed, never a series colour |

Type: one system sans, fixed px scale `11 / 12.5 / 14 / 16 / 20 / 25`, plus a
26px readout figure. Mono (`--mono`) only for record ids, paths, table and
field names, and code — never as a costume for "technical".
Space scale `4 / 8 / 12 / 16 / 24 / 32 / 48`. Radius 5px controls, 7px panels.

## Rules

1. **No nested containers.** A `.panel` holds bodies, rows and tables — never
   another panel. Content that needs separating gets `.body + .body` (a
   hairline), not a second card.
2. **Colour never carries meaning alone.** Status is always `.st` = a coloured
   dot plus the word. Chart bars always carry a direct label and their count.
3. **Accent is not decoration.** Blue marks the current selection, the primary
   action and focus. Nothing else is blue.
4. **Figures are readouts, not hero tiles.** `.readout` = value + label
   separated by hairline dividers, tabular figures, no boxes.
5. **Every interactive element ships all its states**: hover, focus-visible
   (2px accent ring, 2px offset), active, disabled, busy. Tables add row hover,
   `aria-selected`, and keyboard activation via `tabindex` + the `data-act`
   delegation in `app.js`.
6. **Empty states teach.** Icon, what this surface is for, and the action that
   fills it — never "nothing here".
7. **Loading is a skeleton**, not a spinner in the middle of content. The one
   exception is an inline `.spin` beside text that names what is happening.
8. **Motion conveys state**, 140–220 ms, and honours `prefers-reduced-motion`.
   No page-load choreography.
9. **Charts keep a readable measure** (`.charts` caps columns at 400px) rather
   than stretching to the panel width.
10. **Contrast**: body and muted text ≥4.5:1, control borders ≥3:1, in both
    themes. `--ink-3` and `--field-line` exist to hold that line; if a value
    changes, re-check it.

## Structure

- Hash router in `app.js`: `#/corpora`, `#/corpus/<id>/<tab>`, `#/new`,
  `#/settings`. Every corpus and tab is linkable and the Back button works —
  this is a requirement, not a nicety.
- Behaviour hangs off `data-act` attributes registered with `onAct(name, fn)`;
  inline `onclick` is only for static, argument-free navigation.
- `esc()` escapes quotes as well as angle brackets, so it is safe in both text
  and attribute positions. All interpolated data goes through it.
- File split: `app.js` (shell, router, helpers, drawer) · `create.js` (intake
  flow + schema editor) · `database.js` (ledger, corpus, tables, charts,
  review) · `settings.js`. Keep it at four; transfer-friendliness beats
  further splitting.

## Air gap

System fonts, one inline SVG sprite, no external requests of any kind. The
selftest greps every shipped frontend file for `http(s)://` and fails the
build on a hit — including placeholder text. Asset URLs carry
`?v=APPVER`, rewritten at serve time from `APP_VERSION` + the newest static
mtime, so a file patched in place inside the air gap can never be served from
a stale browser cache.
