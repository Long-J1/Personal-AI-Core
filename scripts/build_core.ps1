# 打包 Personal AI Core → desktop/build/core/（PyInstaller onedir，D012）
#
# 用法（两种构建模式）：
#   普通公开版： powershell -File scripts/build_core.ps1
#   Beta 版  ： powershell -File scripts/build_core.ps1 -Beta
#
# ## 为什么必须显式区分两种模式（别再退回旧做法）
#
# 旧逻辑是"没设 $env:PAI_BETA_CODE 就把 models/_beta_preset.py 删掉"。
# 后果是**一次疏忽就让 Beta 静默失效**：构建日志只打一行"Beta 预设未注入"，
# 安装后 UI 按钮全在、但输任何码都只回「体验码无效或当前不可用」。
# 已经栽过一次（见记忆里 0.6.0-beta.1 那个包）。
#
# 现在改成**构建模式决定产物**：
#   -Beta  → preset 必须存在；缺凭证就**直接构建失败**，不产出半个包。
#   无 -Beta → 明确生成一个"不含 Beta 能力"的 preset，PyInstaller 照收，
#             core/beta.py 的 available 永远 False，UI 入口自动隐藏。
#
# 两套包都能独立运行，互不依赖，也都不需要事后手工收拾。
param([switch]$Beta)

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

# ---- 版本号注入 -------------------------------------------------------
# 版本只从 desktop/package.json 读一次，避免 Core 与 Electron 两处各写一遍
# 然后走偏（历史上就出现过 0.5.4 / 0.5.5 不一致）。
$pkg = Get-Content 'desktop\package.json' -Raw | ConvertFrom-Json
$version = $pkg.version
$versionFile = 'core\_build_version.py'
$pyVersion = @"
# 由 scripts/build_core.ps1 生成 —— 请勿提交（.gitignore 已排除）。
# 单一来源是 desktop/package.json 的 version。
BUILD_VERSION = "$version"
BUILD_CHANNEL = "$([string]$(if ($Beta) { 'beta' } else { 'public' }))"
"@
Set-Content -Path $versionFile -Value $pyVersion -Encoding UTF8
Write-Host "构建版本：$version（$([string]$(if ($Beta) { 'Beta 渠道' } else { '公开渠道' }))）"

# ---- Beta 体验预设 ----------------------------------------------------
# 凭证只从环境变量读，脚本里不出现明文；生成物 models/_beta_preset.py
# 已 gitignore，不进仓库 / history / 官网。
#   $env:PAI_BETA_CODE      体验码明文（只用来算 SHA256，不会写进产物）
#   $env:PAI_BETA_API_KEY   Beta 专用云端 Key（**不是**作者主 Key）
#   $env:PAI_BETA_BASE_URL  可选
#   $env:PAI_BETA_MODEL     可选
$presetPath = 'models\_beta_preset.py'

# ⚠️ 无论哪种模式都**先删**：上一轮的残留绝不能被这一轮捡去用。
# 这不是"没变量就删"的那种脆弱删除，而是保证产物与本次构建一一对应。
Remove-Item $presetPath -Force -ErrorAction SilentlyContinue

$hasCode = [bool]$env:PAI_BETA_CODE
$hasKey = [bool]$env:PAI_BETA_API_KEY

if ($Beta) {
  # ---- Beta 模式：凭证不全就直接失败，绝不产出"看起来装了其实没功能"的包 ----
  if (-not ($hasCode -and $hasKey)) {
    throw @"
Beta 构建缺少凭证，已中止（不会产出残缺包）。

  PAI_BETA_CODE     = $(if($hasCode){'已设置'}else{'**缺失**'})
  PAI_BETA_API_KEY  = $(if($hasKey){'已设置'}else{'**缺失**'})

这两个是 Beta 版的一键配置入口所需。补上后重跑：
  `$env:PAI_BETA_CODE='<体验码>'; `$env:PAI_BETA_API_KEY='<Beta专用Key>'; powershell -File scripts/build_core.ps1 -Beta

要出普通公开版就去掉 -Beta 参数。
"@
  }

  # 先在 PowerShell 里算好 SHA256，生成的 Python 里**只留摘要**。
  # 不要把明文码写进去：它会被 PyInstaller 编进 exe，别人一搜就能拿到，
  # 与 docs/decisions/D020 里「预设里存的是摘要不是明文码」的承诺矛盾。
  $sha = [System.Security.Cryptography.SHA256]::Create()
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($env:PAI_BETA_CODE)
  $codeSha = ([System.BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLower()
  $sha.Dispose()

  $py = @"
# 由 scripts/build_core.ps1 -Beta 生成 —— 请勿提交（.gitignore 已排除）。
# 内容是 Beta 测试凭证，**不是安全边界**：任何人都能从安装包里提取 Key。
# 体验码**明文不在这里**（只有 SHA256 摘要），但 Key 在。
#
# 回退：普通公开版不含这个模块的能力，`beta.enabled()` 恒为 False，
# UI 入口自动隐藏，用户原有配置与数据一概不受影响。
BUILD_CHANNEL = "beta"

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
  # ---- 公开模式：明确生成"不含 Beta 能力"的 preset --------------------
  # 为什么要有这个文件（而不是完全不生成）：
  #   core/beta.py 用 __import__("models._beta_preset") 这种字符串变量形式，
  #   PyInstaller 的静态分析**看不见** → 模块不会自动进包。
  #   而"模块不在包里"和"模块在包里但说没 Beta"这两种状态，
  #   对排查的人来说是同一类难题。显式写一个 available=False 的 preset，
  #   至少让 /api/beta/status 有确定答案，而不是 import 失败。
  $py = @"
# 由 scripts/build_core.ps1（公开模式）生成 —— 请勿提交。
# 本包**不含** Beta 一键配置能力：enabled() 会因为缺 provider 键而返回 False，
# UI 的 Beta 入口自动隐藏。用户自己的模型配置与所有数据一概不受影响。
BUILD_CHANNEL = "public"

PRESET = {}
"@
  Set-Content -Path $presetPath -Value $py -Encoding UTF8
  Write-Host '公开版：Beta 能力已关闭（生成显式的空 preset，UI 入口会隐藏）'
}

$stage = 'build\pyi-dist'
$staticDir = Join-Path (Get-Location) 'interfaces\static'
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item 'desktop\build\core' -Recurse -Force -ErrorAction SilentlyContinue

# 两种模式都**必须**收这个模块（公开模式也有那个文件，见上）。
# 不能写成"文件存在才加" —— 那正是让 Beta 静默失效的那个坑。
$betaHidden = @('--hidden-import', 'models._beta_preset')
Write-Host 'PyInstaller 收 models._beta_preset（两种模式都收，公开版里它是空 preset）'

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

# 打包后自检：preset 模块必须真的在产物里。这是 Beta 曾经失效的根因，
# 光看构建日志说"已注入"不算数——那正是当时骗过人的地方。
$xref = Get-ChildItem 'build\pyi-work' -Recurse -Filter 'xref-*.html' -EA SilentlyContinue |
        Select-Object -First 1
if ($xref) {
  $html = Get-Content $xref.FullName -Raw
  $inPack = $html.Contains('models._beta_preset')
  Write-Host ("产物自检：models._beta_preset {0}" -f $(if ($inPack) { '在包内 [OK]' } else { '*** 不在包内 ***' }))
  if (-not $inPack) { throw '产物缺少 models._beta_preset —— Beta 会静默失效，中止' }
} else {
  Write-Host '警告：找不到 PyInstaller 交叉引用报告，跳过产物自检'
}

$sizeMB = [math]::Round((Get-ChildItem 'desktop\build\core' -Recurse | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "OK: $exe（整体 $sizeMB MB，渠道 $($pyVersion -match 'beta'))"