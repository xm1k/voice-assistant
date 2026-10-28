# Mobile frontend implementation plan

**Goal:** Improve mobile usability while preserving desktop appearance and behavior.

**Architecture:** Keep shared Workspace and existing data/actions. Scope CSS changes to max-width: 720px; add viewport-fit for mobile safe areas.

**Tech Stack:** React 19, Vite 7, CSS media queries.

**Spec:** docs/superpowers/specs/2026-10-26-mobile-frontend-design.md

## Constraints and review focus

- No desktop visual changes; compare desktop screenshots before/after.
- No horizontal page overflow at 320–720 px, including long titles.
- Keep every navigation destination, audio upload and calendar action available.
- Scrollable forms on short screens; comfortable touch controls and 16px form text.
- Preserve dark theme and internal calendar/chat scrolling.

## Tasks

- [x] Capture baseline desktop and mobile screenshots in mock mode.
- [x] Extend mobile styles in src/app.css and src/workspace.css; preserve existing palette and typography. Add viewport-fit=cover in index.html.
- [x] Exercise navigation, messaging, task creation, record editor, month/week calendar and settings in mobile browser emulation.
- [x] Run frontend node tests and production build; compare desktop screenshot with baseline and review diff for mobile-only scope.

Paths above are relative to services/gateway/app/frontend unless explicitly prefixed with docs/.

## Verification

- 23 frontend Node tests passed; production Vite build passed.
- All five mobile views fit 320, 375, 390, 430 and 720 px without horizontal page overflow.
- Calendar record saved; task with long unbroken title created; message sent and composer cleared in mock mode.
- Editor at 320×480: top 12px, bottom 468px, inner content scrolls without horizontal overflow.
- Week calendar scrolls horizontally inside its 300px container; page remains 320px wide.
- Dark settings/chat screenshots inspected.
- Desktop 1440×900 before/after PNG SHA-256 matches exactly: 9cc4e9933325ab0af8ccad79bac68eb35e482a4ae3b08b4b00ace3f648b5338f.
- Mobile CSS changes only; no React or data-flow changes. Physical iOS keyboard/safe-area behavior remains unverified.
