"""Tests for MAF ExperimentalWarning suppression functionality."""

from __future__ import annotations

import warnings

import pytest


class MockExperimentalWarning(UserWarning):
    """Mock MAF ExperimentalWarning for testing."""

    pass


class MockFeatureStageWarning(UserWarning):
    """Mock MAF FeatureStageWarning for testing."""

    pass


class TestWarningSuppressionPatches:
    """Test that warning suppression patches work correctly."""

    def test_suppress_maf_warnings_flag_exists(self) -> None:
        """Verify the global suppression flag is exported."""
        import azure_functions_agents as afa

        assert hasattr(afa, "_suppress_maf_warnings")
        assert isinstance(afa._suppress_maf_warnings, bool)

    def test_original_warn_functions_are_stored(self) -> None:
        """Verify original warning functions are stored for restoration."""
        import azure_functions_agents as afa

        assert hasattr(afa, "_original_warn_explicit")
        assert hasattr(afa, "_original_warn")
        assert callable(afa._original_warn_explicit)
        assert callable(afa._original_warn)

    def test_patched_warn_suppresses_experimental_warning_by_category_name(
        self,
    ) -> None:
        """Test that warnings with ExperimentalWarning in category name are suppressed."""
        import azure_functions_agents as afa

        # Ensure suppression is enabled
        original_flag = afa._suppress_maf_warnings
        afa._suppress_maf_warnings = True

        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                # Call the patched warn with a mock ExperimentalWarning
                afa._patched_warn(
                    "Test experimental warning",
                    MockExperimentalWarning,
                    stacklevel=1,
                )
                # Should be suppressed - no warnings recorded
                assert len(w) == 0
        finally:
            afa._suppress_maf_warnings = original_flag

    def test_patched_warn_suppresses_feature_stage_warning_by_category_name(
        self,
    ) -> None:
        """Test that warnings with FeatureStageWarning in category name are suppressed."""
        import azure_functions_agents as afa

        original_flag = afa._suppress_maf_warnings
        afa._suppress_maf_warnings = True

        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                afa._patched_warn(
                    "Test feature stage warning",
                    MockFeatureStageWarning,
                    stacklevel=1,
                )
                assert len(w) == 0
        finally:
            afa._suppress_maf_warnings = original_flag

    def test_patched_warn_suppresses_by_message_content(self) -> None:
        """Test that warnings with experimental message content are suppressed."""
        import azure_functions_agents as afa

        original_flag = afa._suppress_maf_warnings
        afa._suppress_maf_warnings = True

        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                afa._patched_warn(
                    "This is experimental and may change or be removed in future versions",
                    UserWarning,
                    stacklevel=1,
                )
                assert len(w) == 0
        finally:
            afa._suppress_maf_warnings = original_flag

    def test_patched_warn_allows_non_maf_warnings(self) -> None:
        """Test that non-MAF warnings are still emitted."""
        import azure_functions_agents as afa

        original_flag = afa._suppress_maf_warnings
        afa._suppress_maf_warnings = True

        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                afa._patched_warn(
                    "This is a regular warning",
                    UserWarning,
                    stacklevel=1,
                )
                assert len(w) == 1
                assert "regular warning" in str(w[0].message)
        finally:
            afa._suppress_maf_warnings = original_flag

    def test_patched_warn_allows_all_when_suppression_disabled(self) -> None:
        """Test that all warnings are emitted when suppression is disabled."""
        import azure_functions_agents as afa

        original_flag = afa._suppress_maf_warnings
        afa._suppress_maf_warnings = False

        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                afa._patched_warn(
                    "Test experimental warning",
                    MockExperimentalWarning,
                    stacklevel=1,
                )
                # Should NOT be suppressed when flag is False
                assert len(w) == 1
        finally:
            afa._suppress_maf_warnings = original_flag

    def test_patched_warn_explicit_suppresses_experimental_warning(self) -> None:
        """Test that warn_explicit also suppresses MAF warnings."""
        import azure_functions_agents as afa

        original_flag = afa._suppress_maf_warnings
        afa._suppress_maf_warnings = True

        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                afa._patched_warn_explicit(
                    "Test experimental warning",
                    MockExperimentalWarning,
                    filename="test.py",
                    lineno=1,
                    module="test_module",
                )
                assert len(w) == 0
        finally:
            afa._suppress_maf_warnings = original_flag

    def test_patched_warn_explicit_allows_non_maf_warnings(self) -> None:
        """Test that warn_explicit allows non-MAF warnings."""
        import azure_functions_agents as afa

        original_flag = afa._suppress_maf_warnings
        afa._suppress_maf_warnings = True

        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                afa._patched_warn_explicit(
                    "This is a regular warning",
                    UserWarning,
                    filename="test.py",
                    lineno=1,
                    module="test_module",
                )
                assert len(w) == 1
        finally:
            afa._suppress_maf_warnings = original_flag
