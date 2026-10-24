import sys, subprocess
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PS = r'''
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
    New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Window))
foreach ($w in $wins) {
    $proc = ""
    try { $proc = (Get-Process -Id $w.Current.ProcessId -ErrorAction Stop).ProcessName } catch {}
    if ($proc -notin @("ChatGPT","codex")) { continue }
    Write-Output ("WINDOW name=[" + $w.Current.Name + "] pid=" + $w.Current.ProcessId + " proc=" + $proc + " class=[" + $w.Current.ClassName + "]")
    # walk subtree, dump buttons and their ancestry, depth-limited BFS
    $stack = New-Object System.Collections.Stack
    $stack.Push(@{el=$w; depth=0; path=""})
    while ($stack.Count -gt 0) {
        $node = $stack.Pop()
        if ($node.depth -ge 25) { continue }
        $kids = $node.el.FindAll([System.Windows.Automation.TreeScope]::Children,
            [System.Windows.Automation.Condition]::TrueCondition)
        foreach ($k in $kids) {
            $ct = $k.Current.ControlType.ProgrammaticName -replace "ControlType.",""
            $nm = $k.Current.Name
            $cls = $k.Current.ClassName
            $path = $node.path + "/" + $ct + ($nm ? "[" + $nm + "]" : "")
            if ($ct -in @("Button","ToggleButton") -or ($nm -match "continue|Continue|继续|Resume|恢复|goal|Goal|目标")) {
                if ($nm -or $ct -eq "Button") {
                    Write-Output ("  " + "  " * $node.depth + $ct + " name=[" + $nm + "] class=[" + $cls + "] aid=[" + $k.Current.AutomationId + "]")
                }
            }
            if ($ct -notin @("Button","ToggleButton","Text","Edit","Custom") -or $node.depth -lt 6) {
                $stack.Push(@{el=$k; depth=$node.depth + 1; path=$path})
            }
        }
    }
}
'''
r = subprocess.run(["powershell", "-NoProfile", "-Command", PS],
                   capture_output=True, text=True, timeout=120,
                   encoding="utf-8", errors="replace")
print(r.stdout[:6000])
if r.returncode != 0:
    print("ERR:", r.stderr[:800])
