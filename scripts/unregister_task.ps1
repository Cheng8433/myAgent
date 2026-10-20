# 卸载任务计划程序里的日报任务（回滚/停用用），不删除任何数据。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\unregister_task.ps1

$ErrorActionPreference = "Stop"
$TaskName = "myAgent Daily Report"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $existing) {
    Write-Host "任务 $TaskName 本来就不存在，无需卸载。"
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "已卸载任务：$TaskName"
Write-Host "数据没有动：var/、.env、var/auth/ 都还在。"
