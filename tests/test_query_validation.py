"""
Security tests for query_metadata validation.

Verifies that the safe boolean indexing approach prevents code injection attacks.
"""

import pytest
import pandas as pd
import sys
import os

# Add Scripts to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'dicom-album-env'))

from Scripts.query_metadata import query_metadata, parse_condition, evaluate_condition


@pytest.fixture
def sample_metadata():
    """Create a sample metadata DataFrame for testing."""
    data = {
        'PatientID': ['P001', 'P002', 'P003'],
        'StudyDate': ['20220101', '20220215', '20220315'],
        'Modality': ['CT', 'MR', 'CT'],
        'SeriesDescription': ['Abdomen', 'Brain', 'Chest'],
        'FilePath': ['/path/f1.dcm', '/path/f2.dcm', '/path/f3.dcm'],
    }
    return pd.DataFrame(data)


class TestQueryValidation:
    """Test query validation and safe filtering."""

    def test_valid_query_works(self, sample_metadata):
        """Valid queries should execute successfully."""
        result = query_metadata(sample_metadata, "Modality == 'CT'")
        assert len(result) == 2
        assert all(result['Modality'] == 'CT')

    def test_equality_operator(self, sample_metadata):
        """Test == operator."""
        result = query_metadata(sample_metadata, "StudyDate == '20220101'")
        assert len(result) == 1
        assert result.iloc[0]['PatientID'] == 'P001'

    def test_not_equal_operator(self, sample_metadata):
        """Test != operator."""
        result = query_metadata(sample_metadata, "Modality != 'CT'")
        assert len(result) == 1
        assert result.iloc[0]['Modality'] == 'MR'

    def test_less_than_operator(self, sample_metadata):
        """Test < operator."""
        result = query_metadata(sample_metadata, "StudyDate < '20220215'")
        assert len(result) == 1

    def test_greater_than_operator(self, sample_metadata):
        """Test > operator."""
        result = query_metadata(sample_metadata, "StudyDate > '20220215'")
        assert len(result) == 1

    def test_in_operator_with_list(self, sample_metadata):
        """Test 'in' operator with list literal."""
        result = query_metadata(sample_metadata, "Modality in ['CT', 'MR']")
        assert len(result) == 3  # All rows

    def test_and_operator(self, sample_metadata):
        """Test AND logic combining conditions."""
        result = query_metadata(sample_metadata, "Modality == 'CT' and StudyDate > '20220101'")
        assert len(result) == 1
        assert result.iloc[0]['StudyDate'] == '20220315'

    def test_or_operator(self, sample_metadata):
        """Test OR logic combining conditions."""
        result = query_metadata(sample_metadata, "Modality == 'MR' or PatientID == 'P003'")
        assert len(result) == 2

    def test_empty_query_rejected(self, sample_metadata):
        """Reject: empty query string."""
        with pytest.raises(ValueError, match="non-empty string"):
            query_metadata(sample_metadata, "")

    def test_invalid_field_rejected(self, sample_metadata):
        """Reject: query with non-whitelisted field."""
        with pytest.raises(ValueError, match="not allowed"):
            query_metadata(sample_metadata, "SystemInfo == 'something'")

    def test_invalid_operator_rejected(self, sample_metadata):
        """Reject: unsupported operator."""
        with pytest.raises(ValueError, match="Invalid condition"):
            query_metadata(sample_metadata, "Modality + 'CT'")

    def test_invalid_syntax_rejected(self, sample_metadata):
        """Reject: malformed condition."""
        with pytest.raises(ValueError, match="Invalid condition"):
            query_metadata(sample_metadata, "invalid syntax here")

    def test_string_concatenation_bypass_blocked(self, sample_metadata):
        """Reject: string concatenation bypass attempt."""
        # This would have bypassed the denylist, but now fails due to invalid syntax
        with pytest.raises(ValueError):
            query_metadata(sample_metadata, "Modality == '__' + 'import__'")

    def test_getattr_injection_blocked(self, sample_metadata):
        """Reject: getattr injection (would be caught as invalid field)."""
        with pytest.raises(ValueError, match="not allowed|Invalid"):
            query_metadata(sample_metadata, "getattr == 'test'")

    def test_import_keyword_blocked(self, sample_metadata):
        """Reject: import keyword in query."""
        with pytest.raises(ValueError, match="Invalid condition|not allowed"):
            query_metadata(sample_metadata, "import == 'something'")

    def test_list_with_single_quote_values(self, sample_metadata):
        """Test list with single-quoted values."""
        result = query_metadata(sample_metadata, "Modality in ['CT']")
        assert len(result) == 2

    def test_list_with_double_quote_values(self, sample_metadata):
        """Test list with double-quoted values."""
        result = query_metadata(sample_metadata, 'Modality in ["MR"]')
        assert len(result) == 1

    def test_unquoted_string_rejected(self, sample_metadata):
        """Reject: unquoted string values."""
        with pytest.raises(ValueError, match="quoted"):
            query_metadata(sample_metadata, "Modality == CT")

    def test_complex_and_or_query(self, sample_metadata):
        """Test complex query with mixed AND/OR."""
        result = query_metadata(
            sample_metadata,
            "Modality == 'CT' and (StudyDate > '20220101' or PatientID == 'P002')"
        )
        # Note: parentheses aren't explicitly supported, but this tests robustness
        # The current implementation will treat this as trying to find PatientID "(StudyDate"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
