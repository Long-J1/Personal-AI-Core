# 打包 Personal AI Core → desktop/build/core/（PyInstaller onedir，D012）
# 用法：powershell -File scripts/build_core.ps1
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

# ---- Beta 体验预设（构建时注入）-------------------------------------
# 凭证只从环境变量读，脚本里不出现明文；生成物 models/_beta_preset.py
# 已 gitignore，不进仓库 / history / 官网。
#   $env:PAI_BETA_CODE      体验码明文（用于算 SHA256）
#   $env:PAI_BETA_API_KEY   Beta 预设云端 Key
#   $env:PAI_BETA_BASE_URL  可选，默认智谱
#   $env:PAI_BETA_MODEL     可选，默认 glm-4-flash
$presetPath = 'models\_beta_preset.py'
Remove-Item $presetPath -Force -ErrorAction SilentlyContinue
if ($env:PAI_BETA_CODE -and $env:PAI_BETA_API_KEY) {
  # 先在 PowerShell 里算好体验码的 SHA256，生成的 Python 里**只留摘要**。
  # 不要把明文代码写进去：那样它会被 PyInstaller 编进 exe，
  # 别人从安装包里一搜就能拿到码明文 —— 与 docs/decisions/D020
  # 里「预设里存的是摘要不是明文码」的承诺相矛盾。
  $sha = [System.Security.Cryptography.SHA256]::Create()
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($env:PAI_BETA_CODE)
  $codeSha = ([System.BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLower()
  $sha.Dispose()

  $py = @"
# 由 scripts/build_core.ps1 在构建时生成 —— 请勿提交（.gitignore 已排除）。
# 内容是短期 Beta 测试凭证，不是安全边界：任何人都能从安装包里提取 Key。
# 体验码**明文不在这里**（只有 SHA256 摘要），但 Key 在。
import hashlib

PRESET = {
    "provider": "openai",
    "base_url": "$($env:PAI_BETA_BASE_URL)",
    "model": "$($env:PAI_BETA_MODEL)",
    "api_key": "$env:PAI_BETA_API_KEY",
    "code_sha256": ["$codeSha"],
}
"@
  Set-Content -Path $presetPath -Value $py -Encoding UTF8
  Write-Host 'Beta 预设已注入（体验码仅以 SHA256 形式写入，凭证不进仓库）'
} else {
  Write-Host 'Beta 预设未注入（未设 PAI_BETA_CODE / PAI_BETA_API_KEY）'
}

$stage = 'build\pyi-dist'
$staticDir = Join-Path (Get-Location) 'interfaces\static'
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item 'desktop\build\core' -Recurse -Force -ErrorAction SilentlyContinue

# Beta 预设模块必须显式收进包：core/beta.py 用的是 __import__("models._beta_preset")
# 这种**字符串变量**形式的动态导入，PyInstaller 静态分析看不见它，
# 结果就是 UI 按钮都在、但 /api/beta/status 永远 available=False。
# 只能按需加：文件不存在时（没设 PAI_BETA_* 的普通构建）加了会报 module not found。
$betaHidden = @()
if (Test-Path $presetPath) {
  $betaHidden = @('--hidden-import', 'models._beta_preset')
  Write-Host '检测到 Beta 预设，PyInstaller 加收 models._beta_preset'
} else {
  Write-Host '无 Beta 预设，跳过 hidden-import（该版本不含 Beta 体验入口）'
}

python -m PyInstaller --noconfirm --clean --onedir `
  --name personal-ai-core `
  --add-data "$staticDir;interfaces/static" `
  --collect-submodules uvicorn `
  --hidden-import interfaces.webapp `
  $betaHidden `
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
