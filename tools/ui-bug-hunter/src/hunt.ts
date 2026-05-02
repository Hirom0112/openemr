import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { query } from "@anthropic-ai/claude-agent-sdk";

const __dirname = dirname(fileURLToPath(import.meta.url));
const toolRoot = resolve(__dirname, "..");
const reportsDir = resolve(toolRoot, "reports");
mkdirSync(reportsDir, { recursive: true });

const targetUrl = process.env.TARGET_URL ?? "http://localhost:8300/";
const username = process.env.USERNAME ?? "admin";
const password = process.env.PASSWORD ?? "pass";
const maxTurns = Number.parseInt(process.env.MAX_TURNS ?? "60", 10);
const headed = process.env.HEADED === "1";

const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
const reportPath = resolve(reportsDir, `ui-findings-${timestamp}.md`);

const playwrightArgs = ["-y", "@playwright/mcp@latest"];
if (!headed) playwrightArgs.push("--headless");

const systemPrompt = `You are a defensive QA agent hunting bugs in the OpenEMR web UI.

Target: ${targetUrl}
Login: ${username} / ${password}
There is also a "Co-Pilot" tab (clinical-copilot feature). A provider login \`sara\` / \`pass\` exists if you want to test it.

Mission (PRIORITY ORDER):

1. **Co-Pilot tab — this is the highest priority.** After login, find and click the "Co-Pilot" tab in the navigation. It opens a chat panel (iframe) that auto-dispatches a census query on open. Test it exhaustively:
   - Confirm the panel loads, greets the user, and the census auto-runs within ~5 seconds.
   - Click EVERY interactive element in the chat: patient cards, expand/collapse rationale buttons, priority badges (P1–P10), any links, any "ask follow-up" or "show more" controls.
   - Type messages in the chat input box. Try: a normal clinical question ("what's the status of patient pt-001?"), an empty submit, a 5000-character string, weird unicode (emoji, RTL marks, null bytes), HTML/JS injection (\`<script>alert(1)</script>\`), SQL-looking input, and rapid repeated submits.
   - Verify chat responses render, streaming works (if any), and rationale expansion is under 2 seconds.
   - Switch to another nav tab and back — Co-Pilot state must persist.
   - Try logging in as \`sara\`/\`pass\` (provider) to compare behavior.
   - Watch the network tab for calls to \`/agent\`, \`/triage\`, \`/briefing\` endpoints — flag any 4xx/5xx, hangs, or malformed responses.

2. **Broad UI sweep — secondary.** Click through other nav items (Calendar, Flow, Recalls, Messages, Patient, Reports, Admin). Try invalid input in forms (empty required fields, oversized strings, weird unicode, SQL/HTML-looking strings).

3. **Watch always for:** JS console errors, HTTP 4xx/5xx, broken layouts, stuck spinners, unhandled promise rejections, blank pages, dialogs that won't close, hung network requests.

Use the Playwright MCP tools to navigate, click, type, and inspect console + network. Be efficient — Co-Pilot first, then breadth.

IMPORTANT — write the report EARLY and UPDATE IT as you go. Do not wait until the end. Use the Write tool to (re)save the markdown report after the first few findings, then overwrite with new findings as you discover them. If you run out of turns, the last saved version is what we keep. Path:
${reportPath}

The report MUST have these sections, in order:
# Summary
# Critical
# Major
# Minor
# Console Errors
# Network Failures
# Notes

Each finding should include: where it happened (URL or UI path), what you did, what went wrong, and any console/network evidence. If a section has no findings, write "None observed." under it.`;

const userPrompt = `Begin hunting bugs at ${targetUrl}. Log in with ${username}/${password}, explore thoroughly, and write the report to ${reportPath} when finished or before you exhaust your turns.`;

const result = query({
  prompt: userPrompt,
  options: {
    model: process.env.CLAUDE_MODEL ?? "claude-sonnet-4-6",
    systemPrompt,
    maxTurns,
    permissionMode: "bypassPermissions",
    mcpServers: {
      playwright: {
        type: "stdio",
        command: "npx",
        args: playwrightArgs,
      },
    },
  },
});

for await (const message of result) {
  if (message.type === "assistant") {
    for (const block of message.message.content) {
      if (block.type === "text") {
        process.stdout.write(block.text + "\n");
      } else if (block.type === "tool_use") {
        process.stdout.write(`[tool] ${block.name}\n`);
      }
    }
  } else if (message.type === "result") {
    process.stdout.write(`\n[done] ${message.subtype} — turns=${message.num_turns}\n`);
    if (message.subtype === "success") {
      process.stdout.write(`[report] ${reportPath}\n`);
    }
  }
}
