# Undo the Fluent visual redesign.
#
# Puts every visual file back exactly as it was before the redesign — palette, logo, favicon,
# icons, analyst, chart colours — and redeploys. Nothing else moves: the rail icons, the
# icon-only navigation, the AI Analysis engine and the model authentication fix all stay,
# because none of them is part of the design change.
#
#     pwsh .\undo-redesign.ps1
#
# It restores from the git tag `design-before`, which is the commit immediately prior to the
# redesign. Re-running it is harmless. To go forward again afterwards:
#
#     git checkout design-fluent -- web/assets app/report.py test_visuals.py
#
# and run this script's deploy step, or just deploy normally.

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

Write-Host "Restoring the pre-redesign look..." -ForegroundColor Cyan

# Only the files the redesign touched. A whole-tree revert would also undo the analysis engine
# and the auth fix, which were separate work that happens to share a commit range.
$files = @(
    "web/assets/app.css",
    "web/assets/app.js",
    "web/assets/bot.js",
    "web/assets/logo.svg",
    "web/assets/favicon.svg",
    "web/index.html",
    "web/login.html",
    "app/report.py",
    "test_visuals.py"
)

git checkout design-before -- $files
if ($LASTEXITCODE -ne 0) { throw "Could not restore from the 'design-before' tag." }

Write-Host "Files restored. Deploying..." -ForegroundColor Cyan

$zip = Join-Path $env:TEMP "cloudlens-undo.zip"
if (Test-Path $zip) { Remove-Item $zip }

python -c @"
import os, zipfile
root = os.getcwd()
out = os.path.join(os.environ['TEMP'], 'cloudlens-undo.zip')
skip = {'.venv', '__pycache__', '.pytest_cache', 'data', '.git'}
z = zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED)
n = 0
for top in ('app', 'web', 'tools'):
    for dp, dn, fn in os.walk(os.path.join(root, top)):
        dn[:] = [d for d in dn if d not in skip]
        for f in fn:
            if f.endswith(('.pyc', '.log', '.err')):
                continue
            full = os.path.join(dp, f)
            z.write(full, os.path.relpath(full, root).replace('\\', '/'))
            n += 1
for f in ('requirements.txt', 'ingest.py', 'README.md'):
    z.write(os.path.join(root, f), f)
    n += 1
z.close()
print(f'{n} files packaged')
"@

az webapp deploy `
    -g <your-resource-group> `
    -n <your-app-name> `
    --subscription 11111111-2222-3333-4444-555555555555 `
    --src-path $zip --type zip -o none

Remove-Item $zip -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Done. The previous look is back at https://<your-app-name>.azurewebsites.net" -ForegroundColor Green
Write-Host "Hard-refresh the browser (Ctrl+Shift+R) — the stylesheet is cached." -ForegroundColor Yellow
