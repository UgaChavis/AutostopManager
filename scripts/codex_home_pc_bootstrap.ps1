[CmdletBinding()]
param(
    [string]$ServerHost = "46.8.254.189",
    [string]$ServerUser = "codex-home-tunnel",
    [int]$ServerSshPort = 22,
    [string]$RemoteListenHost = "127.0.0.1",
    [int]$ServerListenPort = 22220,
    [string]$LocalSshHost = "127.0.0.1",
    [int]$LocalSshPort = 22,
    [string]$CodexUser = "codexadmin"
)

$ErrorActionPreference = "Stop"

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated PowerShell/Codex session."
    }
}

function New-StrongLocalPassword {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    return ([Convert]::ToBase64String($bytes) + "aA1!")
}

function Set-PrivateAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    & icacls $Path /inheritance:r /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F" | Out-Null
}

function Set-SystemOwner {
    param([Parameter(Mandatory = $true)][string]$Path)
    try {
        $acl = Get-Acl -LiteralPath $Path
        $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier("S-1-5-18")))
        Set-Acl -LiteralPath $Path -AclObject $acl
    } catch {
        Write-Host "owner_warning=$Path"
    }
}

function Remove-ManagedBlock {
    param([string[]]$Lines)
    $out = New-Object System.Collections.Generic.List[string]
    $skip = $false
    foreach ($line in $Lines) {
        if ($line -eq "# BEGIN CODEX REMOTE MANAGED") {
            $skip = $true
            continue
        }
        if ($line -eq "# END CODEX REMOTE MANAGED") {
            $skip = $false
            continue
        }
        if (-not $skip) {
            $out.Add($line)
        }
    }
    return ,$out.ToArray()
}

function Set-CodexSshdConfig {
    param([Parameter(Mandatory = $true)][string]$ConfigPath)

    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        New-Item -ItemType File -Path $ConfigPath -Force | Out-Null
    }

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    Copy-Item -LiteralPath $ConfigPath -Destination "$ConfigPath.codex-remote.bak.$timestamp" -Force

    $lines = @(Get-Content -LiteralPath $ConfigPath -ErrorAction SilentlyContinue)
    $lines = @(Remove-ManagedBlock -Lines $lines)

    $firstMatch = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^\s*Match\s+") {
            $firstMatch = $i
            break
        }
    }

    if ($firstMatch -eq 0) {
        $pre = @()
        $post = @($lines)
    } elseif ($firstMatch -gt 0) {
        $pre = @($lines[0..($firstMatch - 1)])
        $post = @($lines[$firstMatch..($lines.Count - 1)])
    } else {
        $pre = @($lines)
        $post = @()
    }

    $managedKeys = @(
        "Port",
        "ListenAddress",
        "PubkeyAuthentication",
        "PasswordAuthentication",
        "KbdInteractiveAuthentication",
        "ChallengeResponseAuthentication",
        "PermitEmptyPasswords",
        "AuthorizedKeysFile"
    )
    $keyPattern = "^\s*(" + (($managedKeys | ForEach-Object { [regex]::Escape($_) }) -join "|") + ")\b"

    $cleanPre = New-Object System.Collections.Generic.List[string]
    foreach ($line in $pre) {
        if ($line -match $keyPattern -and $line -notmatch "^\s*#") {
            $cleanPre.Add("# CodexRemote disabled global duplicate: $line")
        } else {
            $cleanPre.Add($line)
        }
    }

    $block = @(
        "# BEGIN CODEX REMOTE MANAGED",
        "Port 22",
        "ListenAddress 127.0.0.1",
        "PubkeyAuthentication yes",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "PermitEmptyPasswords no",
        "AuthorizedKeysFile .ssh/authorized_keys __PROGRAMDATA__/ssh/administrators_authorized_keys",
        "# END CODEX REMOTE MANAGED"
    )

    $newLines = @($cleanPre.ToArray()) + $block + @($post)
    Set-Content -LiteralPath $ConfigPath -Value $newLines -Encoding ascii
}

function Ensure-OpenSshServer {
    $capability = Get-WindowsCapability -Online |
        Where-Object { $_.Name -like "OpenSSH.Server*" } |
        Select-Object -First 1

    if ($null -eq $capability) {
        throw "OpenSSH.Server Windows capability was not found."
    }

    if ($capability.State -ne "Installed") {
        Add-WindowsCapability -Online -Name $capability.Name | Out-Null
    }

    Set-Service -Name sshd -StartupType Automatic
}

function Ensure-OpenSshHostKeyAcl {
    param([Parameter(Mandatory = $true)][string]$ProgramDataSsh)

    if (-not (Test-Path -LiteralPath $ProgramDataSsh)) {
        return
    }

    Get-ChildItem -LiteralPath $ProgramDataSsh -Filter "ssh_host_*_key" -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notlike "*.pub" } |
        ForEach-Object {
            Set-PrivateAcl -Path $_.FullName
            Set-SystemOwner -Path $_.FullName
        }
}

function Ensure-CodexAdminUser {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$SecretPath
    )

    $adminSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-32-544")
    $adminGroup = $adminSid.Translate([Security.Principal.NTAccount]).Value.Split("\")[-1]
    $existing = Get-LocalUser -Name $Name -ErrorAction SilentlyContinue

    if ($null -eq $existing) {
        $plain = New-StrongLocalPassword
        $secure = ConvertTo-SecureString $plain -AsPlainText -Force
        New-LocalUser -Name $Name -Password $secure -FullName "Codex Remote Admin" -Description "Admin account for Codex reverse SSH access." -PasswordNeverExpires | Out-Null
        Set-Content -LiteralPath $SecretPath -Value $plain -Encoding ascii
        Set-PrivateAcl -Path $SecretPath
        Write-Host "codexadmin_created=true"
    } else {
        Write-Host "codexadmin_created=false"
    }

    $members = @(Get-LocalGroupMember -Group $adminGroup -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
    $memberNames = @($members | ForEach-Object { ($_ -split "\\")[-1] })
    if ($memberNames -notcontains $Name) {
        Add-LocalGroupMember -Group $adminGroup -Member $Name
    }
    Write-Host "codexadmin_is_admin=true"
}

function Ensure-AdminAuthorizedKey {
    param(
        [Parameter(Mandatory = $true)][string]$PublicKeySource,
        [Parameter(Mandatory = $true)][string]$AuthorizedKeysPath
    )

    $publicKey = (Get-Content -LiteralPath $PublicKeySource -Raw).Trim()
    if ($publicKey -notmatch "^ssh-ed25519\s+") {
        throw "Expected an ed25519 public key in $PublicKeySource."
    }

    if (-not (Test-Path -LiteralPath $AuthorizedKeysPath)) {
        New-Item -ItemType File -Path $AuthorizedKeysPath -Force | Out-Null
    }
    $existing = Get-Content -LiteralPath $AuthorizedKeysPath -Raw -ErrorAction SilentlyContinue
    if ($existing -notmatch [regex]::Escape($publicKey)) {
        Add-Content -LiteralPath $AuthorizedKeysPath -Value $publicKey -Encoding ascii
    }
    Set-PrivateAcl -Path $AuthorizedKeysPath
}

function Ensure-LoopbackFirewallRule {
    Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue |
        Disable-NetFirewallRule | Out-Null

    $existing = Get-NetFirewallRule -Name "CodexRemote-OpenSSH-Loopback" -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        New-NetFirewallRule `
            -Name "CodexRemote-OpenSSH-Loopback" `
            -DisplayName "Codex Remote OpenSSH loopback only" `
            -Direction Inbound `
            -Action Allow `
            -Protocol TCP `
            -LocalAddress 127.0.0.1 `
            -LocalPort 22 | Out-Null
    } else {
        Enable-NetFirewallRule -Name "CodexRemote-OpenSSH-Loopback" | Out-Null
    }
}

function Write-TunnelRunner {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Base,
        [Parameter(Mandatory = $true)][string]$KeyPath,
        [Parameter(Mandatory = $true)][string]$KnownHostsPath
    )

    $content = @"
`$ErrorActionPreference = "Continue"
`$Base = "$Base"
`$LogDir = Join-Path `$Base "logs"
`$Log = Join-Path `$LogDir "reverse-tunnel.log"
`$SshExe = Join-Path `$env:WINDIR "System32\OpenSSH\ssh.exe"
`$Key = "$KeyPath"
`$KnownHosts = "$KnownHostsPath"
New-Item -ItemType Directory -Force -Path `$LogDir | Out-Null

function Redact-Text {
    param([AllowNull()][string]`$Text)
    if ([string]::IsNullOrWhiteSpace(`$Text)) { return "" }
    `$safe = `$Text
    `$safe = `$safe -replace [regex]::Escape(`$Key), "[private-key-file]"
    `$safe = `$safe -replace "home_reverse_to_server_ed25519", "[private-key-file]"
    `$safe = `$safe -replace "codexadmin-password\.txt", "[secret-file]"
    `$safe = `$safe -replace "(?i)(password|token|secret)\s*[:=]\s*\S+", '`$1=[redacted]'
    return `$safe
}

function Add-LogLine {
    param([string]`$Line)
    Add-Content -LiteralPath `$Log -Value `$Line -Encoding UTF8
}

function Quote-Arg {
    param([string]`$Arg)
    if (`$Arg -match '[\s"]') {
        return '"' + (`$Arg -replace '"','\"') + '"'
    }
    return `$Arg
}

while (`$true) {
    `$started = Get-Date -Format o
    Add-LogLine "[`$started] starting reverse tunnel"

    `$sshArgs = @(
        "-NT",
        "-p", "$ServerSshPort",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=`$KnownHosts",
        "-i", `$Key,
        "-R", "$RemoteListenHost`:$ServerListenPort`:$LocalSshHost`:$LocalSshPort",
        "$ServerUser@$ServerHost"
    )

    `$psi = New-Object System.Diagnostics.ProcessStartInfo
    `$psi.FileName = `$SshExe
    `$psi.Arguments = (`$sshArgs | ForEach-Object { Quote-Arg `$_ }) -join " "
    `$psi.UseShellExecute = `$false
    `$psi.CreateNoWindow = `$true
    `$psi.RedirectStandardOutput = `$true
    `$psi.RedirectStandardError = `$true

    `$process = New-Object System.Diagnostics.Process
    `$process.StartInfo = `$psi
    try {
        [void]`$process.Start()
        `$process.WaitForExit()
        `$stdout = `$process.StandardOutput.ReadToEnd()
        `$stderr = `$process.StandardError.ReadToEnd()
        `$combined = ((`$stdout, `$stderr) | Where-Object { -not [string]::IsNullOrWhiteSpace(`$_) }) -join "`n"
        if (-not [string]::IsNullOrWhiteSpace(`$combined)) {
            Add-LogLine (Redact-Text `$combined)
        }
        `$code = `$process.ExitCode
    } catch {
        Add-LogLine (Redact-Text ("runner_exception: " + `$_.Exception.Message))
        `$code = -1
    }

    `$ended = Get-Date -Format o
    Add-LogLine "[`$ended] ssh exited code=`$code; restarting in 10s"
    Start-Sleep -Seconds 10
}
"@
    Set-Content -LiteralPath $Path -Value $content -Encoding ascii
}

function Ensure-TunnelTask {
    param([Parameter(Mandatory = $true)][string]$RunnerPath)

    $taskName = "CodexRemoteReverseTunnel"
    $taskPath = "\Autostop\"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RunnerPath`""
    $triggers = @(
        (New-ScheduledTaskTrigger -AtStartup),
        (New-ScheduledTaskTrigger -AtLogOn)
    )
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Days 30)
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

    Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Action $action -Trigger $triggers -Settings $settings -Principal $principal | Out-Null
    Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath
}

Assert-Administrator

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$base = Join-Path $env:ProgramData "CodexRemote"
$sshDir = Join-Path $base "ssh"
$scriptDir = Join-Path $base "scripts"
$secretDir = Join-Path $base "secrets"
$logDir = Join-Path $base "logs"
New-Item -ItemType Directory -Force -Path $base, $sshDir, $scriptDir, $secretDir, $logDir | Out-Null
Set-PrivateAcl -Path $sshDir
Set-PrivateAcl -Path $secretDir

$reverseKeySource = Join-Path $scriptRoot "home_reverse_to_server_ed25519"
$serverPublicKeySource = Join-Path $scriptRoot "server_to_home_authorized_key.pub"
if (-not (Test-Path -LiteralPath $reverseKeySource)) {
    throw "Missing private reverse tunnel key file: $reverseKeySource"
}
if (-not (Test-Path -LiteralPath $serverPublicKeySource)) {
    throw "Missing server public key file: $serverPublicKeySource"
}

$reverseKeyDest = Join-Path $sshDir "home_reverse_to_server_ed25519"
$knownHostsPath = Join-Path $sshDir "known_hosts"
$runnerPath = Join-Path $scriptDir "Start-CodexReverseTunnel.ps1"
$passwordPath = Join-Path $secretDir "codexadmin-password.txt"

Copy-Item -LiteralPath $reverseKeySource -Destination $reverseKeyDest -Force
Set-PrivateAcl -Path $reverseKeyDest

Ensure-OpenSshServer
Ensure-CodexAdminUser -Name $CodexUser -SecretPath $passwordPath

$programDataSsh = Join-Path $env:ProgramData "ssh"
New-Item -ItemType Directory -Force -Path $programDataSsh | Out-Null
Ensure-OpenSshHostKeyAcl -ProgramDataSsh $programDataSsh
$authorizedKeysPath = Join-Path $programDataSsh "administrators_authorized_keys"
Ensure-AdminAuthorizedKey -PublicKeySource $serverPublicKeySource -AuthorizedKeysPath $authorizedKeysPath

$sshdConfig = Join-Path $programDataSsh "sshd_config"
Set-CodexSshdConfig -ConfigPath $sshdConfig
$sshdExe = Join-Path $env:WINDIR "System32\OpenSSH\sshd.exe"
& $sshdExe -t -f $sshdConfig
if ($LASTEXITCODE -ne 0) {
    $compat = @(Get-Content -LiteralPath $sshdConfig) | ForEach-Object {
        if ($_ -eq "KbdInteractiveAuthentication no") {
            "# CodexRemote compatibility disabled: $_"
        } else {
            $_
        }
    }
    Set-Content -LiteralPath $sshdConfig -Value $compat -Encoding ascii
    & $sshdExe -t -f $sshdConfig
}
if ($LASTEXITCODE -ne 0) {
    throw "sshd_config validation failed."
}

Ensure-LoopbackFirewallRule
Restart-Service -Name sshd -Force

Write-TunnelRunner -Path $runnerPath -Base $base -KeyPath $reverseKeyDest -KnownHostsPath $knownHostsPath
Set-PrivateAcl -Path $runnerPath
Ensure-TunnelTask -RunnerPath $runnerPath

Start-Sleep -Seconds 5
$tcp = Test-NetConnection -ComputerName $LocalSshHost -Port $LocalSshPort -WarningAction SilentlyContinue
$listeners = @(Get-NetTCPConnection -LocalPort $LocalSshPort -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty LocalAddress -Unique)
$task = Get-ScheduledTask -TaskName "CodexRemoteReverseTunnel" -TaskPath "\Autostop\" -ErrorAction SilentlyContinue

Write-Host "codex_remote_bootstrap=ok"
Write-Host "base_path=$base"
Write-Host "local_sshd_tcp_test=$($tcp.TcpTestSucceeded)"
Write-Host "local_sshd_listeners=$($listeners -join ',')"
Write-Host "reverse_task_state=$($task.State)"
Write-Host "server_reverse_listener=$RemoteListenHost`:$ServerListenPort"
Write-Host "secrets_not_printed=true"
