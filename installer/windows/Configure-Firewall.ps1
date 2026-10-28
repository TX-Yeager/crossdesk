param(
    [ValidateSet('Install', 'Remove', 'Plan')][string]$Action = 'Install',
    [switch]$Elevated
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Replaced by the packager from the verified, frozen Windows archive.
$ExpectedHostHash = '@HOST_SHA256@'
$RuleGroup = 'CrossDesk Installer LAN'

function Get-CrossDeskFirewallPlan([string]$InstallDirectory) {
    $directory = [IO.Path]::GetFullPath($InstallDirectory)
    if ($directory.StartsWith('\\')) { throw 'Install CrossDesk on a local drive.' }
    $program = Join-Path $directory 'CrossDesk.exe'
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($program.ToLowerInvariant()))
        $id = ([BitConverter]::ToString($hash)).Replace('-', '').Substring(0, 20)
    } finally { $sha.Dispose() }
    @(
        [pscustomobject]@{ Name="CrossDesk-$id-TCP"; DisplayName='CrossDesk - trusted LAN control';
            Program=$program; Direction='Inbound'; Action='Allow'; Profile='Private';
            RemoteAddress='LocalSubnet'; Protocol='TCP'; LocalPort=48193; Group=$RuleGroup },
        [pscustomobject]@{ Name="CrossDesk-$id-UDP"; DisplayName='CrossDesk - trusted LAN discovery';
            Program=$program; Direction='Inbound'; Action='Allow'; Profile='Private';
            RemoteAddress='LocalSubnet'; Protocol='UDP'; LocalPort=48194; Group=$RuleGroup }
    )
}

function Assert-CrossDeskRuleOwnership($Rule, $Plan) {
    $filter = $Rule | Get-NetFirewallApplicationFilter
    if ($Rule.Group -ne $RuleGroup -or $filter.Program -ine $Plan.Program) {
        throw 'A conflicting firewall rule exists; no unrelated rule will be changed.'
    }
}

function Set-CrossDeskFirewall($Plans, [string]$Operation) {
    # Validate all existing names before changing any rules.
    foreach ($plan in $Plans) {
        $existing = @(Get-NetFirewallRule -PolicyStore PersistentStore -Name $plan.Name -ErrorAction SilentlyContinue)
        foreach ($rule in $existing) { Assert-CrossDeskRuleOwnership $rule $plan }
    }
    foreach ($plan in $Plans) {
        $existing = @(Get-NetFirewallRule -PolicyStore PersistentStore -Name $plan.Name -ErrorAction SilentlyContinue)
        if ($existing.Count) {
            Remove-NetFirewallRule -PolicyStore PersistentStore -Name $plan.Name
        }
        if ($Operation -eq 'Install') {
            $parameters = @{}
            foreach ($property in $plan.PSObject.Properties) { $parameters[$property.Name] = $property.Value }
            New-NetFirewallRule @parameters -PolicyStore PersistentStore -Enabled True -EdgeTraversalPolicy Block | Out-Null
        }
    }
}

# Dot sourcing exposes the rule planner for isolated tests, without elevation or OS changes.
if ($MyInvocation.InvocationName -eq '.') { return }

try {
    $plans = @(Get-CrossDeskFirewallPlan $PSScriptRoot)
    if ($Action -eq 'Plan') { $plans | ConvertTo-Json -Depth 4; exit 0 }
    if ($Action -eq 'Install') {
        $actualHash = (Get-FileHash -LiteralPath $plans[0].Program -Algorithm SHA256).Hash
        if ($actualHash -ine $ExpectedHostHash) { throw 'CrossDesk.exe integrity check failed.' }
    }
    if (-not $Elevated) {
        # A separate process raises UAC only for these two app-specific rules.
        $arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Action {1} -Elevated' -f $PSCommandPath, $Action
        try {
            $process = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -Wait -PassThru
            exit $process.ExitCode
        } catch {
            $cause = $_.Exception
            while ($null -ne $cause) {
                if ($cause -is [ComponentModel.Win32Exception] -and $cause.NativeErrorCode -eq 1223) { exit 2 }
                $cause = $cause.InnerException
            }
            if ($_.CategoryInfo.Reason -eq 'OperationCanceledException') {
                exit 2
            }
            throw
        }
    }
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Administrator approval is required for firewall configuration.'
    }
    Import-Module NetSecurity
    Set-CrossDeskFirewall $plans $Action
    if ($Action -eq 'Install') {
        # Do not change a network's trust category on the user's behalf.
        $privateNetworks = @(Get-NetConnectionProfile | Where-Object NetworkCategory -eq 'Private')
        if (-not $privateNetworks.Count) {
            'Rules installed. No active Private network: see INSTALL.html.' | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'firewall-result.txt') -Encoding UTF8
            exit 3
        }
    }
    "$Action completed." | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'firewall-result.txt') -Encoding UTF8
    exit 0
} catch {
    try { $_.Exception.Message | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'firewall-result.txt') -Encoding UTF8 } catch { }
    exit 1
}
