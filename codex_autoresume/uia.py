"""UI-automation fallback for resuming goals the app holds the writer on.

When the desktop app owns a goal thread, every external RPC
(thread/goal/set, thread/resume, thread/queue/start) is rejected with
"already has an active writer". The only safe remaining move is the app's
own Continue button, so this module finds that button in the app's UI tree
(WebView2 exposes accessible names through UI Automation) and invokes it —
exactly what a human click does, no protocol shortcuts.

Must run inside the interactive user session (the daemon's scheduled task
does). PowerShell + System.Windows.Automation keeps this dependency-free.
"""

from __future__ import annotations

import base64
import json
import subprocess

# candidate accessible names for the goal bar's continue control, zh + en
CONTINUE_NAMES = ["继续", "Continue", "恢复", "Resume"]

_PROBE_PS = r'''
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$names = __NAMES__
$conds = @()
foreach ($n in $names) {
    $conds += New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, $n)
}
$nameCond = $conds[0]
if ($conds.Count -gt 1) { $nameCond = New-Object System.Windows.Automation.OrCondition($conds) }
$btnCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Button)
$target = New-Object System.Windows.Automation.AndCondition($nameCond, $btnCond)
$root = [System.Windows.Automation.AutomationElement]::RootElement
$found = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $target)
$res = @()
foreach ($e in $found) {
    $pattern = $null
    $invokable = $false
    try {
        $pattern = $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $invokable = $true
    } catch {}
    $res += @{ name = $e.Current.Name
               pid  = $e.Current.ProcessId
               class = $e.Current.ClassName
               offscreen = $e.Current.IsOffscreen
               invokable = $invokable }
}
$res | ConvertTo-Json -Compress -Depth 3
'''


def _run_ps(script: str, timeout: int = 60) -> str:
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", enc],
                       capture_output=True, timeout=timeout)
    return (r.stdout or b"").decode("utf-8", "replace").strip()


def find_continue_buttons(names: list[str] | None = None) -> list[dict]:
    """Search every desktop window for continue-style buttons (read-only)."""
    ps_names = "@(" + ",".join('"%s"' % n for n in (names or CONTINUE_NAMES)) + ")"
    script = _PROBE_PS.replace("__NAMES__", ps_names)
    out = _run_ps(script)
    try:
        data = json.loads(out)
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


def click_continue(names: list[str] | None = None,
                   require_visible: bool = True) -> dict:
    """Invoke the first clickable continue button. Returns what happened."""
    click_ps = r'''
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$names = __NAMES__
$conds = @()
foreach ($n in $names) {
    $conds += New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, $n)
}
$nameCond = $conds[0]
if ($conds.Count -gt 1) { $nameCond = New-Object System.Windows.Automation.OrCondition($conds) }
$btnCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Button)
$target = New-Object System.Windows.Automation.AndCondition($nameCond, $btnCond)
$root = [System.Windows.Automation.AutomationElement]::RootElement
$found = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $target)
$clicked = $false
$detail = ""
foreach ($e in $found) {
    if (__REQUIRE_VISIBLE__ -and $e.Current.IsOffscreen) { continue }
    try {
        $p = $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $p.Invoke()
        $clicked = $true
        $detail = ("pid=" + $e.Current.ProcessId + " name=[" + $e.Current.Name + "]")
        break
    } catch {
        $detail = "invoke failed: " + $_.Exception.Message
    }
}
@{ clicked = $clicked; detail = $detail } | ConvertTo-Json -Compress
'''
    ps_names = "@(" + ",".join('"%s"' % n for n in (names or CONTINUE_NAMES)) + ")"
    script = click_ps.replace("__NAMES__", ps_names)
    script = script.replace("__REQUIRE_VISIBLE__",
                            "$true" if require_visible else "$false")
    out = _run_ps(script, timeout=90)
    try:
        return json.loads(out)
    except Exception:
        return {"clicked": False, "detail": "no parseable result: %s" % out[:200]}


_DEEP_PS = r"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
    [System.Windows.Automation.Condition]::TrueCondition)
$res = @()
foreach ($w in $wins) {
    $proc = "?"
    try { $proc = (Get-Process -Id $w.Current.ProcessId -ErrorAction Stop).ProcessName } catch {}
    if ($proc -ne "ChatGPT" -and $proc -ne "codex") { continue }
    $stack = New-Object System.Collections.Stack
    $stack.Push(@{el=$w; path=""})
    while ($stack.Count -gt 0) {
        $node = $stack.Pop()
        $kids = $node.el.FindAll([System.Windows.Automation.TreeScope]::Children,
            [System.Windows.Automation.Condition]::TrueCondition)
        foreach ($k in $kids) {
            $ct = $k.Current.ControlType.ProgrammaticName -replace "ControlType.",""
            $nm = $k.Current.Name
            $path = $node.path + "/" + $ct + ":" + $nm
            if ($ct -eq "Button" -and ($nm -match "Resume|Continue|继续|恢复|Start|开始|Retry|重试")) {
                $res += @{ kind="btn"; name=$nm; path=$path }
            }
            if ($nm -and ($nm -match "BLOCKED|blocked|受限|capacity|LIMITED|limit")) {
                $res += @{ kind="txt"; name=$nm; path=$path }
            }
            $stack.Push(@{el=$k; path=$path})
        }
    }
}
$res | ConvertTo-Json -Compress -Depth 3
"""


def probe_detailed() -> list[dict]:
    """Buttons and status texts with ancestry paths (read-only diagnostics)."""
    out = _run_ps(_DEEP_PS, timeout=180)
    try:
        data = json.loads(out)
        return data if isinstance(data, list) else [data]
    except Exception:
        return []
