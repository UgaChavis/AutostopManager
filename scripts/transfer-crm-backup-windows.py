#!/usr/bin/env python3
"""Copy one already prepared CRM archive; never remove local or remote files.

Not installed or scheduled. A failed/unknown SSH result requires manual review of
its printed ID. This helper does not prove that its input is a CRM snapshot.
"""

import argparse
import base64
import hashlib
import os
import stat
import subprocess
import sys
import uuid

HOST = "autostop-pc"
SSH = [
    "ssh",
    "-T",
    "-oBatchMode=yes",
    "-oConnectTimeout=10",
    "-oStrictHostKeyChecking=yes",
    "-oIdentitiesOnly=yes",
    "-oControlMaster=no",
    "-oControlPath=none",
    "-oConnectionAttempts=1",
    "-oServerAliveInterval=15",
    "-oServerAliveCountMax=3",
    HOST,
]
CHUNK = 1024 * 1024

# Arguments inserted below are either a generated UUID hex string or an integer.
# All other values are fixed, not supplied via a shell.
PREAMBLE = r"""
$ErrorActionPreference = 'Stop'
if ([Environment]::MachineName -cne 'DESKTOP-7RAOHT7') { throw 'host mismatch' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (($identity.Name -split '\\')[-1] -cne '9860606') { throw 'account mismatch' }
$dir = 'C:\Users\9860606\Desktop\Резерв CRM'
$p = Get-Item -LiteralPath $dir -Force
while ($null -ne $p) {
    if (($p.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'reparse point' }
    $p = $p.Parent
}
$id = '__ID__'
$part = Join-Path $dir ('crm-' + $id + '.part')
$final = Join-Path $dir ('crm-' + $id + '.backup')
$user = $identity.User
$system = New-Object Security.Principal.SecurityIdentifier('S-1-5-18')
$admins = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
function Assert-PrivateFile([string]$path) {
    $item = Get-Item -LiteralPath $path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $item.PSIsContainer) { throw 'invalid file' }
    $security = [IO.File]::GetAccessControl($path)
    if (-not $security.AreAccessRulesProtected) { throw 'inherited ACL' }
    $allowed = @($user.Value, $system.Value, $admins.Value)
    foreach ($rule in $security.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            $rule.IdentityReference.Value -notin $allowed) { throw 'unexpected ACL' }
    }
}
"""
WRITE = r"""
$security = New-Object Security.AccessControl.FileSecurity
$security.SetAccessRuleProtection($true, $false)
foreach ($sid in @($user, $system, $admins)) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow)
    $security.AddAccessRule($rule)
}
$stream = New-Object IO.FileStream($part, [IO.FileMode]::CreateNew,
    [Security.AccessControl.FileSystemRights]::Write, [IO.FileShare]::None,
    4096, [IO.FileOptions]::None, $security)
try {
    $inputStream = [Console]::OpenStandardInput()
    $buf = New-Object byte[] 1048576
    [long]$count = 0
    while (($n = $inputStream.Read($buf, 0, $buf.Length)) -gt 0) {
        $count += $n
        if ($count -gt __SIZE__) { throw 'too many bytes' }
        $stream.Write($buf, 0, $n)
    }
    if ($count -ne __SIZE__) { throw 'truncated input' }
    $stream.Flush($true)
} finally { $stream.Dispose() }
Assert-PrivateFile $part
"""
READ = r"""
$path = __PATH__
Assert-PrivateFile $path
$inputStream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
try { $inputStream.CopyTo([Console]::OpenStandardOutput()) } finally { $inputStream.Dispose() }
"""
PUBLISH = r"""
Assert-PrivateFile $part
[IO.File]::Move($part, $final)
Assert-PrivateFile $final
"""


def command(script, ident, size=0, final=False):
    body = PREAMBLE.replace("__ID__", ident) + script.replace("__SIZE__", str(size)).replace(
        "__PATH__", "$final" if final else "$part"
    )
    encoded = base64.b64encode(body.encode("utf-16le")).decode("ascii")
    return [*SSH, "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def digest(stream):
    h = hashlib.sha256()
    size = 0
    for block in iter(lambda: stream.read(CHUNK), b""):
        h.update(block)
        size += len(block)
    return size, h.digest()


def remote_digest(cmd, expected_size):
    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        h = hashlib.sha256()
        size = 0
        while True:
            block = process.stdout.read(CHUNK)
            if not block:
                break
            size += len(block)
            if size > expected_size:
                raise RuntimeError("remote file is larger than source")
            h.update(block)
        process.stdout.close()
        if process.wait() != 0:
            raise RuntimeError("remote read failed")
        return size, h.digest()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def transfer(source):
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size == 0:
            raise RuntimeError("source must be a nonempty regular file")
        expected = digest(stream)
        stream.seek(0)
        ident = uuid.uuid4().hex
        print("Transfer ID:", ident, flush=True)
        result = subprocess.run(
            command(WRITE, ident, expected[0]),
            stdin=stream,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        after = os.fstat(fd)
        if result.returncode != 0:
            raise RuntimeError("write failed or outcome unknown")
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise RuntimeError("source changed during transfer")
        if remote_digest(command(READ, ident), expected[0]) != expected:
            raise RuntimeError("temporary file verification failed")
        result = subprocess.run(
            command(PUBLISH, ident),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("publication failed or outcome unknown")
        if remote_digest(command(READ, ident, final=True), expected[0]) != expected:
            raise RuntimeError("published file verification failed or outcome unknown")
        print("Published and read back:", ident, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="approved prepared archive on server")
    args = parser.parse_args()
    try:
        transfer(args.source)
    except (OSError, RuntimeError) as exc:
        print("Transfer incomplete:", exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
