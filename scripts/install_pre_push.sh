#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_PATH="$REPO_ROOT/.git/hooks/pre-push"

cat > "$HOOK_PATH" <<'EOF'
#!/usr/bin/env bash
# W2 advisory pre-push hook — runs a 10-case smoke. NEVER blocks the push.
# CI on GitHub Actions is the canonical gate. Only red CI on the PR blocks
# submission. This hook exists to catch obvious regressions before the
# 5–10 minute CI wait.

set +e   # never exit non-zero — that would block the push

cd "$(git rev-parse --show-toplevel)/agent-api" || exit 0
python3 -m pytest tests/test_w2_eval_smoke.py -m smoke -q 2>&1 | tail -10
RC=$?

if [ $RC -ne 0 ]; then
  echo ""
  echo "  ⚠️  Pre-push smoke FAILED — push proceeds anyway."
  echo "  CI is the canonical gate; check your PR's W2 Eval Suite job."
  echo ""
fi

exit 0
EOF

chmod +x "$HOOK_PATH"

echo ""
echo "✅ Pre-push hook installed at .git/hooks/pre-push"
echo ""
echo "  ADVISORY ONLY: this hook NEVER blocks a push."
echo "  CI on GitHub Actions is the canonical gate."
echo "  Only red CI on the PR blocks submission."
echo "  The hook exists to catch obvious regressions before the"
echo "  5–10 minute CI wait, not to be the gate itself."
echo ""
