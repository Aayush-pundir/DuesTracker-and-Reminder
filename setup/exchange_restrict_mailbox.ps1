# STEP 2 of 2 - run by an Exchange Online admin (PowerShell 7 + ExchangeOnlineManagement module).
# Limits the app so it can send ONLY as the given mailbox (Application Access Policy). Untested against your tenant.
param(
  [Parameter(Mandatory = $true)][string]$AppId,
  [string]$Mailbox   = "accounts@eroute.in",
  [string]$GroupName = "dues-control-room-mailboxes"
)
Connect-ExchangeOnline
if (-not (Get-DistributionGroup -Identity $GroupName -ErrorAction SilentlyContinue)) {
  New-DistributionGroup -Name $GroupName -Alias ($GroupName -replace '[^a-zA-Z0-9]', '') -Type Security -Members $Mailbox | Out-Null
} else {
  Add-DistributionGroupMember -Identity $GroupName -Member $Mailbox -ErrorAction SilentlyContinue
}
New-ApplicationAccessPolicy -AppId $AppId -PolicyScopeGroupId $GroupName -AccessRight RestrictAccess `
  -Description "Dues Control Room may send only as $Mailbox"
Write-Host "Checking the policy (should say AccessCheckResult : Granted):"
Test-ApplicationAccessPolicy -Identity $Mailbox -AppId $AppId
Write-Host "Policies can take 30-60 minutes to take effect. Then use Admin > Verify credentials > Send test e-mail in the app."
