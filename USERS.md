# Clinical Co-Pilot — Users & Use Cases

This document defines the target user, their workflow, and the specific use cases the Clinical Co-Pilot agent must solve. It is the source of truth that W1_ARCHITECTURE.md must trace back to. Every agent capability built must point to a use case defined here.

---

## Quick Read

The agent is built for one person in one very specific situation: a hospitalist physician rounding on 14 to 18 patients in the morning, with most of those patients admitted overnight while she wasn't there.

Her name is Dr. Sarah Chen — a composite built from real hospitalist workflows. She arrives at 6:50 AM, picks up a census sheet with bed numbers and admit diagnoses in the overnight resident's handwriting, and has twenty minutes before her team assembles. In that window she needs to understand every patient on the service well enough to run the morning. Right now she does that by reading handwritten sign-out notes, scanning the EHR inbox for critical labs, and checking nursing escalation flags — by hand, one patient at a time. It takes ten to fifteen minutes on a good day, and the quality of the result depends entirely on how thorough the overnight sign-out was. Last Tuesday the sickest patient was third on the sign-out because the resident missed a potassium of 6.1. The agent exists to catch that.

When Dr. Chen opens the agent, the first thing she gets — automatically, without asking — is a ranked list of all her patients sorted by clinical urgency. Not by bed number, not alphabetically. The patient with unacknowledged critical labs or sepsis-criteria vitals is at the top. The patient who had a quiet night is at the bottom. Every ranking traces back to a specific data point so she can ask "why is bed 7 first?" and get a one-sentence answer before the huddle.

During rounding she uses the agent between rooms. She has roughly ninety seconds in the hallway before she walks in. She taps a patient's name and gets a three-to-four sentence briefing: who the patient is, why they were admitted, what changed overnight, and the one thing she needs to know before she enters. Every clinical claim cites its source and timestamp. If there's a critical flag, it leads the briefing — it's never buried. If data is incomplete, the agent says so explicitly rather than filling in the gaps with inference.

She also asks follow-up questions during rounding: "what did the last echo show?", "has she been on steroids before?", "what's her creatinine trend?" The agent searches the chart and gives a direct answer with a citation, or tells her exactly what it searched and what wasn't found. It never guesses. At the end of rounds, she asks for handoff. The agent generates one structured paragraph per patient — diagnosis, morning events, current plan, one open item — and she reviews, edits if needed, and sends it.

There are hard limits. The agent never places an order, never makes a diagnosis, never recommends ICU transfer. It surfaces what the chart says. The physician makes every clinical decision. Every response ends with the data's timestamp and a reminder to verify critical values directly in the chart.

The use cases and acceptance criteria below are the contract the architecture must satisfy. Every feature built maps to one of these five use cases or it doesn't get built.

---

---

## 1. Target User Profile

**Name:** Dr. Sarah Chen (composite persona — behaviors based on real hospitalist workflows)  
**Role:** Attending Hospitalist, Internal Medicine  
**Experience:** 6 years post-residency, board-certified Internal Medicine  
**Shift:** 7:00 AM – 7:00 PM, rounding window 7:00 AM – 11:30 AM hard stop  
**Team composition:** 1 attending (Dr. Chen), 2 medicine residents (PGY-1 and PGY-2), 1 medical student, 1 case manager joining at 9 AM, consult teams available after 8 AM  
**Census:** 14–18 total inpatients on the medicine service. Of those, 8–12 are new overnight admissions she has not personally examined.  
**Where she rounds:** Mobile workstation cart in hallway, pre-round in team room 6:50–7:10 AM, occasionally bedside for complex patients.

---

## 2. Workflow Overview

Dr. Chen arrives at 6:50 AM to find a paper census sheet on the team room table — bed numbers and admit diagnoses in the overnight resident's handwriting. Before the team assembles, she has twenty minutes to form a mental model of every patient on the service, triage the sick ones, and decide how to route the morning.

From 6:50 to 7:10 AM she runs pre-round prep. Currently this is manual labor: she reads the overnight sign-out, scans the EHR inbox for critical labs, checks nursing notes for escalations, and writes a hand-prioritized list on the census sheet. The process takes ten to fifteen minutes. The quality of the resulting list depends entirely on the quality of the overnight sign-out, which varies widely by resident. The agent replaces this process entirely.

At 7:10 AM the team huddles. Dr. Chen assigns patients to residents based on the priority order she has already formed. The allocation decisions — who sees which patient first, which rooms get attending-only coverage — are made in this window before anyone enters a room.

From 7:15 AM to 11:00 AM the team moves room to room. Transit between rooms averages 90 seconds. That window is all the time she has for a focused query before stepping through the door. Bedside encounters run 8 to 12 minutes. The agent is used in the hallway, not at bedside; it must deliver answers in the window between knocking and entering.

At 11:00 to 11:30 AM the team reconvenes for a debrief and disposition decisions: who goes home, who needs another night, who needs escalation. From 11:30 AM to noon Dr. Chen prepares handoff for the afternoon attending. The agent generates the handoff draft.

Dr. Chen is interrupted constantly — nursing calls, rapid responses, consult teams stopping her in the hallway. She frequently leaves a conversation mid-thread and returns 20 or more minutes later. The agent must maintain conversation context across these gaps without requiring her to re-explain the patient or the question.

---

## 3. The 30 Seconds Before She Opens the Agent

It is 6:52 AM. The team room smells like burnt coffee. Dr. Chen pours a cup anyway, sets it on the counter, and looks at the census sheet. Fourteen names. Eight of them were admitted overnight by the PGY-1 — a solid resident, careful, but it was a busy night and the sign-out notes are thinner than she would like.

The PGY-1 is at the table. Dr. Chen could ask who is sickest and trust the answer. Most mornings she would. But last Tuesday the sickest patient on the list was in bed 7, and bed 7 was third on the sign-out because the resident had focused on the admit diagnosis and missed the overnight potassium of 6.1 that nobody had addressed.

She opens the agent instead. She wants to verify the resident's read against the chart before she commits the team's morning to it. The use case is not to replace the resident's judgment — it is to stress-test it against objective data in the two minutes before the huddle begins.

---

## 4. Information Needs Per Patient

Per admission, the agent must have immediate access to:

- Chief complaint and admit diagnosis
- Overnight events summary (acute changes, nursing escalations)
- Vitals trend — last 6 hours, direction of travel
- Critical and abnormal labs from overnight (flagged, not just listed)
- Active medication list — especially new medications added overnight
- Code status
- Isolation status
- Pending items: consults not yet responded, imaging ordered but not resulted

**Auto-highlight triggers** (agent must surface these without being asked):

- Any lab value outside critical threshold
- Vitals meeting SIRS or sepsis criteria (qSOFA ≥ 2)
- NEWS2 score ≥ 5
- Medication added overnight that interacts with an existing documented allergy
- Consult request unanswered > 6 hours
- Code status documented as unknown or blank

**The four decisions she makes at or before each patient encounter:**

1. Does this patient need ICU transfer before I walk in?
2. Is this patient a discharge candidate today?
3. Does this patient need urgent imaging or procedure?
4. Do I need to call a family meeting?

Every agent output must map to helping her answer one of these four questions. If a data point does not help answer one of these four questions, the agent should not surface it by default.

---

## 5. Use Cases

### Notification Policy

The agent operates on a pull-first model. Dr. Chen opens the agent — the agent does not interrupt her before she does.

Pull behavior (default):
- UC-1 triage list is generated automatically the moment she opens the agent session. No query required. No prior notification sent.
- All other use cases are pull — she asks, the agent answers.
- The agent never sends unsolicited messages to her phone, pager, inbox, or any other channel during an active session.

Push behavior (one exception only):
- If a patient on her service meets ALL THREE of the following simultaneously, the agent may push a single pre-session alert to her OpenEMR inbox:
  1. Vitals meeting qSOFA ≥ 2 within the last 30 minutes
  2. No physician acknowledgment of the deterioration in the chart
  3. Her shift is active (she is the attending of record)
- The push alert contains: patient name, bed number, the criteria met, and a direct link to open the agent on that patient.
- This is the only condition under which the agent pushes anything.
- All other flags appear in the UC-1 triage list when she opens the agent. They do not generate pre-session push alerts.

Opt-out: Dr. Chen can disable the push exception entirely in her agent preferences. Default is push enabled for the qSOFA-only condition.

Rationale: Push notifications in clinical settings cause alert fatigue and interrupt focus. The agent earns trust by being present when opened, not by demanding attention before it is sought. The qSOFA exception exists because active deterioration while the physician is off the floor is the one scenario where a pull model has patient safety consequences.

---

### UC-1: Morning Priority Triage

**ID:** UC-1  
**Name:** Morning Priority Triage  
**Trigger:** Agent session opens at 6:50 AM  
**Input required:** Full census roster, overnight vitals, overnight labs, nursing escalation flags, consult response status, code status for all patients  
**Output:** Ranked list of all patients, one line each, urgency-sorted. Each line includes bed number, admit diagnosis, one key flag, and a single-word urgency tag (Urgent / Watch / Stable). Ranking is dynamic — based on overnight events, not static acuity scores.  
**Delivered:** Automatically generated on session open. No query required.  
**Responsible to act:** Dr. Chen — reviews list, adjusts if clinical knowledge overrides, uses as huddle input  
**Why a conversational agent and not a dashboard:** A dashboard displays the same data in the same order every time. It cannot rerank based on what changed overnight versus what was already known. It cannot explain why a patient moved up the list. Dr. Chen needs to ask "why is bed 7 ranked first?" and get a one-sentence answer before the huddle begins. A dashboard cannot answer that question.  
**Acceptance criteria:**
- All patients with at least one critical flag appear in the top 4 positions
- Zero patients with an urgent flag appear below rank 8
- Full census list delivered in < 15 seconds
- Every ranking is traceable to a specific overnight event or value

#### Ranking Priority Weights

The triage list ranks patients by the highest-severity signal present. Each patient is assigned the priority level of their single most critical active signal. Priority levels and triggering conditions in descending order:

| Priority | Label | Triggering Condition |
|---|---|---|
| 1 | URGENT | qSOFA ≥ 2 based on current vitals |
| 2 | URGENT | Critical lab value posted with no physician acknowledgment |
| 3 | URGENT | Rapid response or code event in last 12 hours |
| 4 | WATCH | NEWS2 score ≥ 5 |
| 5 | WATCH | New medication added overnight matching documented allergy |
| 6 | WATCH | Discharge plan documented but critical result still pending |
| 7 | WATCH | Code status blank or unknown |
| 8 | WATCH | Consult unanswered > 6 hours |
| 9 | WATCH | Abnormal but non-critical lab value unacknowledged |
| 10 | STABLE | No active flags — stable overnight course |

Tie-breaking rule: if two patients share the same priority level, the patient with the more recent triggering event ranks higher. Example: two patients both at priority 4 (NEWS2 ≥ 5) — the one whose NEWS2 crossed threshold more recently ranks first.

Multi-flag behavior: a patient with multiple simultaneous flags is ranked by their highest-priority flag only. All active flags are shown in the triage line for that patient. The ranking is not additive — a patient with three WATCH flags does not outrank a patient with one URGENT flag.

Config note: these weights reflect Dr. Chen's stated preferences for internal medicine hospitalist context. The priority table is configurable per deployment and per specialty. The ranking algorithm reads from this table — changing the table changes the ranking without a code deployment.

---

### UC-2: Pre-Encounter Briefing

**ID:** UC-2  
**Name:** Pre-Encounter Briefing  
**Trigger:** Dr. Chen taps a patient name on the census list or asks "tell me about bed 512"  
**Input required:** Admit note, overnight nursing notes, current vitals, overnight labs, active medications, code status, isolation status, pending consults and imaging  
**Output — two states:**

Default state (shown immediately, no interaction required):
- 3–4 sentence briefing containing: who the patient is, why they are admitted, what changed overnight, and one flag to address before entering the room
- Every clinical claim includes an inline source citation: (source type, date, time). Example: "K+ 5.9 this morning (lab result, 0312)."
- If a critical flag exists it appears as the first sentence, not buried at the end
- If no flags exist the briefing ends with: "No urgent flags at this time as of [timestamp]."

Expanded state (shown when she taps "More" or asks "tell me more"):
The expanded view contains the following sections in this exact order:

1. Admit summary — one sentence: admit date, admitting diagnosis, admitting physician, admit source (ED, transfer, direct)

2. Overnight events — bullet list of discrete events since last physician note: new nursing escalations, acute vitals changes, new lab results posted, new orders placed by overnight team, any rapid response or code events. Each bullet cites source and timestamp.

3. Current vitals — most recent complete vital sign set with timestamp. Trend indicator for each value: stable / improving / worsening based on last 6 hours. Abnormal values in bold.

4. Active medications — full current med list. New medications added in last 12 hours flagged with NEW label. Any medication with a potential chart-based concern flagged with REVIEW label and one-line reason.

5. Pending items — explicit list of: unanswered consult requests (with hours elapsed), imaging ordered but not resulted (with hours elapsed), labs ordered but not resulted, any plan items from the admit note marked as pending.

6. Code status and isolation — always shown, always explicit. Never omitted. If blank: "Code status: NOT DOCUMENTED — verify before rounds."

Items never included in the expanded view:
- Billing or insurance information
- Scheduling or appointment data
- Information about other patients
- Any claim not traceable to a source record

Collapsed state: when she collapses from expanded back to default, the original 3–4 sentence briefing is restored exactly as it was. The expanded view does not alter the default state.

**Delivered:** Inline in the conversation thread  
**Responsible to act:** Dr. Chen — uses briefing to frame the bedside encounter  
**Why a conversational agent and not a dashboard:** The briefing length and emphasis must adapt to the specific patient. A dashboard showing the same fields for a post-op day 2 knee replacement and a new COPD exacerbation with three overnight nursing escalations is not useful. Dr. Chen needs a different briefing for each patient — one that leads with what matters for that patient today. That requires inference, not display.  
**Acceptance criteria:**
- Every clinical claim is traceable to a source record with timestamp
- No claim is made about a value not present in the chart
- When data is incomplete, uncertainty is stated explicitly
- Delivered in < 5 seconds

---

### UC-3: Targeted Record Query

**ID:** UC-3  
**Name:** Targeted Record Query  
**Trigger:** Follow-up question during rounding — "what did the last echo show?" / "has she been on steroids before?" / "what's her creatinine trend?"  
**Input required:** Relevant chart sections determined by query type (imaging results, medication history, lab history)  
**Output:** Direct answer with source citation — note type, date, ordering or documenting clinician. If the queried information is not found: an explicit statement of what was searched and what was absent, not a guess.  
**Delivered:** Inline in the conversation thread  
**Responsible to act:** Dr. Chen — uses the answer to make a clinical decision in the hallway  
**Why a conversational agent and not a dashboard:** Dr. Chen does not know in advance what she will need to ask. A dashboard requires the information to be anticipated and surfaced in a panel. A creatinine trend from six months ago is not on any dashboard. The ability to ask arbitrary retrospective questions against the chart is the core capability.  
**Acceptance criteria:**
- Zero confident incorrect answers — the agent does not speculate when data is absent
- Uncertainty is always stated when data is incomplete or ambiguous
- Not found responses specify which sections were searched
- Delivered in < 3 seconds
- Default historical search depth: 24 months of encounters, 12 months of lab results, full medication history with no date cutoff
- Every not found response explicitly states the search window used. Example: "No echo results found in the last 24 months of encounters. If older records exist they are outside the default search window."
- Physician can override the default window for a single query by requesting explicitly: "search all records for X"

---

### UC-4: Medication Safety Surface

**ID:** UC-4  
**Name:** Medication Safety Surface  
**Trigger:** Dr. Chen asks "she is on metoprolol — anything in her chart I should know?" or the agent auto-flags a new overnight medication  
**Input required:** Documented allergies, active problem list, relevant labs (electrolytes, renal function, hepatic function), active medication list  
**Output:** Structured chart-data-only safety summary. Lists documented allergies, relevant diagnoses from the problem list, and relevant lab values. Does not recommend dosing. Does not diagnose drug interactions. Surfaces chart data — the physician makes the clinical call.  
**Delivered:** Inline in the conversation thread, or as an auto-flag appended to the UC-2 briefing when a new overnight medication is detected  
**Responsible to act:** Dr. Chen  
**Why a conversational agent and not a dashboard:** A dashboard can display an allergy list. It cannot answer "is there anything in her chart relevant to this specific medication?" in a way that draws from problem list, labs, and allergy section simultaneously and presents only what is relevant to that drug in that patient. That synthesis requires a query, not a panel.  
**Acceptance criteria:**
- Never states "no known allergies" when the allergy section is blank — always flags section incompleteness
- Always surfaces potassium, renal function, and hepatic function when present and relevant
- Always flags allergy section when any field is incomplete
- Delivered in < 5 seconds

---

### UC-5: End-of-Rounds Handoff Generation

**ID:** UC-5  
**Name:** End-of-Rounds Handoff Generation  
**Trigger:** "Give me handoff for all my patients" spoken or typed at approximately 11:30 AM  
**Input required:** Full census, morning encounter notes if available, current plan documentation, pending results and consults  
**Output:** One structured paragraph per patient: primary diagnosis, key morning event or change, current plan, one open item for the afternoon team. Formatted for verbal handoff or secure message.  
**Persistence policy:** Generated handoff text is clipboard-copy only in v1. It is not written to the EHR handoff tool automatically and does not appear in the official medical record. Dr. Chen reviews the output, edits if needed, and pastes it to her secure message system or reads it aloud to the incoming team. Auto-persistence to the EHR handoff module is a v2 feature pending governance and compliance approval. No AI-generated text enters the official chart without explicit physician action.  
**Delivered:** As a full-census block in the conversation thread, copyable as a unit  
**Responsible to act:** Dr. Chen — reviews, corrects if needed, delivers to afternoon attending  
**Why a conversational agent and not a dashboard:** Handoff generation requires assembling information from multiple chart sections into a narrative that reflects what happened this morning, not just what the chart contains. A dashboard can display fields. It cannot write "K improved from 5.9 to 5.2 after kayexalate this morning — recheck this afternoon" from the combination of an overnight lab, a nursing medication administration record, and a morning repeat lab. That synthesis is the entire value.  
**Acceptance criteria:**
- Every open item traces to a specific pending result or unresolved plan element in the chart
- Dr. Chen makes fewer than 2 corrections before using it
- Full census handoff delivered in < 20 seconds

---

## 6. What the Agent Must Refuse

The agent explicitly refuses to:
- Place, modify, or suggest specific medication orders
- Make a diagnosis
- Recommend ICU transfer directly — it can flag criteria that suggest reassessment, it cannot say "transfer to ICU"
- Discharge a patient or generate a discharge order
- Access records of patients not on Dr. Chen's current service
- Answer questions about other physicians' patients unless documented cross-coverage exists

**Standard agent language:**

- "I can surface what the chart says about X. The clinical decision is yours."
- "This patient meets 2 of 3 qSOFA criteria based on current vitals as of [time]. This warrants your assessment."
- NEVER: "This patient has sepsis."
- ALWAYS: "Current vitals meet qSOFA criteria of 2/3 — see vitals documented at [time]."

Every agent response includes:

> *This summary is generated from EHR data as of [timestamp]. Verify critical values directly in the chart.*

---

## 7. Success Metrics

### Primary Metrics

| Metric | Baseline | Target |
|---|---|---|
| Pre-round prep time per patient | ~75 seconds | < 25 seconds |
| Time-to-first-action on agent-surfaced flags | unmeasured | < 2 minutes |
| Agent queries per rounding session | 0 (no agent) | ≥ 10 |
| Missed critical lab rate | unmeasured | 0 unacknowledged critical flags |

### Latency Targets by Use Case

| Use Case | Target |
|---|---|
| UC-1 — Morning triage list | < 15 seconds |
| UC-2 — Patient briefing | < 5 seconds |
| UC-3 — Targeted record query | < 3 seconds |
| UC-4 — Medication safety surface | < 5 seconds |
| UC-5 — Handoff generation | < 20 seconds |

**Cost target:** < $0.02 per patient briefing (LLM inference cost)

### Output Format Defaults

- Pre-round list: one line per patient
- Patient briefing: 3–4 sentences default, expandable on request
- Tone: concise clinical — not verbose, not teaching-style

**Example of correct tone:**
> "K 5.9, trending up over 6h. On lisinopril. Verify before morning orders."

**Example of incorrect tone:**
> "Hyperkalemia is defined as a serum potassium above 5.0 mEq/L and can be caused by..."

### Pilot Evaluation Plan

Scope: Single medicine service, 3 attending hospitalists, 2-week observation period.

Baseline measurement (week 1, agent off):
- Pre-round prep time per patient: timed from census review start to first room entry, divided by patient count
- Critical lab acknowledgment time: time between lab result posting and first physician chart action
- Handoff generation time: time to produce complete verbal handoff for full census

Agent measurement (week 2, agent on):
- Same three metrics collected under identical conditions
- Agent queries per session logged automatically
- Flags surfaced vs flags acted on tracked per session

Go/no-go criteria for service-wide rollout:
- Greater than 30% reduction in pre-round prep time per patient
- Zero confidently incorrect responses on critical values (K+, Na+, creatinine, code status) across all sessions
- Agent adoption rate greater than 80% of eligible rounding sessions
- Physician-reported trust score greater than 4 out of 5 on post-pilot survey

If go/no-go criteria are not met, the pilot extends one additional week with a documented remediation plan before escalation to department-wide rollout.

---

## 8. Edge Cases the Agent Must Handle

| Scenario | Required Agent Behavior |
|---|---|
| Vitals meeting qSOFA ≥ 2 | Flag prominently at the top of the UC-2 briefing — not buried in a list |
| Critical lab posted overnight with no physician acknowledgment | Auto-surface in UC-1 triage; flag again in UC-2 briefing |
| Code status blank or unknown | Flag for every patient, every time — never omit this flag |
| Medication added overnight matching a documented allergy | Surface immediately in UC-2 and UC-4, regardless of whether queried |
| Discharge plan documented but critical result still pending | Flag explicitly: "discharge plan noted but [result] still pending as of [time]" |
| Incomplete allergy section | Never state "no known allergies" — always state "allergy section incomplete: verify directly" |
| Session interrupted mid-conversation | Maintain context; resume without requiring re-explanation of patient or question |
| Agent data feed unavailable | Degrade gracefully — "agent unavailable, view directly" with a direct link to the relevant chart section. Never fail silently. Never block access to OpenEMR. |
| High census (> 16 patients) | Automatically compress output format; raise auto-flag threshold to prevent alert fatigue |
| Conflicting records (two notes with different values for the same field) | Surface both values with timestamps and sources — never silently resolve the conflict |
| Agent outputs — persistence | All summaries, briefings, and generated text exist only within the active agent session. When the session ends, outputs are not persisted anywhere in OpenEMR. The agent never implies its output has been saved or filed. |
| Alert fatigue — multiple simultaneous urgent flags | When 4 or more patients meet urgent flag criteria simultaneously, surface the top 3 most critical individually with full detail, then group the remainder under a single expandable "Additional flags (N)" entry. Dr. Chen can ask "show me all flags" at any time to see the complete unfiltered list. Grouping is visual consolidation only — no flag is ever silently dropped. |
| Cross-coverage invocation — Dr. Chen covering a colleague's patients | Agent does not automatically grant access to non-service patients. Agent prompts: "Patient [name] is not on your current service. Do you have documented cross-coverage authorization for this patient?" If she confirms: agent grants session-scoped access to that patient only — access expires at session end and does not persist automatically. Every cross-coverage access event is written to the OpenEMR audit log with: her user ID, patient ID, timestamp, and the text "cross-coverage session access — physician confirmed." A persistent banner displays during cross-coverage: "Cross-coverage mode — [patient name] — access expires at session end." The agent never grants blanket access to all of a colleague's patients from a single confirmation. |

---

## 9. Extended User Scope

The agent is designed around Dr. Chen, but the five use cases are rounding-physician use cases, not hospitalist-specific ones. Any inpatient attending who rounds on a defined census with 90-second windows between rooms maps directly onto UC-1 through UC-5 without workflow changes. The clinical rules layer is the only specialty-specific component, and it is configurable by design.

### Physician Type Tier Summary

| Tier | Physician Types | What Changes |
|---|---|---|
| Zero changes required | Nocturnist, locum hospitalist, internal medicine floor attending, teaching attending | Nothing (add one RBAC layer for teaching attending) |
| Configuration only | Surgical hospitalist, neurohospitalist, cardiologist, pediatric hospitalist | Clinical flag rules in config file |
| Requires meaningful work | ED attending, ICU intensivist, outpatient PCP | Workflow and use case layer redesign |

### Zero Changes Required

These physician types can use the agent immediately with no modification:

- Nocturnist — identical workflow, different shift timing
- Locum hospitalist — identical workflow; no institutional memory of patients, which increases agent value
- Internal medicine floor attending — same role, different hospital
- Teaching attending — same data needs; add one RBAC layer to separate resident view from attending view

### Configuration Changes Only

These physician types need specialty-specific clinical flag rules updated in a configuration file. The workflow layer, RBAC model, and all five use cases remain identical:

- Surgical hospitalist — wound complication flags replace sepsis-first defaults
- Neurohospitalist — NIH Stroke Scale flags, neuro-specific lab thresholds
- Cardiologist rounding on inpatients — EKG-based triggers, cardiac biomarker thresholds
- Pediatric hospitalist — weight-based dosing flags, pediatric-specific vital sign thresholds

```yaml
specialty: internal_medicine
auto_flags:
  - rule: qSOFA
    threshold: 2
    priority: urgent
  - rule: NEWS2
    threshold: 5
    priority: watch
  - rule: critical_lab
    fields: [potassium, sodium, creatinine]
    priority: urgent
```

Changing `specialty` and the `auto_flags` block is the entire scope of a specialty adaptation. No code changes required.

### Requires Meaningful Work

These physician types have fundamentally different workflow shapes that do not map onto the five use cases:

- ED attending — no pre-defined census; patients arrive dynamically; triage model replaces rounding model
- ICU intensivist — continuous monitoring, not episodic rounding; different data model entirely
- Outpatient PCP — no rounding; longitudinal not episodic; pre-visit prep replaces pre-round prep

These are not out of scope permanently — they are out of scope for this build. The authorization model and access layer built for Dr. Chen are reusable. The workflow and use case layer would need to be redesigned.

### RBAC Model

The permission structure built around "attending-on-service sees these patients" generalizes to any inpatient attending role without changes. Resident supervision, cross-coverage, and consult access are already modeled in the architecture and support all physician types in the zero-changes and configuration-changes tiers without additional authorization work.

### What Generalizes and What Does Not

To be explicit about what any rounding physician inherits from this design versus what they would need to configure or rebuild:

| Component | Generalizes | Notes |
|---|---|---|
| UC-1 through UC-5 workflow | Yes — all rounding physicians | No changes needed |
| RBAC and session model | Yes — all tiers | Attending-on-service permission model |
| Verification layer | Yes — all tiers | Source citation requirement is universal |
| Observability and logging | Yes — all tiers | Audit trail is not specialty-specific |
| Clinical flag rules | Config only | Specialty thresholds in config file |
| Auto-flag thresholds | Config only | Numeric values configurable per deployment |
| Historical search depth | Config only | Default 24 months encounters, adjustable |
| Output tone and format | Config only | Concise clinical is default, adjustable |
| Workflow shape (rounding model) | No — rounding physicians only | ED and ICU require redesign |
| Use case set UC-1 to UC-5 | No — ED and ICU only | Those personas need a new use case set |

The five use cases in this document are the contract between the user and the system. Every item in the Generalizes column is available to any rounding physician without modifying that contract. Every item in the Config only column requires a deployment decision but not an engineering change. Every item in the No column requires a new USERS.md before a line of code is written.
