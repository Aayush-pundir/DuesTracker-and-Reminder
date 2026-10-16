#!/usr/bin/env bash
# STEP 1 of 2 - run by a Microsoft 365 / Entra ID admin (Azure Cloud Shell works: portal.azure.com -> ">_").
# Creates the "Dues Control Room" app, grants it the Microsoft Graph APPLICATION permission Mail.Send, gives admin consent,
# and prints the values for data/channels.env. Untested against your tenant - read it before running.
set -euo pipefail
APP_NAME="${APP_NAME:-Dues Control Room}"
GRAPH_APP_ID="00000003-0000-0000-c000-000000000000"            # Microsoft Graph
MAIL_SEND_ROLE="b633e1c5-b582-4048-a93e-9f11b44c7e96"          # Mail.Send (Application)
MAILBOX="${MAILBOX:-accounts@eroute.in}"

APP_ID=$(az ad app create --display-name "$APP_NAME" --sign-in-audience AzureADMyOrg --query appId -o tsv)
az ad sp create --id "$APP_ID" >/dev/null
az ad app permission add --id "$APP_ID" --api "$GRAPH_APP_ID" --api-permissions "$MAIL_SEND_ROLE=Role"
echo "Waiting for Entra to replicate the app..."; sleep 25
az ad app permission admin-consent --id "$APP_ID"
SECRET=$(az ad app credential reset --id "$APP_ID" --display-name "dues-control-room" --years 1 --query password -o tsv)
TENANT=$(az account show --query tenantId -o tsv)

cat <<EOF

=== Put these lines in data/channels.env (keep the file private) ===
MS_TENANT_ID=$TENANT
MS_CLIENT_ID=$APP_ID
MS_CLIENT_SECRET=$SECRET
MS_SENDER=$MAILBOX

=== Next: STEP 2 - restrict the app to ONE mailbox (otherwise it could send as anyone) ===
pwsh ./exchange_restrict_mailbox.ps1 -AppId $APP_ID -Mailbox $MAILBOX
(The secret expires in 1 year - set a reminder to rotate it.)
EOF
