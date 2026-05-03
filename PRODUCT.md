# Product

## Register

product

## Users

Hospital physicians (hospitalists, residents, attendings) on shift, embedded in OpenEMR. Primary context: morning rounds, mid-shift triage decisions, and end-of-shift handoff. They are time-pressed, interrupt-driven, and reading the chat surface in an iframe alongside the OpenEMR chart, often on a hospital workstation in landscape, sometimes on a smaller tablet at bedside. They are clinically expert; they do not need handholding, but they cannot afford ambiguity in a tool that touches medication safety and triage.

## Product Purpose

Clinical Copilot is a chat-shaped agent embedded in OpenEMR that answers questions about a physician's current census: who needs attention first, why a patient is P1, what the medication risks are, and a structured I-PASS handoff at end of shift. It is not a replacement for the chart; it is a faster path to the answers a clinician would otherwise click through 4-5 OpenEMR screens to assemble. Success looks like: the physician trusts a Brief / Meds / Census answer enough to act on it (with a chart-verify step for orders), and shaves minutes off rounds and handoff.

## Brand Personality

Clinical, calm, expert. Three words: **trustworthy, terse, deferential**. The voice never claims certainty it cannot back with citations. It defers to the chart for anything order-bound. It uses neutral clinical language — never marketing-warm, never chirpy. Emotional goal: a tired physician at 2am should feel like a competent colleague is handing them a structured summary, not a chatbot performing helpfulness.

## Anti-references

- **Consumer-AI chat aesthetic** (ChatGPT-style giant avatars, big rounded bubbles, gradients-as-personality, emoji reactions). This is a clinical tool inside an EHR, not a friend.
- **Hospital-software cliché** (saturated navy + teal, stock stethoscope iconography, clip-art reassurance). Reflex healthcare palette is exactly the trap to avoid.
- **SaaS dashboard dazzle** (hero metrics, gradient cards, rainbow status pills, Linear-clone). Wrong register — this is decision support, not analytics.
- **Generic Material Design** (floating action buttons, ripple effects, big elevated cards). Visual noise where signal density matters.
- **Anything that looks AI-generated** — gradient avatars, sparkle icons used as decoration, "AI is thinking…" with shimmer effects. The AI affordance must be honest and quiet.

## Design Principles

1. **Signal over surface.** Every pixel earns its place by carrying clinical information or by structuring it. Decoration that does not separate signal from noise is removed.
2. **Defer to the chart.** The UI never pretends to be the system of record. Citations, "Verify in Chart", and timestamps are first-class — not afterthoughts in footers.
3. **Triage-first hierarchy.** P1/P2 outranks layout symmetry. When the design and the clinical priority disagree, the clinical priority wins.
4. **Calm density.** A hospitalist scans, they don't read. Information density is high but rhythm is steady — no surprise bright accents, no motion that demands attention it didn't earn.
5. **Honest affordances.** A button that opens the chart looks like it leaves the surface. A refusal looks like a refusal. A cached answer looks cached. The UI never over-sells what the agent can do.

## Accessibility & Inclusion

- **WCAG 2.1 AA minimum.** Hospital workstations span a wide range of monitors and lighting; contrast must hold under fluorescent overhead light and on aged TN panels.
- **Keyboard-complete.** Every action — Send, Brief, Meds, Generate Handoff, Verify in Chart, Refresh, expand/collapse a message, the "New messages" pill — must be reachable and operable without a mouse. Physicians often work one-handed (phone in the other).
- **Screen reader labels** on every icon-only control. The collapse chevron, AI avatar, severity dots, and progress spinner all need text equivalents (most already do — the audit will flag any that don't).
- **`prefers-reduced-motion`** must disable the spinner animation, the smooth scroll, and any future motion.
- **Color is never the only signal.** Severity (RED/AMB/NEU) is always paired with a label, dot shape, or position. A red-green colorblind physician must read the same priority order as everyone else.
- **Tappable targets ≥ 44px** for the bedside-tablet case, even though the desktop iframe is the primary surface.
