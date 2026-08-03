from __future__ import annotations

from azure_functions_agents.controller.package import (
    FUNCS_ZIP_DIGEST_KIND,
    CapturedContentPackage,
)

_ARCHIVE_BYTES = b"test-content-package"
_DIGEST = "sha256:0370e2fa208b1075b25c45d19a84d457acbde1a18bdaafdff8bc54be978d3f89"
_CONTENT_PACKAGE = CapturedContentPackage.create(
    archive_bytes=_ARCHIVE_BYTES,
    digest_kind=FUNCS_ZIP_DIGEST_KIND,
    digest=_DIGEST,
)


def content_package() -> CapturedContentPackage:
    return _CONTENT_PACKAGE
