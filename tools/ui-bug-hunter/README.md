# ui-bug-hunter

Autonomous Claude agent that drives a browser via the Playwright MCP server to explore the OpenEMR UI, try to break things, and write a findings report.

## Quick start

```bash
cd tools/ui-bug-hunter
npm install
npm run install-browsers
ANTHROPIC_API_KEY=sk-... npm run hunt
```

Make sure OpenEMR is running locally (default `docker/development-easy/`) and reachable at `http://localhost:8300/` before launching.

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `ANTHROPIC_API_KEY` | (required) | Claude API key. |
| `TARGET_URL` | `http://localhost:8300/` | Base URL to attack. |
| `USERNAME` | `admin` | Login username. |
| `PASSWORD` | `pass` | Login password. |
| `MAX_TURNS` | `60` | Hard cap on agent turns. |
| `HEADED` | unset | Set to `1` to watch the browser. |

## Output

Reports land in `tools/ui-bug-hunter/reports/ui-findings-<ISO timestamp>.md`. They are gitignored by default.

Sections: Summary, Critical, Major, Minor, Console Errors, Network Failures, Notes.

## Notes

This is a defensive QA tool. Do not point it at production systems.
