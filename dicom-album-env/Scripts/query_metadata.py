import pandas as pd
import re
from Scripts import load_dicom_files, extract_metadata

# Whitelist of allowed DICOM metadata fields for querying
ALLOWED_FIELDS = {
    "AccessionNumber",
    "FilePath",
    "Modality",
    "PatientAge",
    "PatientID",
    "PatientSex",
    "SeriesDescription",
    "SeriesNumber",
    "StudyDate",
    "StudyDescription",
}

# Only allow safe, well-defined operators
SAFE_OPERATORS = {'==', '!=', '<', '>', '<=', '>=', 'in'}


def split_by_keyword(query, keyword):
    """
    Split query by keyword (and/or), ignoring keywords inside quoted strings.
    
    This ensures that strings like "SeriesDescription == 'Anterior and Posterior'"
    are not incorrectly split by the 'and' keyword inside the quoted value.
    
    Args:
        query: Query string to split
        keyword: Keyword to split by ('and' or 'or')
        
    Returns:
        List of substrings split by the keyword, excluding quoted content
    """
    parts = []
    current = []
    in_quotes = False
    quote_char = None
    i = 0
    
    while i < len(query):
        # Handle quote transitions
        if query[i] in ('"', "'") and (i == 0 or query[i-1] != '\\'):
            if not in_quotes:
                in_quotes = True
                quote_char = query[i]
            elif query[i] == quote_char:
                in_quotes = False
                quote_char = None
            current.append(query[i])
            i += 1
        # Check for keyword match outside quotes
        elif not in_quotes and i + len(keyword) <= len(query):
            chunk = query[i:i+len(keyword)]
            # Check if this is a whole word (surrounded by spaces or boundaries)
            if chunk.lower() == keyword.lower():
                # Verify it's a word boundary before
                before_ok = i == 0 or query[i-1].isspace()
                # Verify it's a word boundary after
                after_ok = i + len(keyword) >= len(query) or query[i+len(keyword)].isspace()
                
                if before_ok and after_ok:
                    # Found a keyword - split here
                    if current:
                        parts.append(''.join(current).strip())
                        current = []
                    i += len(keyword)
                    # Skip whitespace after keyword
                    while i < len(query) and query[i].isspace():
                        i += 1
                    continue
            
            current.append(query[i])
            i += 1
        else:
            current.append(query[i])
            i += 1
    
    if current:
        parts.append(''.join(current).strip())
    
    return parts


def parse_condition(condition):
    """
    Parse a single condition like "Modality == 'CT'" into components.
    
    Args:
        condition: String representation of a single comparison
        
    Returns:
        (field, operator, value) tuple
        
    Raises:
        ValueError: If condition cannot be parsed
    """
    condition = condition.strip()
    
    # Try each operator, longest first to avoid partial matches
    for op in sorted(SAFE_OPERATORS, key=len, reverse=True):
        pattern = r'(\w+)\s*' + re.escape(op) + r'\s*(.+)'
        match = re.match(pattern, condition)
        
        if match:
            field = match.group(1)
            value = match.group(2).strip()
            return field, op, value
    
    raise ValueError(f"Invalid condition: {condition}. Must use format: field operator value")


def evaluate_condition(df, field, op, value):
    """
    Evaluate a single condition safely using boolean indexing.
    
    Args:
        df: DataFrame to query
        field: Column name
        op: Operator (==, !=, <, >, <=, >=, in)
        value: Value to compare (quoted string or list)
        
    Returns:
        Boolean Series for indexing
        
    Raises:
        ValueError: If field/operator invalid or condition cannot be evaluated
    """
    if field not in ALLOWED_FIELDS:
        raise ValueError(
            f"Field '{field}' not allowed. Allowed fields: {', '.join(sorted(ALLOWED_FIELDS))}"
        )
    
    if op not in SAFE_OPERATORS:
        raise ValueError(
            f"Operator '{op}' not allowed. Allowed operators: {', '.join(sorted(SAFE_OPERATORS))}"
        )
    
    # Handle list literals: ['CT', 'MR']
    if value.startswith('[') and value.endswith(']'):
        if op != 'in':
            raise ValueError(f"List values only allowed with 'in' operator, got '{op}'")
        
        # Parse list manually without eval for safety
        list_content = value[1:-1]  # Remove brackets
        # Extract all quoted strings
        items = re.findall(r"['\"]([^'\"]*)['\"]", list_content)
        
        if not items:
            raise ValueError(f"Invalid list value: {value}")
        
        return df[field].isin(items)
    
    # Handle string literals: 'CT' or "CT"
    if (value.startswith("'") and value.endswith("'")) or \
       (value.startswith('"') and value.endswith('"')):
        parsed_value = value[1:-1]  # Remove quotes
    else:
        raise ValueError(f"String values must be quoted: {value}")
    
    # Apply operator using safe boolean indexing
    if op == '==':
        return df[field] == parsed_value
    elif op == '!=':
        return df[field] != parsed_value
    elif op == '<':
        return df[field] < parsed_value
    elif op == '>':
        return df[field] > parsed_value
    elif op == '<=':
        return df[field] <= parsed_value
    elif op == '>=':
        return df[field] >= parsed_value
    else:
        # Should never reach here due to earlier validation
        raise ValueError(f"Unknown operator: {op}")


def query_metadata(metadata_df, query):
    """
    Query the metadata DataFrame using safe boolean indexing.
    
    Safely filters DataFrame by parsing queries into atomic conditions,
    validating each component, and using pandas boolean indexing instead
    of DataFrame.query(). This eliminates code injection vulnerabilities.
    
    Supported syntax:
        - Single condition: "Modality == 'CT'"
        - Multiple conditions: "Modality == 'CT' and StudyDate > '20220101'"
        - Logical OR: "Modality == 'CT' or Modality == 'MR'"
        - List membership: "Modality in ['CT', 'MR']"
    
    Args:
        metadata_df: DataFrame containing DICOM metadata
        query: Query string using supported syntax
        
    Returns:
        Filtered DataFrame containing rows matching the query
        
    Raises:
        ValueError: If query is invalid or unsafe
    """
    if not query or not isinstance(query, str):
        raise ValueError("Query must be a non-empty string")
    
    query = query.strip()
    
    # Split by 'or' first (lowest precedence) using quote-aware splitting
    # This correctly handles strings like "SeriesDescription == 'Anterior and Posterior'"
    or_conditions = split_by_keyword(query, 'or')
    
    result_mask = None
    
    # Process each OR branch
    for or_part in or_conditions:
        # Split by 'and' (higher precedence) using quote-aware splitting
        and_conditions = split_by_keyword(or_part, 'and')
        
        and_mask = None
        
        # Process each AND condition within this OR branch
        for condition in and_conditions:
            try:
                field, op, value = parse_condition(condition)
                cond_mask = evaluate_condition(metadata_df, field, op, value)
                
                if and_mask is None:
                    and_mask = cond_mask
                else:
                    and_mask = and_mask & cond_mask
            except Exception as e:
                raise ValueError(f"Error evaluating condition '{condition}': {str(e)}")
        
        # Combine with previous OR conditions
        if result_mask is None:
            result_mask = and_mask
        else:
            result_mask = result_mask | and_mask
    
    return metadata_df[result_mask]
