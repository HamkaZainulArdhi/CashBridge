# Design System: Google Finance — Dark Mode Market Dashboard

## 1. Visual Theme & Atmosphere
A dense, data-first financial dashboard rebuilt natively for dark mode — not a light theme with inverted colors. The surface is near-black charcoal rather than pure black, so numbers and chart lines have somewhere to "glow" against without eye strain. Depth is communicated through **elevation (layered surface tints)**, not drop shadows — shadows barely read on a dark background, so hierarchy comes from how much lighter a surface is than the one behind it. Density stays high (8/10): tickers, tables, news feed. Variance stays low (3/10): grid-regular, predictable. Motion stays restrained (3/10): functional only. The atmosphere should feel like a trading terminal at night — calm, legible, zero glare.

## 2. Color Palette & Roles — Elevation-Based Surfaces
Dark mode is not "swap white for black." Each elevation level gets its own surface tone so stacked cards are visually separable without borders or shadows:

- **Base Canvas** (#0E0F11) — The page background, lowest elevation (dp0)
- **Surface 1 — Cards** (#17181B) — Default card/table container (dp1)
- **Surface 2 — Raised/Active** (#1F2023) — Hovered rows, active nav tab background, the "Ask AI" bar (dp3)
- **Surface 3 — Overlay** (#28292D) — Dropdowns, tooltips, modals (dp8+)
- **Hairline Divider** (rgba(255,255,255,0.08)) — Row/card separators — barely-there, not a hard line like light mode
- **Ink Primary** (rgba(255,255,255,0.87)) — Primary text, headline prices. Never pure #FFFFFF — full white on dark is harsh and causes halation on OLED
- **Ink Secondary** (rgba(255,255,255,0.60)) — Labels, metadata, timestamps
- **Ink Disabled** (rgba(255,255,255,0.38)) — Disabled controls, placeholder text
- **Signal Blue — Dark Adjusted** (#8AB4F8) — Desaturated/lightened from light-mode's #1A73E8. Saturated blue on dark backgrounds vibrates; this lighter tone keeps the same brand identity while staying legible and calm
- **Gain Green — Dark Adjusted** (#81C995) — Lightened from #188038 for the same reason; a fully saturated dark green nearly disappears against a charcoal background
- **Gain Green Surface Tint** (rgba(129,201,149,0.16)) — Chip background behind positive movers — a translucent tint of the accent over Surface 1, not a flat opaque fill
- **Loss Red — Dark Adjusted** (#F28B82) — Lightened from #D93025
- **Loss Red Surface Tint** (rgba(242,139,130,0.16)) — Chip background behind negative movers
(Rule: every accent and semantic color used on dark surfaces must be a *lightened/desaturated* variant of its light-mode hex — never reuse the light-mode saturated value directly. This is the single most common dark-mode mistake to avoid.)

## 3. Elevation & Depth System (replaces shadows)
- Elevation is expressed as a **white-opacity overlay** mixed into the base surface color, increasing with each level: dp0 → +0%, dp1 → +3%, dp3 → +6%, dp8 → +10%, dp16 → +13%. This is why Surface 1/2/3 above get progressively lighter, not darker.
- No `box-shadow` for card separation in the default state — elevation is read through surface tone contrast + the 8% hairline divider only.
- A thin shadow (`0 1px 2px rgba(0,0,0,0.4)`) is permitted only on true floating/overlay elements (modals, the AI chat bar when it's in a focused/expanded state) — never on resting cards or table rows.
- Pressed/active states on interactive surfaces (buttons, chips) get a brief opacity overlay (white 10–12%) rather than a darken filter, since darkening is imperceptible on already-dark surfaces.

## 4. Typography Rules
- **Display/Headings:** Google Sans, Medium weight — same font family as light mode, but rendered slightly lighter in weight (Medium instead of Bold) at equivalent sizes, since bold white-on-dark text can look heavier/blurrier than the same weight on light backgrounds
- **Body/UI text:** Roboto Regular/Medium, Ink Secondary for anything non-critical
- **Numeric/Tabular data:** Roboto with `font-variant-numeric: tabular-nums`, always Ink Primary (87% opacity) regardless of surrounding text — numbers are the product and must read at full primary contrast
- **Letter-spacing:** add a hair more tracking (+0.1–0.2px) on small uppercase labels versus light mode — dark backgrounds make tightly-set caps feel like they're clumping
- **Minimum contrast:** every text/surface pairing must hit WCAG AA (4.5:1 body, 3:1 large text) against the *specific elevation surface it sits on*, not against the base canvas — a card at Surface 2 needs its own contrast check

## 5. Component Stylings
- **Ticker Chips:** Pill on Surface 1. Value in Ink Primary; change wrapped in Gain/Loss Surface Tint (translucent, not flat fill) with the dark-adjusted arrow icon color. No border — separation from scroll-strip background via the tint alone.
- **Data Tables:** Surface 1 background, 8% opacity hairline between rows (no vertical rules). Header row: Ink Secondary, uppercase, slightly wider tracking. Row hover: background steps up to Surface 2, transition 120ms. Positive/negative values colored with the dark-adjusted green/red — never the light-mode saturated hex.
- **Cards:** Surface 1 on Base Canvas, 1px border at `rgba(255,255,255,0.08)` **only if** two cards of the same elevation sit directly adjacent with no canvas gap between them; otherwise rely on the canvas gap alone, no border needed.
- **Buttons:** Primary = Signal Blue (dark-adjusted) fill with Base Canvas–colored text (not white) for sufficient contrast against the lighter blue; pill or 8px radius. Secondary = outline at the hairline divider color, Ink Primary text. Active press = +10% white overlay, no translate/scale needed.
- **AI Chat Input ("Ask anything"):** Surface 2 pill, sits visually "raised" above Base Canvas via the elevation tint rather than a shadow. Icons at Ink Secondary; send button flips to Signal Blue fill only once text is entered (resting disabled state = Ink Disabled).
- **Icons (Material Symbols):** Outlined style, Ink Secondary by default, Ink Primary on hover/active — never filled/solid icons at rest, since filled icons on dark backgrounds read as heavier than intended.
- **Loading States:** Skeletal shimmer using a Surface 1 → Surface 2 → Surface 1 sweep (not gray-on-white), same exact dimensions as final content.
- **Focus Rings:** 2px Signal Blue (dark-adjusted) ring with a 2px Base Canvas offset gap — critical in dark mode where a ring flush against the element can be nearly invisible.

## 6. Layout Principles
- Same structural grid as light mode (top nav → horizontal ticker strip → stacked content cards → persistent bottom AI bar) — dark mode is a surface/color system change, not a layout change
- Canvas gaps between cards do more visual separation work in dark mode than in light mode, since borders are intentionally faint — don't compress vertical rhythm to save space; keep 16–24px gaps between card-level modules
- Images/favicons/logos in news cards: wrap in a subtle Surface 2 rounded container if the source image has a transparent or white background, so it doesn't appear as a harsh bright rectangle against the dark canvas

## 7. Motion & Interaction
- Same timing as light mode (120–180ms ease-out), but live-price tick flashes use the translucent Gain/Loss Surface Tint at a slightly higher opacity pulse (~24% → 16%) since a subtle flash on dark can otherwise go unnoticed
- No glow/bloom effects on the flash — opacity change only, never `box-shadow` blur, to avoid the "neon" look
- Animate `transform`/`opacity` only, same as light mode

## 8. Anti-Patterns (Dark Mode Specific — Banned)
- No pure black (#000000) background — always Base Canvas #0E0F11 or similar charcoal
- No pure white (#FFFFFF) text — always Ink Primary at 87% opacity
- No reusing light-mode saturated accent/semantic hex values directly on dark surfaces (causes vibration/glow)
- No drop-shadows for resting card/table elevation — use surface-tint elevation instead
- No filled/solid icon style at rest
- No borders as the *only* depth cue when an elevation-tint alternative is available
- No neon glow or blurred box-shadow on "live update" flashes
- No focus rings flush against element edges with no offset gap
- No dark-mode-as-afterthought: every color, icon, and image container must be re-evaluated for this theme, not just color-inverted from light mode