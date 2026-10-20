# ═══════════════════════════════════════════════════════════════════
#  注册 Windows 任务计划程序 —— PLAN §M9 的「无人值守」落点。
#
#  用法（普通 PowerShell 窗口，不需要管理员）：
#      powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
#
#  ★ 为什么是任务计划程序而不是 APScheduler 常驻进程：
#    本项目每天只跑一次。常驻进程为了等这一次要占 24 小时的资源，
#    还要自己处理"进程死了谁重启它"—— 任务计划程序免费提供这些。
#    （选项与理由见 docs/ADR/001-deploy-windows-task-scheduler.md。）
#
#  ★ 为什么任务动作只有一行 `cmd /c run_report.cmd`：
#    工作目录 / UTF-8 / venv / 日志重定向都收在那个 .cmd 里，
#    手动跑和自动跑走【同一个入口】—— 排查时不必猜"任务里的命令和
#    我手敲的有什么区别"。
#
#  ★ MultipleInstances=IgnoreNew 是单实例锁之外的第二层：
#    任务计划程序层面就不让第二个实例启动；app.lock 兜的是"人手动跑"
#    和"任务恰好同时跑"这两条它管不到的路径。两层各治一种情况。
# ═══════════════════════════════════════════════════════════════════

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
# 每天 08:00（东八区，本机时区）。★ 时间点选在上班前：
# 若登录态过期，主人早上打开电脑就能看到退出码 3 的提示并续期，
# 而不是等到晚上才发现今天没有日报。
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
