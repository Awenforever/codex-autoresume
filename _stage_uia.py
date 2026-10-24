import sys, base64
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PS = r'''
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$out = "C:\Users\king_\cxr_uia_out.txt"
$lines = @()
$root = [System.Windows.Automation.AutomationElement]::RootElement
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
    [System.Windows.Automation.Condition]::TrueCondition)
foreach ($w in $wins) {
    $proc = "?"
    try { $proc = (Get-Process -Id $w.Current.ProcessId -ErrorAction Stop).ProcessName } catch {}
    if ($proc -ne "ChatGPT" -and $proc -ne "codex") { continue }
    $lines += ("WINDOW=[" + $w.Current.Name + "] pid=" + $w.Current.ProcessId)
    $stack = New-Object System.Collections.Stack
    $stack.Push(@{el=$w; depth=0})
    $count = 0
    while ($stack.Count -gt 0 -and $count -lt 6000) {
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
                $lines += ("BTN d" + $node.depth + " [" + $nm + "] aid=[" + $aid + "]")
            } elseif ($nm -and ($nm -match "Continue|Resume|继续|恢复|[Gg]oal|目标|受限|limit")) {
                $lines += ("ELM d" + $node.depth + " " + $ct + " [" + $nm + "]")
            }
            $stack.Push(@{el=$k; depth=($node.depth + 1)})
        }
    }
    $lines += ("scanned=" + $count)
}
if ($lines.Count -eq 0) { $lines += "NO_CODEX_WINDOW" }
$lines | Out-File -FilePath $out -Encoding utf8
'''

b64 = base64.b64encode(PS.encode("utf-16-le")).decode()
open(r"C:\Users\king_\cxr_uia_probe.ps1", "w", encoding="utf-8").write(
    "powershell -NoProfile -EncodedCommand " + b64 + "\n")
print("probe staged (cmd wrapper with encoded payload)")
