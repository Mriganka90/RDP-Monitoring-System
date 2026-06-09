# ============================================================
#  RDP Session Collector  |  rdp_collector.ps1
#  Run on EACH target machine (the ones being remoted INTO)
#  Polls Windows Event Logs and POSTs events to FastAPI backend
# ============================================================
#
#  USAGE:
#    .\rdp_collector.ps1 -BackendUrl "http://192.168.24.10:8000" -PollIntervalSec 10
#
#  To run as a scheduled task silently (every 1 min):
#    schtasks /create /tn "RDP-Collector" /tr "powershell -NonInteractive -File C:\rdp_collector.ps1 -BackendUrl http://192.168.24.10:8000" /sc minute /mo 1 /ru SYSTEM
#
# ============================================================

param(
    [string]$BackendUrl     = "http://192.168.24.10:8000",  # <-- Your server IP
    [int]   $PollIntervalSec = 10,                           # How often to poll logs
    [int]   $LookbackMinutes = 2,                            # How far back to look on each poll
    [switch]$RunOnce                                         # Use with Task Scheduler
)

# ── Config ────────────────────────────────────────────────────────────────────
$ApiEndpoint  = "$BackendUrl/api/event"
$TargetIp     = (Get-NetIPAddress -AddressFamily IPv4 |
                    Where-Object { $_.PrefixOrigin -eq 'Dhcp' -or $_.PrefixOrigin -eq 'Manual' } |
                    Where-Object { $_.IPAddress -notmatch '^127\.' } |
                    Select-Object -First 1).IPAddress
$TargetHost   = $env:COMPUTERNAME

# Event IDs we care about
$WatchEventIds = @(4624, 4625, 4634, 4647, 4778, 4779, 21, 23, 24, 25)

# ── Helpers ───────────────────────────────────────────────────────────────────
function Write-Log($msg) {
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Write-Host "[$ts] $msg"
}

function Get-SourceIp($msg) {
    if ($msg -match 'Source Network Address:\s+([0-9a-fA-F.:]+)') {
        return $matches[1]
    }
    if ($msg -match 'Client Address:\s+::ffff:([0-9.]+)') {
        return $matches[1]
    }
    if ($msg -match 'Client Address:\s+([0-9.]+)') {
        return $matches[1]
    }
    return $null
}

function Get-LogonId($msg) {
    if ($msg -match 'Logon ID:\s+(0x[0-9A-Fa-f]+)') {
        return $matches[1]
    }
    if ($msg -match 'Session ID:\s+(\d+)') {
        return $matches[1]
    }
    return $null
}

function Get-Username($event) {
    # Security log uses SubjectUserName / TargetUserName
    if ($event.Message -match 'New Logon:[\s\S]*?Account Name:\s+(\S+)') {
        $u = $matches[1]
        if ($u -and $u -ne '-' -and $u -notmatch '\$$') { return $u }
    }
    if ($event.Message -match 'Account Name:\s+(\S+)') {
        $u = $matches[1]
        if ($u -and $u -ne '-' -and $u -notmatch '\$$') { return $u }
    }
    # TerminalServices logs use "User:" field
    if ($event.Message -match 'User:\s+([^\r\n]+)') {
        return $matches[1].Trim()
    }
    return $null
}

function Is-RdpLogon($event) {
    # Logon Type 10 = RemoteInteractive (RDP)
    if ($event.Message -match 'Logon Type:\s+10') { return $true }
    # TerminalServices events are always RDP
    if ($event.ProviderName -match 'TerminalServices') { return $true }
    return $false
}

function Send-Event($eventId, $user, $sourceIp, $logonId, $eventTime) {
    if (-not $user) { return }

    $payload = @{
        target_ip   = $TargetIp
        target_host = $TargetHost
        event_id    = $eventId
        user        = $user
        source_ip   = $sourceIp
        logon_id    = $logonId
        event_time  = $eventTime
    } | ConvertTo-Json

    try {
        $response = Invoke-RestMethod `
            -Uri        $ApiEndpoint `
            -Method     POST `
            -Body       $payload `
            -ContentType "application/json" `
            -ErrorAction Stop
        Write-Log "Sent Event $eventId | User: $user | SrcIP: $sourceIp | Response: $($response.message)"
    }
    catch {
        Write-Log "ERROR sending event $eventId : $_"
    }
}

# ── Log Queries ───────────────────────────────────────────────────────────────
function Poll-SecurityLog($since) {
    # Security log: 4624, 4625, 4634, 4647, 4778, 4779
    $secEventIds = @(4624, 4625, 4634, 4647, 4778, 4779)
    try {
        Get-WinEvent -FilterHashtable @{
            LogName   = 'Security'
            Id        = $secEventIds
            StartTime = $since
        } -ErrorAction SilentlyContinue | ForEach-Object {
            $ev     = $_
            $eid    = $ev.Id
            $msg    = $ev.Message
            $time   = $ev.TimeCreated.ToUniversalTime().ToString("o")

            # Only care about RDP (Logon Type 10) for 4624/4634
            if ($eid -in @(4624, 4634) -and -not (Is-RdpLogon $ev)) { return }

            $user   = Get-Username $ev
            $src    = Get-SourceIp $msg
            $lid    = Get-LogonId  $msg

            Send-Event $eid $user $src $lid $time
        }
    }
    catch { Write-Log "Security log query error: $_" }
}

function Poll-TerminalServicesLog($since) {
    # TerminalServices-LocalSessionManager: 21, 23, 24, 25
    $tsEventIds = @(21, 23, 24, 25)
    try {
        Get-WinEvent -FilterHashtable @{
            LogName   = 'Microsoft-Windows-TerminalServices-LocalSessionManager/Operational'
            Id        = $tsEventIds
            StartTime = $since
        } -ErrorAction SilentlyContinue | ForEach-Object {
            $ev   = $_
            $eid  = $ev.Id
            $msg  = $ev.Message
            $time = $ev.TimeCreated.ToUniversalTime().ToString("o")
            $user = Get-Username $ev
            $src  = Get-SourceIp $msg
            $lid  = Get-LogonId  $msg

            # Skip events from LOCAL (console) sessions
            if ($src -eq 'LOCAL' -or $src -eq '127.0.0.1') { return }

            Send-Event $eid $user $src $lid $time
        }
    }
    catch { Write-Log "TerminalServices log query error: $_" }
}

# ── Main Loop ─────────────────────────────────────────────────────────────────
Write-Log "RDP Collector starting | Target: $TargetIp ($TargetHost) | Backend: $BackendUrl"
Write-Log "Watching Event IDs: $($WatchEventIds -join ', ')"

do {
    $since = (Get-Date).AddMinutes(-$LookbackMinutes)
    Write-Log "Polling logs since $($since.ToString('HH:mm:ss'))..."

    Poll-SecurityLog        $since
    Poll-TerminalServicesLog $since

    if ($RunOnce) { break }
    Start-Sleep -Seconds $PollIntervalSec

} while ($true)
