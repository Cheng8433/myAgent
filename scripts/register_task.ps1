# 注册 Windows 任务计划程序：每天 08:00 运行 scripts\run_report.cmd。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1

$ErrorActionPreference = "Stop"

$TaskName = "myAgent Daily Report"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $repo "scripts\run_report.cmd"
$python = Join-Path $repo ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "找不到 $python —— 请先按 README 建好 .venv 并安装依赖。"
}
if (-not (Test-Path $runner)) {
    throw "找不到 $runner —— 仓库不完整？"
}

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$runner`""
# 每天 08:00（本机时区，东八区）。选在上班前，登录过期能及早发现。
$trigger = New-ScheduledTaskTrigger -Daily -At "08:00"
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "招聘信息日报 Agent：每天 08:00 跑一次 report（入口 scripts\run_report.cmd）" `
    -Force | Out-Null

Write-Host "已注册任务：$TaskName"
Write-Host "  每天 08:00，入口：$runner"
Write-Host "  只读查看：schtasks /Query /TN `"$TaskName`" /V /FO LIST"
Write-Host "  手动触发：schtasks /Run /TN `"$TaskName`""
Write-Host "  卸载：powershell -ExecutionPolicy Bypass -File scripts\unregister_task.ps1"
Write-Host ""
Write-Host "注意：任务以当前用户身份运行，默认只在【用户已登录】时执行；"
Write-Host "      关机/休眠错过 08:00 时，StartWhenAvailable 会在下次开机后补跑一次。"
