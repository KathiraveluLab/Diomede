import logging
import math
import string
import struct
from os import PathLike
from typing import Union, Optional
from threading import Lock

import pydicom
from pydicom.sequence import Sequence

LOG = logging.getLogger(__name__)

MACHO_SIGNATURES = (
    (b'\xfe\xed\xfa\xce', "Mach-O 32-bit big-endian"),
    (b'\xce\xfa\xed\xfe', "Mach-O 32-bit little-endian"),
    (b'\xfe\xed\xfa\xcf', "Mach-O 64-bit big-endian"),
    (b'\xcf\xfa\xed\xfe', "Mach-O 64-bit little-endian"),
    (b'\xca\xfe\xba\xbe', "Mach-O universal binary or Java class file"),
)

_METADATA_KEYS = ('PatientID', 'StudyDate', 'Modality', 'SeriesInstanceUID')

SKIPPED_DICOM_FILES = []
_SKIPPED_DICOM_FILES_LOCK = Lock()

BASE64_CHAR_BYTES = set(ord(c) for c in (string.ascii_letters + string.digits + '+/='))

PYTHON_BYTECODE_MAGICS = (
    b'\x03\xf3\r\n',  # Python 2.7
    b'\x42\x0d\r\n',  # Python 3.7
    b'\x55\x0d\r\n',  # Python 3.8
    b'\x61\x0d\r\n',  # Python 3.9
    b'\x6f\x0d\r\n',  # Python 3.10
    b'\xa7\x0d\r\n',  # Python 3.11
    b'\xcb\x0d\r\n',  # Python 3.12
)

SCANNABLE_PRIVATE_VRS = {
    'OB', 'OW', 'UT', 'ST', 'LT', 'UN',
    'UC', 'UR', 'OF', 'OD', 'OL', 'OV', 'SV', 'UV',
}
MAX_PRIVATE_TAG_SCAN_LENGTH = 1024 * 1024
PRIVATE_TAG_SAMPLE_BYTES = 128
MAX_TRAVERSED_ELEMENTS = 10000

# Signatures used for XOR-obfuscation detection. Keep MZ checks at 4 bytes
# to reduce false positives from 2-byte matches.
XOR_OBFUSCATION_SIGNATURES = (
    b'\x7fELF',
    b'MZ\x90\x00',
    b'MZ\x00\x00',
    b'\xfe\xed\xfa\xce',
    b'\xce\xfa\xed\xfe',
    b'\xfe\xed\xfa\xcf',
    b'\xcf\xfa\xed\xfe',
    b'\xca\xfe\xba\xbe',
)


class MaliciousDicomError(Exception):
    """Raised when a DICOM file contains malicious executable content in its preamble."""
    pass


def validate_dicom_preamble_from_data(header: bytes) -> bool:
    """
    Validate DICOM preamble from header data for malicious executable headers.
    
    Args:
        header: First 132 bytes of DICOM file (128-byte preamble + 4-byte DICM magic)
        
    Returns:
        True if preamble is safe, False if malicious content detected
        
    Raises:
        MaliciousDicomError: If malicious executable content detected
    """
    if len(header) < 132:
        # File too small to be valid DICOM
        return False
        
    preamble = header[:128]
    magic = header[128:132]
    
    # Verify DICM magic number at correct position
    if magic != b'DICM':
        return False
        
    # Check for executable signatures in preamble
    malicious_signatures = _detect_executable_signatures(preamble)
    
    if malicious_signatures:
        error_msg = f"Malicious executable content detected in DICOM preamble: {', '.join(malicious_signatures)}"
        LOG.error("SECURITY ALERT: %s", error_msg)
        raise MaliciousDicomError(error_msg)
        
    # Additional validation for advanced evasion techniques
    if _detect_advanced_evasion(preamble):
        error_msg = "Advanced evasion technique detected in DICOM preamble"
        LOG.error("SECURITY ALERT: %s", error_msg)
        raise MaliciousDicomError(error_msg)
        
    return True


def validate_dicom_preamble(file_path: Union[str, PathLike]) -> bool:
    """
    Validate DICOM file preamble for malicious executable headers.
    
    DICOM files have a 128-byte preamble followed by "DICM" magic bytes.
    Attackers can embed executable headers (PE, ELF, Mach-O) in the preamble
    to create polyglot files that are both valid DICOM and executable malware.
    
    This function detects common executable signatures in the preamble:
    - Windows PE files (MZ signature)
    - Linux ELF files (ELF signature) 
    - macOS Mach-O files (various magic numbers)
    - Advanced evasion techniques
    
    Args:
        file_path: Path to DICOM file to validate
        
    Returns:
        True if preamble is safe, False if malicious content detected
        
    Raises:
        OSError: If file cannot be read
        MaliciousDicomError: If malicious executable content detected
    """
    try:
        with open(file_path, 'rb') as f:
            # Read first 132 bytes (128-byte preamble + 4-byte DICM magic)
            header = f.read(132)
            return validate_dicom_preamble_from_data(header)
            
    except OSError as e:
        LOG.error("Failed to read DICOM file for preamble validation: %s (%s)", file_path, e)
        raise


def _detect_executable_signatures(preamble: bytes) -> list[str]:
    """
    Detect known executable signatures in DICOM preamble.
    
    Args:
        preamble: 128-byte preamble data
        
    Returns:
        List of detected executable types
    """
    detected = []
    
    # Windows PE executable signatures
    if preamble.startswith(b'MZ'):
        # Prefer a more specific message when DOS header structure looks valid.
        if len(preamble) >= 64:
            try:
                e_lfanew = struct.unpack('<I', preamble[60:64])[0]
                if 0 < e_lfanew < 1024:  # Reasonable PE header offset
                    detected.append("Windows PE (MZ, DOS header)")
                else:
                    detected.append("Windows PE (MZ)")
            except struct.error:
                detected.append("Windows PE (MZ)")
        else:
            detected.append("Windows PE (MZ)")
    
    else:
        # Check for PE signature at various offsets (common in packed executables)
        for offset in range(1, len(preamble) - 1):
            if preamble[offset:offset+2] == b'MZ':
                # Reduce false positives: validate nearby DOS header markers.
                if len(preamble) >= offset + 64:
                    try:
                        e_lfanew = struct.unpack('<I', preamble[offset + 60:offset + 64])[0]
                        if 0 < e_lfanew < 1024:
                            detected.append(f"Windows PE (MZ, DOS header at offset {offset})")
                            break
                    except struct.error:
                        pass

                if preamble[offset + 2:offset + 4] in (b'\x90\x00', b'\x00\x00'):
                    detected.append(f"Windows PE (MZ at offset {offset})")
                    break
    
    # Linux ELF executable signatures - check at start and various offsets
    if preamble.startswith(b'\x7fELF'):
        detected.append("Linux ELF")
    
    else:
        # Check for ELF at other positions (less common but possible)
        for offset in range(1, len(preamble) - 3):
            if preamble[offset:offset+4] == b'\x7fELF':
                detected.append(f"Linux ELF (at offset {offset})")
                break
    
    # macOS Mach-O executable signatures (32-bit and 64-bit, big/little endian)
    for signature, name in MACHO_SIGNATURES:
        offset = preamble.find(signature)
        if offset != -1:
            if offset == 0:
                detected.append(name)
            else:
                detected.append(f"{name} (at offset {offset})")
    
    # Shell scripts (Unix)
    if preamble.startswith(b'#!/'):
        detected.append("Shell script")
    
    # Python bytecode
    if any(preamble.startswith(magic) for magic in PYTHON_BYTECODE_MAGICS):
        detected.append("Python bytecode")
        
    return detected


def _detect_advanced_evasion(preamble: bytes) -> bool:
    """
    Detect advanced evasion techniques in DICOM preamble.
    
    Args:
        preamble: 128-byte preamble data
        
    Returns:
        True if evasion techniques detected
    """
    # Check for suspicious patterns that might indicate obfuscated executables
    
    # 1. High entropy regions (possible packed/encrypted content)
    entropy = _calculate_entropy(preamble)
    if entropy > 6.0:  # Adjusted threshold for high entropy detection
        return True
    
    # 2. Repeated patterns that might hide executable headers
    if _has_suspicious_patterns(preamble):
        return True
        
    return False


def _calculate_entropy(data: bytes) -> float:
    """Calculate Shannon entropy of byte data."""
    if not data:
        return 0.0
        
    # Count byte frequencies
    byte_counts = [0] * 256
    for byte in data:
        byte_counts[byte] += 1
    
    # Calculate entropy
    entropy = 0.0
    data_len = len(data)
    
    for count in byte_counts:
        if count > 0:
            probability = count / data_len
            entropy -= probability * math.log(probability, 2)
    
    return entropy


def _has_suspicious_patterns(preamble: bytes) -> bool:
    """Check for suspicious repeating patterns that might hide executable content."""
    # Look for XOR-obfuscated executable signatures by deriving the candidate
    # key from each offset and verifying full signature bytes.
    for signature in XOR_OBFUSCATION_SIGNATURES:
        sig_len = len(signature)
        if len(preamble) < sig_len:
            continue

        limit = len(preamble) - sig_len + 1
        for i in range(limit):
            key = preamble[i] ^ signature[0]
            if key == 0:
                continue

            if all(preamble[i + j] == (signature[j] ^ key) for j in range(1, sig_len)):
                return True

    # Check for base64-like patterns
    # Count base64 characters, but only in non-null regions
    non_null_bytes = [b for b in preamble if b != 0]
    if len(non_null_bytes) > 0:
        b64_count = sum(1 for b in non_null_bytes if b in BASE64_CHAR_BYTES)
        if len(non_null_bytes) > 32 and b64_count > len(non_null_bytes) * 0.8:  # >80% base64 characters
            return True
        
    return False


def safe_load_dicom_file(
    file_path: Union[str, PathLike],
    *,
    scan_private_tags: bool = True,
) -> Optional[pydicom.Dataset]:
    """
    Safely load a DICOM file from disk with security validation.

    This function performs comprehensive security validation before loading:
    1. Validates DICOM preamble for malicious executable content
    2. Detects Windows PE, Linux ELF, macOS Mach-O executables
    3. Identifies advanced evasion techniques (obfuscation, packing)
    4. Prevents CVE-2019-11687 exploitation

    Returns the pydicom Dataset for valid files or None when invalid/malformed.

    Args:
        file_path: Path to DICOM file to load
        scan_private_tags: Enable recursive private-tag signature checks.
            Set to False for metadata-only fast path if you only need
            preamble CVE-2019-11687 protection.
        
    Returns:
        pydicom.Dataset if file is safe and valid, None otherwise
        
    Raises:
        MaliciousDicomError: If malicious executable content detected

    Security Note:
        This addresses CVE-2019-11687 where attackers can embed executable
        malware in the 128-byte DICOM preamble to create polyglot files.
    """
    try:
        # Open file once and read header for validation
        with open(file_path, 'rb') as f:
            # Read first 132 bytes for preamble validation
            header = f.read(132)
            
            # Validate preamble for security threats
            if not validate_dicom_preamble_from_data(header):
                LOG.warning("DICOM preamble validation failed for file: %s", file_path)
                return None
            
            # Reset file pointer to beginning for pydicom
            f.seek(0)
            
            # Load DICOM metadata only (avoid pixel payload memory overhead).
            dataset = pydicom.dcmread(f, stop_before_pixels=True)
        
        # Additional post-load validation
        if not _validate_dicom_structure(dataset, scan_private_tags=scan_private_tags):
            LOG.warning("DICOM structure validation failed for file: %s", file_path)
            return None
            
        return dataset
        
    except MaliciousDicomError:
        # Re-raise security exceptions (don't suppress them)
        raise
    except (pydicom.errors.InvalidDicomError,
            EOFError,
            ValueError,
            OSError) as ex:
        with _SKIPPED_DICOM_FILES_LOCK:
            SKIPPED_DICOM_FILES.append({
                "file_path": str(file_path),
                "reason": type(ex).__name__,
                "message": str(ex),
            })

        LOG.warning("Skipping invalid or corrupted DICOM file: %s (%s)", file_path, ex)
        return None

def _validate_dicom_structure(dataset: pydicom.Dataset, *, scan_private_tags: bool = True) -> bool:
    """
    Validate basic DICOM dataset structure for additional security.
    
    Args:
        dataset: Loaded pydicom Dataset
        
    Returns:
        True if structure is valid, False otherwise
    """
    try:
        # Check for required DICOM elements
        if not hasattr(dataset, 'file_meta'):
            return False
            
        # Keep transfer syntax compatibility broad to support private syntaxes
        # used by imaging vendors.
        
        if not scan_private_tags:
            return True

        # Iteratively inspect private tags, including nested Sequence items,
        # but only with explicit signature checks and bounded reads.
        traversed = 0
        for elem in _iter_dataset_elements(dataset):
            traversed += 1
            if traversed > MAX_TRAVERSED_ELEMENTS:
                LOG.warning(
                    "Private-tag scan budget exceeded (%d elements); stopping deep scan",
                    MAX_TRAVERSED_ELEMENTS,
                )
                break

            if not elem.tag.is_private:
                continue

            if getattr(elem, 'VR', None) not in SCANNABLE_PRIVATE_VRS:
                continue

            elem_length = _get_element_length(elem)
            if elem_length is not None and elem_length > MAX_PRIVATE_TAG_SCAN_LENGTH:
                continue

            for sample in _iter_private_value_samples(elem.value):
                if len(sample) < 2:
                    continue

                malicious_sigs = _detect_executable_signatures(sample)
                if malicious_sigs:
                    error_msg = f"Malicious content detected in private tag {elem.tag}: {', '.join(malicious_sigs)}"
                    LOG.error("SECURITY ALERT: %s", error_msg)
                    raise MaliciousDicomError(error_msg)
                
        return True
        
    except (AttributeError, KeyError, TypeError) as e:
        LOG.warning("DICOM structure validation error: %s", e)
        return False


def get_skipped_dicom_files():
    """Return a list of structured details for all skipped invalid or corrupted DICOM files."""
    return list(SKIPPED_DICOM_FILES)


def clear_skipped_dicom_files():
    """Clear the list of skipped DICOM files, resetting the internal tracker."""
    with _SKIPPED_DICOM_FILES_LOCK:
        SKIPPED_DICOM_FILES.clear()

def extract_basic_metadata(file_path: Union[str, PathLike]):
    return normalize_metadata(safe_load_dicom_file(file_path))


def normalize_metadata(dataset_or_dict):
    """Normalize DICOM metadata for Dataset/dict inputs."""
    if dataset_or_dict is None:
        return {key: None for key in _METADATA_KEYS}

    getter = dataset_or_dict.get if hasattr(dataset_or_dict, 'get') else None
    if getter is None:
        return {key: None for key in _METADATA_KEYS}

    return {key: getter(key, None) for key in _METADATA_KEYS}


def _iter_dataset_elements(dataset: pydicom.Dataset):
    """Yield all elements from dataset and nested Sequence items iteratively."""
    stack = [dataset]
    while stack:
        current = stack.pop()
        for elem in current:
            yield elem

            if getattr(elem, 'VR', None) == 'SQ':
                sequence_items = elem.value if isinstance(elem.value, Sequence) else []
                for item in reversed(sequence_items):
                    if isinstance(item, pydicom.Dataset):
                        stack.append(item)


def _get_element_length(elem) -> Optional[int]:
    """Best-effort element length lookup without forcing value conversion."""
    for attr in ('length', 'VL'):
        value = getattr(elem, attr, None)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _iter_private_value_samples(value):
    """Yield bytes samples from single or multi-valued private-tag content."""
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = (value,)

    for item in items:
        if isinstance(item, str):
            yield item[:PRIVATE_TAG_SAMPLE_BYTES].encode('utf-8', errors='ignore')
        elif isinstance(item, (bytes, bytearray)):
            yield bytes(item[:PRIVATE_TAG_SAMPLE_BYTES])
