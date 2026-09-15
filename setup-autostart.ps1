# Run this as Administrator to auto-start ModelLink on boot
# This creates a Windows Scheduled Task

$action = New-ScheduledTaskAction -Execute "C:\Modellink\start-hidden.bat" -WorkingDirectory "C:\Modellink"
$trigger = New-ScheduledTaskTrigger -AtLogon
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName "ModelLink Server" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Auto-starts ModelLink API and Cloudflare Tunnel on login"

Write-Host "ModelLink will now auto-start when you log in!" -ForegroundColor Green
Write-Host "To remove: Unregister-ScheduledTask -TaskName 'ModelLink Server'" -ForegroundColor Yellow
