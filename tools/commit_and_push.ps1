Param(
    [string]$Message = "Update from assistant"
)

git add -A
# Commit if there are staged changes
try {
    git commit -m $Message
} catch {
    # no changes to commit
}

git push -u origin main

Write-Output "Committed and pushed."
