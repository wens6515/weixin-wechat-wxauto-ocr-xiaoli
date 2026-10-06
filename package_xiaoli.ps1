# 小漓 一键打包
# 默认【重置测试区】：PyInstaller 重建 dist\小漓 后不恢复运行时数据，
# 打包产物为纯净状态（exe + _internal + 壁纸 + fonts + 表情包），首次启动
# 走全新安装流程，角色卡使用最新模板。config.json / memory.json / cards /
# wxauto / memory_deep 会被一并清掉；打包成功后还会清空用户目录（%USERPROFILE%\小漓）
# 的 usage.jsonl / reminders.json——重置 = 彻底全新，上一轮测试的用量
# 统计与定时提醒不带入。需要保留运行时数据时加 -KeepRuntime 参数：
#   powershell -ExecutionPolicy Bypass -File .\package_xiaoli.ps1 -KeepRuntime
param([switch]$KeepRuntime)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$dist   = Join-Path $root 'dist\小漓'
$backup = Join-Path $root 'dist_runtime_backup'
$venvPy = Join-Path $root 'xiaoli_desktop\.venv\Scripts\python.exe'
$spec   = Join-Path $root '小漓.spec'
$runtimeItems = @('config.json','memory.json','cards','wxauto','memory_deep')

Write-Host "[0] 仓库根: $root"
if (-not (Test-Path $venvPy)) { throw "找不到虚拟环境 python: $venvPy（请确认 xiaoli_desktop\.venv 存在）" }
if (-not (Test-Path $spec))   { throw "找不到 spec: $spec" }

if ($KeepRuntime) {
  Write-Host "[1/4] 备份运行时数据 -> $backup"
  if (Test-Path $backup) { Remove-Item $backup -Recurse -Force }
  New-Item -ItemType Directory -Path $backup | Out-Null
  foreach ($it in $runtimeItems) {
    $src = Join-Path $dist $it
    if (Test-Path $src) { Copy-Item $src (Join-Path $backup $it) -Recurse -Force; Write-Host "      备份 $it" }
  }
} else {
  Write-Host "[1/4] 重置模式：不备份/不恢复运行时数据，打包后测试区为纯净状态"
  foreach ($it in $runtimeItems) {
    if (Test-Path (Join-Path $dist $it)) { Write-Host "      将清除 $it" }
  }
}

Write-Host "[1.5/4] 预检：小漓/天枢是否占用测试区（运行中的 exe 锁 DLL 会让重建失败，"
Write-Host "        且会删掉运行中程序的文件——历史事故：certifi 被删导致 TLS 全断）..."
$xiaoli = Get-Process -Name '小漓' -ErrorAction SilentlyContinue
if (-not $xiaoli) {
  # 中文进程名在部分控制台代码页下匹配失败（GBK 乱码盲区）——按可执行路径
  # 再兜一道：测试区 dist\小漓 下的 exe 无论进程名被读成什么都能命中
  $pattern = Join-Path $dist '*.exe'
  $xiaoli = Get-CimInstance Win32_Process |
    Where-Object { $_.ExecutablePath -like $pattern }
}
if ($xiaoli) {
  $ids = @($xiaoli | ForEach-Object { if ($_.Id) { $_.Id } else { $_.ProcessId } })
  throw ("检测到小漓.exe 正在运行（PID: " + ($ids -join ', ') +
         "）——请先关闭测试区程序再打包")
}
$tianshu = Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
  Where-Object { $_.CommandLine -match 'tianshu' }
if ($tianshu) {
  throw ("检测到天枢 CLI 正在运行（PID: " + ($tianshu.ProcessId -join ', ') +
         "），其数据库锁会让 dist\小漓 重建失败——请先关闭天枢 CLI 再打包")
}

Write-Host "[2/4] PyInstaller 打包（--noconfirm 会重建 dist\小漓）..."
& $venvPy -m PyInstaller $spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败，退出码 $LASTEXITCODE" }

Write-Host "[3/4] 拷贝 壁纸 / fonts / 表情包（PyInstaller 不打包这三个目录）"
Copy-Item (Join-Path $root '壁纸')  (Join-Path $dist '壁纸')  -Recurse -Force
Copy-Item (Join-Path $root 'fonts') (Join-Path $dist 'fonts') -Recurse -Force
Copy-Item (Join-Path $root '表情包') (Join-Path $dist '表情包') -Recurse -Force

if ($KeepRuntime) {
  Write-Host "[4/4] 恢复运行时数据"
  foreach ($it in $runtimeItems) {
    $b = Join-Path $backup $it
    if (Test-Path $b) { Copy-Item $b (Join-Path $dist $it) -Recurse -Force; Write-Host "      恢复 $it" }
  }
} else {
  Write-Host "[4/4] 重置完成：dist\小漓 无运行时数据，首次启动走全新安装（新角色卡模板生效）"
  # 用户目录数据一并清空（重置 = 彻底全新）：用量统计 / 定时提醒存在
  # %USERPROFILE%\小漓（不在测试区程序目录），不清会带上一次测试的残留。
  # 仅在打包成功后执行——构建失败不破坏现有数据
  $userData = Join-Path $env:USERPROFILE '小漓'
  foreach ($f in @('usage.jsonl', 'reminders.json')) {
    $p = Join-Path $userData $f
    if (Test-Path $p) { Remove-Item $p -Force; Write-Host "      已清空用户目录 $f" }
  }
}

Write-Host ""
Write-Host "完成！产物： $dist\小漓.exe" -ForegroundColor Green
if ($KeepRuntime) {
  Write-Host "提示：直接运行该 exe 实机测试；运行时数据已保留。"
} else {
  Write-Host "提示：测试区已重置——首启需重新填 API Key / 选模型（配置可从 dist_runtime_backup 找回）。"
}
