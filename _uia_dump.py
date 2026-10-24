import sys, subprocess, base64
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PS = r'''
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Window)
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
foreach ($w in $wins) {
    $proc = ""
    try { $proc = (Get-Process -Id $w.Current.ProcessId -ErrorAction Stop).ProcessName } catch {}
    if ($proc -ne "ChatGPT" -and $proc -ne "codex") { continue }
    Write-Output ("WINDOW=[" + $w.Current.Name + "] pid=" + $w.Current.ProcessId + " class=[" + $w.Current.ClassName + "]")
    $stack = New-Object System.Collections.Stack
    $stack.Push(@{el=$w; depth=0})
    $count = 0
    while ($stack.Count -gt 0 -and $count -lt 4000) {
        $node = $stack.Pop()
        if ($node.depth -ge 30) { continue }
        $kids = $node.el.FindAll([System.Windows.Automation.TreeScope]::Children,
            [System.Windows.Automation.Condition]::TrueCondition)
        foreach ($k in $kids) {
            $count++
            $ct = $k.Current.ControlType.ProgrammaticName -replace "ControlType.",""
            $nm = $k.Current.Name
            $aid = $k.Current.AutomationId
            if ($ct -eq "Button" -or $ct -eq "ToggleButton") {
                $line = "BTN d" + $node.depth + " [" + $nm + "] aid=[" + $aid + "]"
                Write-Output $line
            } elseif ($nm -and ($nm -match "Continue|Resume|继续|恢复|goal|Goal|目标|limit|受限")) {
                Write-Output ("ELM d" + $node.depth + " " + $ct + " [" + $nm + "]")
            }
            $stack.Push(@{el=$k; depth=($node.depth + 1)})
        }
    }
    Write-Output ("scanned=" + $count)
}
'''

ps1 = base64.b64encode(PS.encode("utf-16-le")).decode()
r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", ps1],
                   capture_output=True, timeout=180)
out = r.stdout.decode("utf-8", errors="replace")
err = r.stderr.decode("utf-8", errors="replace")[:400]
lines = out.splitlines()
btn = [l for l in lines if l.startswith("BTN") or l.startswith("ELM") or l.startswith("WINDOW") or l.startswith("scanned")]
print("\n".join(btn[:120]))
if r.returncode != 0 or not btn:
    print("RC", r.returncode, "ERR", err)
