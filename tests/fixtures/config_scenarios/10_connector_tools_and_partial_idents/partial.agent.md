---
name: Partial Identifier Agent
description: Demonstrates that hyphens and dots terminate identifier matching during substitution.
trigger:
  type: http_trigger
  args:
    route: "partial-idents"
metadata:
  # $REGION resolves but `-primary` is preserved as a literal suffix.
  primary_region: $REGION-primary
  # %REGION% requires the closing %, so %REGION-secondary% is not a match and stays literal.
  raw_label: "%REGION-secondary%"
  # Dotted access is not part of identifier syntax; only $TENANT resolves and `.id` remains.
  tenant_ref: $TENANT.id
---

The primary region is $REGION-primary and the fallback label is %REGION-secondary%.
Tenant pointer: $TENANT.id (only the leading identifier resolves).
