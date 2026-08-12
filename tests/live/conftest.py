"""Quiet the Azure SDK HTTP logger so a live-smoke failure stays readable."""

import logging

logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
