# MeshScore daily check: make a backup, run `meshscore health`, and pop up a Windows notification if something
# needs a hand. Registered as the "MeshScore daily check" scheduled task (see docs/running.md); safe to run by hand:
#
#   powershell -ExecutionPolicy Bypass -File windows\daily-check.ps1
#
# Writes health-latest.txt (the full report) and appends one line to health-history.log.

$ErrorActionPreference = "Continue"
$dir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)     # the meshscore folder
Set-Location $dir
$py = Join-Path $dir ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

& $py -m meshscore backup | Out-Null
$report = & $py -m meshscore health 2>&1 | Out-String
$code = $LASTEXITCODE
$summary = (& $py -m meshscore health --summary 2>&1 | Out-String).Trim()

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
Set-Content -Path (Join-Path $dir "health-latest.txt") -Value "MeshScore health $stamp`r`n`r`n$report" -Encoding UTF8
Add-Content -Path (Join-Path $dir "health-history.log") -Value "$stamp  $(if ($code -eq 0) { 'OK  ' } else { 'FAIL' })  $summary" -Encoding UTF8

function Show-Toast([string]$title, [string]$text) {
    try {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $t = $xml.GetElementsByTagName("text")
        $t.Item(0).AppendChild($xml.CreateTextNode($title)) | Out-Null
        $t.Item(1).AppendChild($xml.CreateTextNode($text)) | Out-Null
        $app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
    } catch {
        # no notification support: the report files are still written
    }
}

if ($code -ne 0) {
    Show-Toast "MeshScore needs a hand" $summary
}
exit $code
