#!/usr/bin/env bash
# Provision Azure resources and deploy the Serverless Agent Portal to Azure
# Container Apps using the Azure Developer CLI (azd).
#
# Prerequisites: azd, Docker, and `az login` (or `azd auth login`).
#
# Usage:
#   ./deploy.sh                          # defaults: env serverless-portal, westus3, target sub
#   LOCATION=eastus2 ./deploy.sh         # override region
#   ENV_NAME=my-portal ./deploy.sh       # override azd environment name
set -euo pipefail

ENV_NAME="${ENV_NAME:-serverless-portal}"
LOCATION="${LOCATION:-eastus2}"
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-1a839f1f-10b2-4613-95ad-0800a22abbf2}"
MSAL_CLIENT_ID="${MSAL_CLIENT_ID:-0ceccceb-9c05-4953-9193-d94f9daa18d3}"
MSAL_AUTHORITY="${MSAL_AUTHORITY:-https://login.microsoftonline.com/organizations}"

cd "$(dirname "$0")"

if ! command -v azd >/dev/null 2>&1; then
  echo "azd (Azure Developer CLI) is not installed. See https://aka.ms/azd-install." >&2
  exit 1
fi

# Create the azd environment if it does not exist yet (idempotent).
if ! azd env list --output json 2>/dev/null | grep -q "\"Name\": *\"${ENV_NAME}\""; then
  azd env new "$ENV_NAME" --subscription "$SUBSCRIPTION_ID" --location "$LOCATION" --no-prompt
fi
azd env select "$ENV_NAME"

# Pin subscription/location and portal configuration for the Bicep templates.
azd env set AZURE_SUBSCRIPTION_ID "$SUBSCRIPTION_ID"
azd env set AZURE_LOCATION "$LOCATION"
azd env set PORTAL_SUBSCRIPTION_ID "$SUBSCRIPTION_ID"
azd env set MSAL_CLIENT_ID "$MSAL_CLIENT_ID"
azd env set MSAL_AUTHORITY "$MSAL_AUTHORITY"

# Provision infra + build/push image + deploy the container app.
azd up --no-prompt

echo ""
echo "Deployed. Portal URL:"
azd env get-values | grep '^PORTAL_URI='
echo ""
echo "Reminder: add the PORTAL_URI origin above as a SPA redirect URI on app"
echo "  ${MSAL_CLIENT_ID}  so browser sign-in works."
