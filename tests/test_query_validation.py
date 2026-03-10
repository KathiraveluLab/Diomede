"""
Security tests for query_metadata validation.

Verifies that the query validation properly prevents code injection attacks.
"""

import pytest
import pandas as pd
import sys
import os

# Add Scripts to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'dicom-album-env'))

from Scripts.query_metadata import query_metadata, validate_query


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
    """Test query validation security."""

    def test_valid_query_works(self, sample_metadata):
        """Valid queries should execute successfully."""
        result = query_metadata(sample_metadata, "Modality == 'CT'")
        assert len(result) == 2
        assert all(result['Modality'] == 'CT')

    def test_import_injection_blocked(self, sample_metadata):
        """Reject: __import__('os').system() injection attempt."""
        with pytest.raises(ValueError, match="Unsafe pattern detected"):
            query_metadata(sample_metadata, "__import__('os').system('rm -rf /')")

    def test_exec_injection_blocked(self, sample_metadata):
        """Reject: exec() code execution attempt."""
        with pytest.raises(ValueError, match="Unsafe pattern detected"):
            query_metadata(sample_metadata, "exec('import os; os.system(\"whoami\")')")

    def test_eval_injection_blocked(self, sample_metadata):
        """Reject: eval() dynamic code evaluation."""
        with pytest.raises(ValueError, match="Unsafe pattern detected"):
            query_metadata(sample_metadata, "eval('__import__(\"os\").system(\"id\")')")

    def test_empty_query_rejected(self, sample_metadata):
        """Reject: empty query string."""
        with pytest.raises(ValueError, match="non-empty string"):
            query_metadata(sample_metadata, "")

    def test_no_valid_fields_rejected(self, sample_metadata):
        """Reject: query without any valid field references."""
        with pytest.raises(ValueError, match="must reference at least one"):
            query_metadata(sample_metadata, "1 == 1")

    def test_validate_query_returns_true(self):
        """Direct validation of safe query should return True."""
        assert validate_query("Modality == 'CT'") is True

    def test_complex_valid_query(self, sample_metadata):
        """Complex queries with AND/OR should work."""
        result = query_metadata(sample_metadata, "Modality == 'CT' and PatientID == 'P001'")
        assert len(result) == 1



if __name__ == "__main__":
    pytest.main([__file__, "-v"])
