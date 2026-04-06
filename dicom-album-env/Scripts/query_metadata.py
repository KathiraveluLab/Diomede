import pandas as pd
import re

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
SAFE_OPERATORS = {'=', '==', '!=', '<', '>', '<=', '>=', 'in', 'contains'}

# Expected type per field for safe comparisons
FIELD_TYPES = {
    "AccessionNumber": "string",
    "FilePath": "string",
    "Modality": "string",
    "PatientAge": "string",
    "PatientID": "string",
    "PatientSex": "string",
    "SeriesDescription": "string",
    "SeriesNumber": "numeric",
    "StudyDate": "date",
    "StudyDescription": "string",
}


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
            return field, normalize_operator(op), value
    
    raise ValueError(f"Invalid condition: {condition}. Must use format: field operator value")


def normalize_operator(op):
    """Normalize accepted operator aliases to canonical form."""
    if op == '=':
        return '=='
    return op


def parse_scalar_value(value, field_type, quotes_required=True):
    """
    Parse scalar literal values and enforce expected field type.

    Returns:
        Parsed value in a Python type appropriate for field_type
    """
    # Handle quoted string literals: 'CT' or "CT"
    single_quoted = re.fullmatch(r"'[^']*'", value) is not None
    double_quoted = re.fullmatch(r'"[^"]*"', value) is not None
    is_quoted = single_quoted or double_quoted
    if single_quoted or double_quoted:
        literal = value[1:-1]
    else:
        literal = value

    if field_type == "string":
        if quotes_required and not is_quoted:
            raise ValueError(f"String values must be quoted: {value}")
        return literal

    if field_type == "numeric":
        try:
            return float(literal)
        except ValueError:
            raise ValueError(f"Numeric comparison requires a numeric value, got: {value}")

    if field_type == "date":
        if not re.fullmatch(r"\d{8}", literal):
            raise ValueError(
                f"Date comparison requires YYYYMMDD format, got: {value}"
            )
        parsed = pd.to_datetime(literal, format="%Y%m%d", errors="coerce")
        if pd.isna(parsed):
            raise ValueError(f"Invalid date value: {value}")
        return parsed

    # Defensive fallback
    raise ValueError(f"Unsupported field type '{field_type}' for value parsing")


def get_typed_series(df, field, field_type):
    """Return a typed pandas Series for safe comparisons."""
    series = df[field]

    if field_type == "string":
        return series.astype(str)

    if field_type == "numeric":
        typed = pd.to_numeric(series, errors="coerce")
        if (typed.isna() & series.notna()).any():
            raise ValueError(
                f"Field '{field}' contains non-numeric values and cannot be compared numerically"
            )
        return typed

    if field_type == "date":
        typed = pd.to_datetime(series, format="%Y%m%d", errors="coerce")
        if (typed.isna() & series.notna()).any():
            raise ValueError(
                f"Field '{field}' contains non-date values and cannot be compared as dates"
            )
        return typed

    raise ValueError(f"Unsupported field type '{field_type}' for field '{field}'")


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
    if field not in df.columns:
        raise ValueError(f"Field '{field}' not present in metadata")

    
    if op not in SAFE_OPERATORS:
        raise ValueError(
            f"Operator '{op}' not allowed. Allowed operators: {', '.join(sorted(SAFE_OPERATORS))}"
        )

    field_type = FIELD_TYPES.get(field, "string")
    
    if op == 'contains':
        if field_type != "string":
            raise ValueError(
                f"Operator '{op}' is only supported for string field '{field}'"
            )
        parsed_value = parse_scalar_value(value, field_type)
        series = get_typed_series(df, field, field_type)
        return series.str.contains(parsed_value, na=False, regex=False)

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
        
        series = get_typed_series(df, field, field_type)
        typed_items = [parse_scalar_value(item, field_type, quotes_required=False) for item in items]
        return series.isin(typed_items)
    
    if op in {'<', '>', '<=', '>='} and field_type == "string":
        raise ValueError(
            f"Operator '{op}' is not supported for string field '{field}'. Use == or !="
        )

    parsed_value = parse_scalar_value(value, field_type)
    series = get_typed_series(df, field, field_type)
    
    # Apply operator using safe boolean indexing
    if op == '==':
        return series == parsed_value
    elif op == '!=':
        return series != parsed_value
    elif op == '<':
        return series < parsed_value
    elif op == '>':
        return series > parsed_value
    elif op == '<=':
        return series <= parsed_value
    elif op == '>=':
        return series >= parsed_value
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
    if not query:
        raise ValueError("Query must be a non-empty string")
    
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
