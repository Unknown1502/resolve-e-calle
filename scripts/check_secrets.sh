#!/usr/bin/env bash
# Quality gate from docs/hackathon-build/build-notes.md:
#   "no secret appears in frontend bundle or logs"
#
# Run before recording a demo or opening a PR.
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
PATTERN='iams_[A-Za-z0-9_-]{4,}|CALLE_API_KEY=[^[:space:]]+|Bearer [A-Za-z0-9._-]{8,}'
# High-confidence version, for scanning real source/doc content rather
# than build output: a 20+ char suffix comfortably clears any
# hand-written placeholder ("iams_test_key" is 8 chars after the
# prefix; a format comment mentioning "iams_live_…" has none) while
# staying far below a real key's ~85-char suffix. The loose PATTERN
# above matched both of those harmless cases and would have made this
# check unusable with real source in the tree.
STRICT_PATTERN='iams_(live|test)_[A-Za-z0-9_-]{20,}'

echo "== built frontend bundle =="
if [ -d frontend/dist ]; then
  if grep -rIEl "$PATTERN" frontend/dist/ 2>/dev/null; then
    echo "  FAIL: a credential reached the browser bundle"; fail=1
  else
    echo "  ok: no credential in frontend/dist"
  fi
else
  echo "  skipped: no build yet (cd frontend && npm run build)"
fi

echo "== tracked files =="
if git ls-files -z 2>/dev/null | xargs -0 grep -IEl "$PATTERN" 2>/dev/null | grep -v '^scripts/check_secrets.sh$'; then
  echo "  FAIL: a credential is committed"; fail=1
else
  echo "  ok: no credential in tracked files"
fi

echo "== files that WOULD be committed (git ls-files returns nothing until"
echo "   the first commit exists -- this is the check that actually matters"
echo "   before that point, and is the one that missed a real leaked live"
echo "   key in .env.example earlier in this project's history) =="
if git ls-files --others --exclude-standard -z 2>/dev/null \
     | xargs -0 grep -IEl "$STRICT_PATTERN" 2>/dev/null \
     | grep -v '^scripts/check_secrets.sh$'; then
  echo "  FAIL: a credential is present in an untracked-but-not-ignored file"
  echo "        (this file would be staged by 'git add -A' right now)"
  fail=1
else
  echo "  ok: no credential in any file git would currently stage"
fi

echo "== .env is ignored =="
if git check-ignore -q .env 2>/dev/null; then
  echo "  ok: .env is git-ignored"
else
  echo "  FAIL: .env is NOT ignored"; fail=1
fi

echo "== running container logs =="
if docker compose ps --quiet api >/dev/null 2>&1; then
  if docker compose logs 2>/dev/null | grep -IEq "$PATTERN"; then
    echo "  FAIL: a credential appears in logs"; fail=1
  else
    echo "  ok: no credential in logs"
  fi
else
  echo "  skipped: stack not running"
fi

[ "$fail" -eq 0 ] && echo "PASS" || echo "FAILED"
exit "$fail"
