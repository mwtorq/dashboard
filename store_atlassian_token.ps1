# Securely store Atlassian Jira API credentials in Windows Credential Manager.
# Username = your Atlassian account email (e.g. mw@timberwilde.net)
# Password = your Atlassian API token (from id.atlassian.com)

$ErrorActionPreference = 'Stop'
$Resource = 'Atlassian:timberwilde.atlassian.net'

Write-Host ''
Write-Host '=== Atlassian Jira credential setup ===' -ForegroundColor Cyan
Write-Host "Resource : $Resource"
Write-Host 'Username : your Atlassian account email'
Write-Host 'Password : your API token (input is hidden)'
Write-Host ''

$defaultEmail = 'mw@timberwilde.net'
$email = Read-Host "Atlassian email [$defaultEmail]"
if ([string]::IsNullOrWhiteSpace($email)) { $email = $defaultEmail }

$secureToken = Read-Host 'Atlassian API token' -AsSecureString
if ($secureToken.Length -eq 0) {
    Write-Error 'No token entered.'
}

$BSTR = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
    $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringAuto($BSTR)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($BSTR) | Out-Null
}

if ($plainToken -match '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$') {
    Write-Host ''
    Write-Host 'WARNING: That looks like a token ID (UUID), not the API token secret.' -ForegroundColor Red
    Write-Host 'At id.atlassian.com the secret is shown only once when you create the token.'
    Write-Host 'It usually starts with ATATT... and is much longer than a UUID.'
    Write-Host ''
    $again = Read-Host 'Store this UUID anyway? [y/N]'
    if ($again -notmatch '^[yY]') { exit 1 }
}

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Security.Credentials.PasswordVault, Windows.Security.Credentials, ContentType = WindowsRuntime]
$vault = New-Object Windows.Security.Credentials.PasswordVault

# Remove existing entry if present (Retrieve throws if missing)
try {
    $existing = $vault.Retrieve($Resource, $email)
    $vault.Remove($existing)
    Write-Host "Replaced existing credential for $email" -ForegroundColor Yellow
} catch {
    # not found — fine
}

$cred = New-Object Windows.Security.Credentials.PasswordCredential($Resource, $email, $plainToken)
$vault.Add($cred)
$plainToken = $null

Write-Host ''
Write-Host "Stored credentials for $email under '$Resource'" -ForegroundColor Green
Write-Host 'Verify in Windows Credential Manager -> Windows Credentials.' -ForegroundColor DarkGray
Write-Host ''
