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

# Only allow safe, well-defined operators
SAFE_OPERATORS = {'==', '!=', '<', '>', '<=', '>=', 'in'}


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
        list_content = value[1:-1]
        items = []

        # Use a robust parser that iterates through quoted strings and ensures no other unquoted values exist.
        last_end = 0
        for match in re.finditer(r"'([^']*)'|\"([^\"]*)\"", list_content):
            gap = list_content[last_end:match.start()]
            # Ensure gaps only contain whitespace and commas.
            if any(c not in ' \t\n\r,' for c in gap):
                raise ValueError(f"Invalid list value: {value}. Contains unquoted items.")
            
            items.append(match.group(1) if match.group(1) is not None else match.group(2))
            last_end = match.end()
        
        # Check for trailing unquoted content.
        trailing_content = list_content[last_end:]
        if any(c not in ' \t\n\r,' for c in trailing_content):
            raise ValueError(f"Invalid list value: {value}. Contains unquoted items.")
        
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
    
    # Split by 'or' first (lowest precedence)
    # Use case-insensitive split to handle "Modality == 'CT' OR PatientID == 'P001'"
    or_conditions = re.split(r'\s+or\s+', query, flags=re.IGNORECASE)
    
    result_mask = None
    
    # Process each OR branch
    for or_part in or_conditions:
        # Split by 'and' (higher precedence)
        and_conditions = re.split(r'\s+and\s+', or_part, flags=re.IGNORECASE)
        
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
            except (ValueError, KeyError) as e:
                raise ValueError(f"Error evaluating condition '{condition}': {str(e)}")
        
        # Combine with previous OR conditions
        if and_mask is not None:
            if result_mask is None:
                result_mask = and_mask
            else:
                result_mask = result_mask | and_mask
    
    if result_mask is None:
        return metadata_df.iloc[0:0]  # Return an empty DataFrame
    
    return metadata_df[result_mask]
