# Launcher for the desktop shortcut: pick a recent chat and continue it on free models.
python (Join-Path $PSScriptRoot 'free-resume.py')
if ($LASTEXITCODE -ne 0) { Read-Host 'Press Enter to close' | Out-Null }
