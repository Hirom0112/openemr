#!/usr/bin/env bash
# Advisory guard: flag hex color literals inside agent-ui *Renderer.tsx files.
# Tool renderers should consume colors from agent-ui/src/styles/tokens.ts so the
# census visual stays the canonical schema for every tool output.
#
# CitationsPanel.tsx is exempt: its CLAIM_COLORS map encodes per-claim-class
# semantics (lab/vital/medication/...), not theme.
#
# Usage: tools/check-renderer-hex-literals.sh
# Exits non-zero if any *Renderer.tsx file contains a /#[0-9a-fA-F]{3,8}/ literal.

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
target_dir="${repo_root}/agent-ui/src/components"

# Match #fff, #abcd, #abcdef, #abcdef12. Skip files that don't end in Renderer.tsx.
# Allow primitive black/white sentinels (#111, #fff, #16a34a green dot inside LivePill).
matches="$(grep -nE "#[0-9a-fA-F]{3,8}\b" "${target_dir}"/*Renderer.tsx 2>/dev/null \
  | grep -vE "'#(111|fff|16a34a)'" \
  || true)"

if [ -n "${matches}" ]; then
  echo "Hex color literals found in *Renderer.tsx — import from styles/tokens.ts instead:" >&2
  echo "${matches}" >&2
  exit 1
fi

echo "OK: no hex literals in agent-ui/src/components/*Renderer.tsx"
