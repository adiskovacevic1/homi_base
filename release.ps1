# homi release (Windows PowerShell): move `stable` forward to what is on `main`, so households get it on their next update.
# Run this from a checkout of main after you have tried the change on your own install.
#   powershell -ExecutionPolicy Bypass -File .\release.ps1          list, confirm, push
#   powershell -ExecutionPolicy Bypass -File .\release.ps1 -Yes     no confirmation
param([switch]$Yes)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

git fetch -q origin main stable 2>$null
$dirty = git status --porcelain
if ($dirty) { Write-Host "Commit or discard your local changes first:" -ForegroundColor Yellow; Write-Host $dirty; exit 1 }
$ahead = git log --oneline --no-decorate "origin/stable..origin/main"
if (-not $ahead) { Write-Host "stable is already level with main ($(git log --oneline -1 origin/main))."; exit 0 }

Write-Host ""
Write-Host "  These would go out to every install on their next update:" -ForegroundColor Green
$ahead | ForEach-Object { Write-Host "   $_" }
Write-Host ""
Write-Host "  Have you run this version on your own install?" -ForegroundColor Yellow
if (-not $Yes) {
  $answer = Read-Host "  Type RELEASE to promote, anything else to stop"
  if ($answer -ne "RELEASE") { Write-Host "Stopped; stable is unchanged."; exit 0 }
}
git push origin origin/main:refs/heads/stable; if (-not $?) { Write-Host "Push failed."; exit 1 }
Write-Host ""
Write-Host "stable is now $(git log --oneline -1 origin/main)."
Write-Host "To undo: git push origin <older-commit>:refs/heads/stable --force-with-lease"
