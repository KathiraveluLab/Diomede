import pandas as pd
import re
from Scripts import load_dicom_files, extract_metadata

# Whitelist of allowed DICOM metadata fields for querying
ALLOWED_FIELDS = {
    "PatientID",
    "StudyDate", 
    "Modality",
    "SeriesDescription",
    "FilePath"
}

# Dangerous patterns that should never appear in queries
DANGEROUS_PATTERNS = [
    'import',
    'exec',
    'eval',
    'lambda',
    '__import__',
    '__builtins__',
    '__class__',
    '__subclasses__',
    '__globals__',
    'os.system',
    'subprocess',
    'open(',
    'file('
]


def validate_query(query):
    """
    Validate a query string to prevent code injection attacks.
    
    Args:
        query: The query string provided by the user
        
    Returns:
        True if query is valid
        
    Raises:
        ValueError: If query is invalid or unsafe
    """
    if not query or not isinstance(query, str):
        raise ValueError("Query must be a non-empty string")
    
    query = query.strip()
    
    # Check for dangerous patterns
    for pattern in DANGEROUS_PATTERNS:
        if pattern.lower() in query.lower():
            raise ValueError(f"Unsafe pattern detected: {pattern}")
    
    # Check that at least one allowed field is mentioned (using word boundaries to prevent bypass)
    has_valid_field = any(
        re.search(r'\b' + field + r'\b', query)
        for field in ALLOWED_FIELDS
    )
    if not has_valid_field:
        raise ValueError(
            f"Query must reference at least one allowed field: {', '.join(sorted(ALLOWED_FIELDS))}"
        )
    
    return True


def query_metadata(metadata_df, query):
    """
    Query the metadata DataFrame and return matching rows.
    
    Validates the input query to prevent code injection attacks
    before executing it against the DataFrame.
    
    Args:
        metadata_df: DataFrame containing DICOM metadata
        query: Query string using Pandas query syntax
        
    Returns:
        Filtered DataFrame containing rows matching the query
        
    Raises:
        ValueError: If query is invalid or unsafe
    """
    # Validate query before execution
    validate_query(query)
    
    try:
        return metadata_df.query(query)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Error executing query: {str(e)}")
