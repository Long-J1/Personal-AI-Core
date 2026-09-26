# 打包 Personal AI Core → desktop/build/core/（PyInstaller onedir，D012）
# 用法：powershell -File scripts/build_core.ps1
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

$stage = 'build\pyi-dist'
$staticDir = Join-Path (Get-Location) 'interfaces\static'
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item 'desktop\build\core' -Recurse -Force -ErrorAction SilentlyContinue

python -m PyInstaller --noconfirm --clean --onedir `
  --name personal-ai-core `
  --add-data "$staticDir;interfaces/static" `
  --collect-submodules uvicorn `
  --hidden-import interfaces.webapp `
  --exclude-module cv2 `
  --exclude-module tkinter `
  --distpath $stage `
  --workpath build\pyi-work `
  --specpath build\pyi `
  serve.py

if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（$LASTEXITCODE）" }

# onedir 产物整体挪进 desktop/build/core（exe 与 _internal 平级）
New-Item -ItemType Directory -Force -Path 'desktop\build' | Out-Null
Copy-Item -Recurse -Force (Join-Path $stage 'personal-ai-core') 'desktop\build\core'

$exe = 'desktop\build\core\personal-ai-core.exe'
if (-not (Test-Path $exe)) { throw "产物缺失：$exe" }
$sizeMB = [math]::Round((Get-ChildItem 'desktop\build\core' -Recurse | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "OK: $exe（整体 $sizeMB MB）"
