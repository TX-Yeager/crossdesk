# Isolated firewall policy tests: no administrator request or OS rule changes.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot '../installer/windows/Configure-Firewall.ps1')
function Assert($Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
$plans = @(Get-CrossDeskFirewallPlan 'C:\Users\Test User\Apps\CrossDesk')
Assert ($plans.Count -eq 2) 'Exactly two rules'
Assert ($plans[0].Protocol -eq 'TCP' -and $plans[0].LocalPort -eq 48193) 'Control port'
Assert ($plans[1].Protocol -eq 'UDP' -and $plans[1].LocalPort -eq 48194) 'Discovery port'
foreach ($plan in $plans) {
    Assert ($plan.Profile -eq 'Private') 'Never Public/Any'
    Assert ($plan.RemoteAddress -eq 'LocalSubnet') 'Same subnet only'
    Assert ($plan.Direction -eq 'Inbound' -and $plan.Action -eq 'Allow') 'Inbound only'
    Assert ($plan.Program -eq 'C:\Users\Test User\Apps\CrossDesk\CrossDesk.exe') 'Bound to exact host'
}
$other = @(Get-CrossDeskFirewallPlan 'C:\Other\CrossDesk')
Assert ($plans[0].Name -ne $other[0].Name) 'Installations have distinct rule names'
$casing = @(Get-CrossDeskFirewallPlan 'c:\users\test user\apps\crossdesk')
Assert ($plans[0].Name -eq $casing[0].Name) 'Case-insensitive Windows path identity'

$script:rules = @{}
$script:added = @()
$script:removed = @()
function Get-NetFirewallRule { param($PolicyStore, $Name, $ErrorAction) if ($script:rules.ContainsKey($Name)) { $script:rules[$Name] } }
function Get-NetFirewallApplicationFilter { param([Parameter(ValueFromPipeline=$true)]$InputObject) process { [pscustomobject]@{ Program=$InputObject.Program } } }
function New-NetFirewallRule {
    param($Name,$DisplayName,$Program,$Direction,$Action,$Profile,$RemoteAddress,$Protocol,$LocalPort,$Group,$PolicyStore,$Enabled,$EdgeTraversalPolicy)
    $copy = [pscustomobject]@{ Name=$Name; Program=$Program; Group=$Group; Profile=$Profile; RemoteAddress=$RemoteAddress }
    Assert ($PolicyStore -eq 'PersistentStore' -and $Enabled -eq 'True' -and $EdgeTraversalPolicy -eq 'Block') 'Safe persistent scope'
    $script:rules[$Name]=$copy
    $script:added += $copy
}
function Remove-NetFirewallRule { param($PolicyStore,$Name) $script:removed += $Name; $script:rules.Remove($Name) }
Set-CrossDeskFirewall $plans 'Install'
Assert ($script:rules.Count -eq 2) 'Install both rules'
Set-CrossDeskFirewall $plans 'Install'
Assert ($script:rules.Count -eq 2) 'Repeat setup remains idempotent'
Set-CrossDeskFirewall $plans 'Remove'
Assert ($script:rules.Count -eq 0) 'Uninstall removes owned rules'
$script:rules[$plans[1].Name] = [pscustomobject]@{ Name=$plans[1].Name; Group='Unrelated'; Program='C:\Other.exe' }
$before = $script:added.Count
$rejected = $false
try { Set-CrossDeskFirewall $plans 'Install' } catch { $rejected = $true }
Assert $rejected 'Conflicting rule rejected'
Assert ($script:added.Count -eq $before) 'No mutation before full ownership validation'
Assert ($script:rules.Count -eq 1) 'Unrelated rule preserved'
Write-Output 'PASS: firewall scope, stable identity, idempotent install/removal, unrelated-rule protection (mocked OS calls).'
