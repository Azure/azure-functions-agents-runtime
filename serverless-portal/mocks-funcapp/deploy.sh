#!/usr/bin/env bash
# Provision a Linux Consumption Azure Function App and publish the static
# Serverless Agent Portal mockups to it, producing a shareable public URL.
#
# Prerequisites: Azure CLI (az), Azure Functions Core Tools (func), and a
# signed-in session (`az login`).
#
# Usage:
#   ./deploy.sh
#   LOCATION=westus3 ./deploy.sh
#   RESOURCE_GROUP=my-rg NAME_PREFIX=portal ./deploy.sh

set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-serverless-portal-mocks}"
LOCATION="${LOCATION:-westus2}"
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-1a839f1f-10b2-4613-95ad-0800a22abbf2}"
NAME_PREFIX="${NAME_PREFIX:-mocks}"

cd "$(dirname "$0")"

for tool in az func; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "'$tool' is required but was not found on PATH." >&2
    exit 1
  }
done

# 1. Refresh the static content from the mockups (kept out of source control).
rm -rf content
cp -r ../mocks content
echo "Copied mockups into ./content"

# 2. Target the subscription and resource group.
az account set --subscription "$SUBSCRIPTION_ID"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

# 3. Provision the Function App and its dependencies.
echo "Provisioning Azure resources..."
outputs=$(az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file infra/main.bicep \
  --parameters location="$LOCATION" namePrefix="$NAME_PREFIX" \
  --query properties.outputs --output json)

app_name=$(printf '%s' "$outputs" | python -c "import sys,json;print(json.load(sys.stdin)['functionAppName']['value'])")
app_url=$(printf '%s' "$outputs" | python -c "import sys,json;print(json.load(sys.stdin)['functionAppUrl']['value'])")

# 4. Publish the function code + static content (remote/Oryx build).
echo "Publishing to ${app_name}..."
func azure functionapp publish "$app_name" --build remote

echo ""
echo "Deployed. Share this URL:"
echo "  ${app_url}"
