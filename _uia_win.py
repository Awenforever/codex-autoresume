import sys, subprocess, base64
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
PS = r'''
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
    [System.Windows.Automation.Condition]::TrueCondition)
foreach ($w in $wins) {
    $proc = "?"
    try { $proc = (Get-Process -Id $w.Current.ProcessId -ErrorAction Stop).ProcessName } catch {}
    Write-Output ("WIN pid=" + $w.Current.ProcessId + " proc=" + $proc + " class=[" + $w.Current.ClassName + "] name=[" + $w.Current.Name + "]")
}
'''
ps1 = base64.b64encode(PS.encode("utf-16-le")).decode()
r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", ps1],
                   capture_output=True, timeout=120)
print(r.stdout.decode("utf-8", errors="replace")[:3000])
