"""
Security tests for query_metadata validation.

Verifies that the safe boolean indexing approach prevents code injection attacks.
"""

import pytest
import pandas as pd
import sys
import os
import importlib.util

# Import query_metadata module directly from file path to avoid package side effects
QUERY_METADATA_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'dicom-album-env', 'Scripts', 'query_metadata.py'
)
spec = importlib.util.spec_from_file_location("query_metadata", QUERY_METADATA_PATH)
query_metadata_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(query_metadata_module)

query_metadata = query_metadata_module.query_metadata
parse_condition = query_metadata_module.parse_condition
evaluate_condition = query_metadata_module.evaluate_condition


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

    def test_single_equals_operator_alias(self, sample_metadata):
        """Test = operator alias for backward-compatible equality checks."""
        result = query_metadata(sample_metadata, "Modality = 'MR'")
        assert len(result) == 1
        assert result.iloc[0]['PatientID'] == 'P002'


    def test_parse_condition_normalizes_single_equals(self):
        """Test parse_condition normalizes = to ==."""
        field, op, value = parse_condition("Modality = 'CT'")
        assert field == 'Modality'
        assert op == '=='
        assert value == "'CT'"

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
        """Test complex query with mixed AND/OR operators."""
        # Valid query with multiple conditions
        result = query_metadata(
            sample_metadata,
            "Modality == 'CT' and StudyDate > '20220101' or PatientID == 'P002'"
        )
        # Should return rows matching either condition group
        assert len(result) > 0
        assert all(
            ((result['Modality'] == 'CT') & (result['StudyDate'] > '20220101')) |
            (result['PatientID'] == 'P002')
        )

    def test_complex_query_with_parentheses_rejected(self, sample_metadata):
        """Reject: query with parentheses, which are not supported."""
        # Parentheses are not supported by the simple parser
        """Test that queries with parentheses are safely rejected."""
        # Note: parentheses aren't explicitly supported. This test verifies that
        # such a query is safely rejected instead of being misinterpreted.
        with pytest.raises(ValueError, match="Invalid condition"):
            query_metadata(
                sample_metadata,
                "Modality == 'CT' and (StudyDate > '20220101' or PatientID == 'P002')"
            )

    def test_quoted_string_with_and_operator(self, sample_metadata):
        """Test: quoted string values containing 'and' keyword."""
        # This tests the critical fix for quote-aware splitting
        # SeriesDescription values might contain 'and', 'or', etc.
        sample_metadata.loc[0, 'SeriesDescription'] = 'Anterior and Posterior'
        
        result = query_metadata(
            sample_metadata,
            "SeriesDescription == 'Anterior and Posterior'"
        )
        assert len(result) == 1
        assert result.iloc[0]['SeriesDescription'] == 'Anterior and Posterior'

    def test_quoted_string_with_or_operator(self, sample_metadata):
        """Test: quoted string values containing 'or' keyword."""
        # This tests the critical fix for quote-aware splitting
        sample_metadata.loc[1, 'SeriesDescription'] = 'Left or Right'
        
        result = query_metadata(
            sample_metadata,
            "SeriesDescription == 'Left or Right'"
        )
        assert len(result) == 1
        assert result.iloc[0]['SeriesDescription'] == 'Left or Right'

    def test_new_metadata_fields_queryable(self, sample_metadata):
        """Test: newly added metadata fields are queryable."""
        # Add test data for new fields
        sample_metadata['PatientAge'] = ['045Y', '060Y', '070Y']
        sample_metadata['PatientSex'] = ['M', 'F', 'M']
        sample_metadata['StudyDescription'] = ['Brain Study', 'Chest Study', 'Abdomen Study']
        sample_metadata['AccessionNumber'] = ['ACC001', 'ACC002', 'ACC003']
        sample_metadata['SeriesNumber'] = ['1', '2', '3']
        
        # Test querying new fields
        result = query_metadata(sample_metadata, "PatientAge == '045Y'")
        assert len(result) == 1
        assert result.iloc[0]['PatientAge'] == '045Y'
        
        result = query_metadata(sample_metadata, "PatientSex == 'F'")
        assert len(result) == 1
        assert result.iloc[0]['PatientSex'] == 'F'
        
        result = query_metadata(sample_metadata, "StudyDescription == 'Brain Study'")
        assert len(result) == 1
        assert result.iloc[0]['StudyDescription'] == 'Brain Study'

        result = query_metadata(sample_metadata, "AccessionNumber == 'ACC002'")
        assert len(result) == 1
        assert result.iloc[0]['AccessionNumber'] == 'ACC002'

        result = query_metadata(sample_metadata, "SeriesNumber > '1'")
        assert len(result) == 2

    def test_string_field_relational_operator_rejected(self, sample_metadata):
        """Reject relational operator on string field."""
        with pytest.raises(ValueError, match="not supported for string field"):
            query_metadata(sample_metadata, "Modality > 'CT'")

    def test_invalid_date_literal_rejected(self, sample_metadata):
        """Reject invalid date literal format."""
        with pytest.raises(ValueError, match="YYYYMMDD format"):
            query_metadata(sample_metadata, "StudyDate > '2022-01-01'")

    def test_invalid_numeric_literal_rejected(self, sample_metadata):
        """Reject invalid numeric literal for numeric field."""
        sample_metadata['SeriesNumber'] = ['1', '2', '3']
        with pytest.raises(ValueError, match="numeric value"):
            query_metadata(sample_metadata, "SeriesNumber > 'abc'")

    def test_allowed_field_missing_in_dataframe_rejected(self, sample_metadata):
        """Reject query on allowed field that is absent in DataFrame."""
        with pytest.raises(ValueError, match="not present in metadata"):
            query_metadata(sample_metadata, "PatientSex == 'M'")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
