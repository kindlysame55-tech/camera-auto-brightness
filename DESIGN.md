---
name: Exposure Workbench
description: Warm editorial paper, restrained orange actions, and real Windows software evidence.
colors:
  accent: "#b34320"
  accent-hover: "#91351a"
  paper: "#f0ece5"
  ink: "#211e1a"
  quiet: "#66635d"
  rule: "#bdbbb2"
  action-text: "#ffffff"
typography:
  brand:
    fontFamily: "'Playfair Display', serif"
    fontSize: "27px"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "-0.025em"
  display:
    fontFamily: "'Playfair Display SC', 'Noto Serif SC', serif"
    fontSize: "88px"
    fontWeight: 700
    lineHeight: 1.18
    letterSpacing: "-0.03em"
  headline:
    fontFamily: "'Playfair Display SC', 'Noto Serif SC', serif"
    fontSize: "clamp(32px, 3vw, 46px)"
    fontWeight: 700
    lineHeight: 1.4
    letterSpacing: "-0.02em"
  title:
    fontFamily: "'Playfair Display SC', 'Noto Serif SC', serif"
    fontSize: "24px"
    fontWeight: 700
    lineHeight: 1.5
  body:
    fontFamily: "'Segoe UI', 'Microsoft YaHei', sans-serif"
    fontSize: "18px"
    fontWeight: 400
    lineHeight: 1.7
  lead-copy:
    fontFamily: "'Noto Serif SC', serif"
    fontSize: "20px"
    fontWeight: 400
    lineHeight: 1.65
  label:
    fontFamily: "'Segoe UI', 'Microsoft YaHei', sans-serif"
    fontSize: "16px"
    fontWeight: 400
rounded:
  action: "5px"
  evidence: "3px"
  slider-thumb: "50%"
spacing:
  small: "8px"
  compact: "18px"
  standard: "24px"
  section-mobile: "38px"
  section-stacked: "58px"
  section-desktop: "85px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.action-text}"
    rounded: "{rounded.action}"
    padding: "18px 30px"
    width: "min(416px, 100%)"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
  link-source:
    textColor: "{colors.accent}"
  language-button:
    textColor: "{colors.ink}"
    padding: "8px 2px"
  language-button-selected:
    textColor: "{colors.accent}"
  navigation:
    textColor: "{colors.ink}"
    typography: "{typography.label}"
  evidence-image:
    rounded: "{rounded.evidence}"
    width: "100%"
  range-thumb:
    backgroundColor: "{colors.accent}"
    rounded: "{rounded.slider-thumb}"
    size: "24px"
---

# Design System: Exposure Workbench

## Overview

**Creative North Star: "Exposure Workbench"**

The approved world borrows the calm of a photographic contact sheet: warm paper, charcoal serif headings, fine rules, and orange marks that identify actions. It presents practical Windows software with an editorial voice. Real interface evidence carries the visual proof.

The page is spacious and flat. Large bilingual headlines give way to readable explanations, a clear download action, and orderly instructions. Controls feel restrained and direct; the light demonstration adds a small moment of discovery while clearly labeling its scope.

**Key Characteristics:**

- Warm paper and charcoal type with one orange action color.
- Serif display and lead copy paired with system sans serif utility text.
- Fine ruled sections and numbered instructions instead of floating cards.
- Real application imagery with an explicit illustrative-state caption.
- Equivalent Chinese and English content with a stacked narrow-screen layout.

## Colors

The palette uses warm, low-chroma neutrals and a burnt-orange accent. Frontmatter records the final cascaded values in `docs/style.css`, overriding its earlier declarations.

### Primary

- **Burnt Orange:** Download fills, source links, selected language, disclosure markers, focus indicators, and demonstration thumb.
- **Deep Burnt Orange:** Primary-action hover state.

### Neutral

- **Warm Paper:** Continuous page surface and slider-thumb border.
- **Charcoal Ink:** Main text, headings, and interface frame.
- **Quiet Stone:** Supporting copy, compatibility information, captions, and footer links.
- **Paper Rule:** Section dividers and instruction separators.
- **Action White:** Text and icons on primary actions, plus selected text.

**The Action Mark Rule.** Use the orange accent to communicate action, selection, or focus; let paper and typography carry the rest of the page.

## Typography

**Display Font:** Playfair Display SC with Noto Serif SC and serif fallbacks. The brand alone uses Playfair Display (700).
**Body Font:** Segoe UI with Microsoft YaHei and sans-serif fallbacks.

**Character:** English small-cap display forms and substantial Chinese serif headings establish an editorial cadence. Subtitle, description, compatibility line, and demo copy use Noto Serif SC; features, FAQ body, navigation, and actions use the sans serif stack.

Fonts are self-hosted in `docs/fonts/`: `brand.ttf` supplies Playfair Display (700), `display-0.ttf` and `display-1.ttf` supply Playfair Display SC (400/700), and `chinese-0.ttf` and `chinese-1.ttf` supply Noto Serif SC (400/700). All use `font-display: swap`; body fonts are operating-system fonts.

### Hierarchy

- **Display:** Frontmatter captures desktop Chinese hero. English uses (58px), falling to (50px) between (1151–1350px); Chinese uses (76px) in that range. Both use explicit two-line spans.
- **Headline:** Fluid section titles follow frontmatter. Installation uses (34px), then (27px) on mobile; general mobile section titles use (30px).
- **Title:** Feature headings follow frontmatter. Installation titles use (23px), then (21px) on mobile.
- **Body:** Feature and FAQ text uses (17px / 1.9), maximum (70ch), then (16px) on mobile. Hero description uses (20px / 1.65), English (18px), and both become (16px / 1.9) on mobile.
- **Label:** Navigation starts at (16px), becoming (13px) on mobile. Captions use (13px); mobile demo labels and note use (12px).

**The Two Languages Rule.** Preserve the type contrast while allowing language-specific sizing and natural copy wrapping; translation must remain legible at narrow widths.

## Layout

The principal centered container uses (84.85%), maximum (1430px). Installation uses (92.5%), maximum (1560px). Desktop hero columns are (44%) text and (45.06%) evidence with an (8.5%) gap. Text starts below the image top; desktop image height is (667px). Ruled sections use generous vertical spacing.

At (1150px) and below, containers become (89%). Hero stacks text/actions, screenshot, then demonstration. Evidence is centered, capped at (620px), and its image returns to intrinsic aspect ratio. Installation, features, and FAQ become single-column sections; closing actions and footer wrap. Installation steps remain three columns until mobile. Stacked Chinese hero uses `clamp(48px, 8.2vw, 88px)` and English uses `clamp(39px, 7.5vw, 76px)`.

At (600px) and below, containers become (88%). Navigation takes a full row. Chinese hero uses `clamp(42px, 11.2vw, 64px)` and English uses `clamp(30px, 8.5vw, 49px)`. Downloads reduce to minimum (64px) height and remain constrained to container width. Installation becomes a vertical numbered list; feature titles sit above copy. Section spacing follows frontmatter's desktop, stacked, and mobile steps.

### Verification evidence

Approved surface: composition C, build path `comp`, seed `52530b7e`; source contract: `.impeccable/site-contract.md`. These identify this implementation, rather than a reusable layout requirement.

Final screenshot evidence: `.impeccable/review/desktop.png`, `mobile.png`, `user-664.png`, `english-desktop.png`, `english-mobile.png`, and `english-320.png`. Independent ship review accepted all six without clipping or material fixes. The raw desktop and English (320px) captures were also visually checked during documentation: brand, subtitle, download action, evidence, and instructions remain visible.

Mechanical review recorded hero fidelity (86%) and desktop match (81%). The responsive gate still flags brand/subtitle as missing; the independent reviewer adjudicated these as crop/alignment false classifications from raw shots and paired crops. The gate remains flagged. These comparison diagnostics do not claim complete visual identity. The direction catalog was unavailable twice, so selection used the degraded direction roll; no separate QUALITY BAR card exists.

## Elevation & Depth

Sections, instructions, navigation, and actions are flat. Rules and whitespace convey structure. The only explicit shadow is the WebKit slider thumb (`0 2px 4px #23272425`); Firefox uses a flat thumb. The actual application screenshot retains its internal interface appearance.

**The Flat Paper Rule.** Separate content with spacing and thin rules; reserve the existing small thumb shadow for its draggable affordance.

## Shapes

Actions use gently softened corners and the interface image a tighter radius; frontmatter records both. Section separators and image frames use single-pixel strokes. Circular slider thumbs and inline SVG sun icons identify the demo. Instructions, feature rows, and disclosures stay open on the paper surface.

## Components

### Buttons

Primary downloads are wide orange anchors with white text, inline SVG or arrow marks, and desktop minimum height (82px). Frontmatter records desktop padding and width; mobile padding is (15px 20px). Hover darkens the fill over (.18s ease-out). Source links stay underlined and orange. Language buttons have transparent backgrounds, minimum (44px) height, and orange selected text.

### Navigation

Brand sits left and compact anchor navigation/language group right on desktop, above a fine rule. Navigation wraps below the brand at smaller widths. Language selection uses `aria-pressed` and updates document language, copy, screenshot alt text, and demo label. Local storage remembers language when available.

### Evidence and demonstration

The real app screenshot is `docs/assets/plates/app.png`, captioned as an illustrative diagnostic state. Preserve caption and descriptive alt text. Range track runs from dark (`#514844`) to peach (`#ffb47b`), with orange thumb and sun icons. Input changes only the image brightness filter (`0.55 + value × 0.009`) with an (.8s) `cubic-bezier(.16,1,.3,1)` transition. The note explicitly says the preview does not control the visitor's display.

### Instruction and feature rows

Installation uses leading-zero numerals, vertical desktop rules, and mobile horizontal dividers with hanging numerals. Feature rows pair serif titles with quiet sans serif explanations, stacking title above copy on mobile. These are flat rows rather than separate cards.

### Disclosures and keyboard behavior

FAQ uses native `details`/`summary`, orange disclosure markers, bottom rules, and orange open-state text. Native links, buttons, range, and disclosures support keyboards. Interactive elements share an orange focus outline (3px), offset (6px); the skip link appears on focus. Range interactive height is at least (44px). Under `prefers-reduced-motion: reduce`, scrolling becomes immediate and image-filter/download transitions stop. Direct download anchors remain usable without JavaScript.

## Do's and Don'ts

### Do:

- **Do** use orange consistently for actions, selection, and focus.
- **Do** preserve the serif/sans role distinction and self-hosted display faces.
- **Do** keep the screenshot caption and preview-only explanation visible in both languages.
- **Do** use thin rules and spacing to organize supporting content.
- **Do** verify Chinese and English at desktop, stacked, and narrow mobile widths.

### Don't:

- **Don't** replace real interface evidence with invented software controls.
- **Don't** imply that the webpage brightness demonstration controls the screen.
- **Don't** remove visible keyboard focus or force motion under reduced-motion preferences.
- **Don't** treat mechanical comparison percentages or adjudicated gate flags as design tokens.
