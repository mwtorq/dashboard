# Retrieve Atlassian credentials from Windows Credential Manager (for scripts).
param(
    [string]$Resource = 'Atlassian:timberwilde.atlassian.net'
)

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Security.Credentials.PasswordVault, Windows.Security.Credentials, ContentType = WindowsRuntime]
$vault = New-Object Windows.Security.Credentials.PasswordVault
$all = $vault.RetrieveAll() | Where-Object { $_.Resource -eq $Resource }
if (-not $all) {
    Write-Error "No credential found for '$Resource'. Run store_atlassian_token.ps1 first."
    exit 1
}
$c = $all | Select-Object -First 1
$c.RetrievePassword()
[PSCustomObject]@{
    Email    = $c.UserName
    Token    = $c.Password
    Resource = $c.Resource
} | ConvertTo-Json -Compress
