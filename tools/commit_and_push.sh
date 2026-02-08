#!/usr/bin/env bash
msg="${1:-Update from assistant}"

git add -A
if git diff --cached --quiet; then
  echo "No changes to commit."
else
  git commit -m "$msg"
fi

git push -u origin main

echo "Committed and pushed."
