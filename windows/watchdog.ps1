# MeshScore watchdog: every 15 minutes, run `meshscore health --notify --quiet`.
# Silent when nothing changed; when the bot breaks (or recovers) it sends the phone / chat alert configured under
# [alerts] in meshscore.toml and also shows a Windows notification. See docs/running.md.

$dir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $dir
$py = Join-Path $dir ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$out = (& $py -m meshscore health --notify --quiet 2>&1 | Out-String).Trim()
if ($out) {
    try {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $t = $xml.GetElementsByTagName("text")
        $t.Item(0).AppendChild($xml.CreateTextNode("MeshScore")) | Out-Null
        $t.Item(1).AppendChild($xml.CreateTextNode($out)) | Out-Null
        $app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
    } catch { }
}
