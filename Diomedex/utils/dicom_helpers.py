import logging
import math
import string
import struct
from os import PathLike
from typing import Union, Optional

import pydicom

LOG = logging.getLogger(__name__)


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
        detected.append("Windows PE (MZ)")
    
    # Check for PE signature at various offsets (common in packed executables)
    for offset in range(1, min(64, len(preamble) - 2)):
        if preamble[offset:offset+2] == b'MZ':
            detected.append(f"Windows PE (MZ at offset {offset})")
            break
    
    # Linux ELF executable signatures - check at start and various offsets
    if preamble.startswith(b'\x7fELF'):
        detected.append("Linux ELF")
    
    # Check for ELF at other positions (less common but possible)
    for offset in range(1, min(32, len(preamble) - 4)):
        if preamble[offset:offset+4] == b'\x7fELF':
            detected.append(f"Linux ELF (at offset {offset})")
            break
    
    # macOS Mach-O executable signatures (32-bit and 64-bit, big/little endian)
    macho_signatures = [
        (b'\xfe\xed\xfa\xce', "Mach-O 32-bit big-endian"),
        (b'\xce\xfa\xed\xfe', "Mach-O 32-bit little-endian"), 
        (b'\xfe\xed\xfa\xcf', "Mach-O 64-bit big-endian"),
        (b'\xcf\xfa\xed\xfe', "Mach-O 64-bit little-endian"),
        (b'\xca\xfe\xba\xbe', "Mach-O universal binary or Java class file"),
    ]
    
    for signature, name in macho_signatures:
        if preamble.startswith(signature):
            detected.append(name)
    
    # DOS executable
    if preamble.startswith(b'MZ') and len(preamble) >= 64:
        # Check for DOS signature patterns
        try:
            # DOS header structure check
            e_lfanew = struct.unpack('<I', preamble[60:64])[0]
            if 0 < e_lfanew < 1024:  # Reasonable PE header offset
                detected.append("DOS/PE executable")
        except (struct.error, IndexError):
            pass
    
    # Shell scripts (Unix)
    if preamble.startswith(b'#!/'):
        detected.append("Shell script")
    
    # Python bytecode
    if preamble.startswith(b'\x03\xf3\r\n') or preamble.startswith(b'\x42\x0d\r\n'):
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
        
    # 3. Null byte padding with embedded content
    if _has_embedded_content_in_nulls(preamble):
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
    # Look for XOR patterns (common obfuscation technique)
    for key in range(1, 256):
        decoded = bytes(b ^ key for b in preamble[:32])
        if decoded.startswith(b'MZ') or decoded.startswith(b'\x7fELF'):
            return True
    
    # Check for base64-like patterns
    b64_chars = set(string.ascii_letters + string.digits + '+/=')
    
    # Count base64 characters, but only in non-null regions
    non_null_bytes = [b for b in preamble if b != 0]
    if len(non_null_bytes) > 0:
        b64_count = sum(1 for b in non_null_bytes if chr(b) in b64_chars)
        if len(non_null_bytes) > 32 and b64_count > len(non_null_bytes) * 0.8:  # >80% base64 characters
            return True
        
    return False


def _has_embedded_content_in_nulls(preamble: bytes) -> bool:
    """Check for executable content embedded within null byte padding."""
    # Split on null bytes and check non-null segments
    segments = preamble.split(b'\x00')
    
    for segment in segments:
        if len(segment) >= 2:  # Minimum size for meaningful executable signature
            # Check if segment contains executable signatures
            if (segment.startswith(b'MZ') or 
                segment.startswith(b'\x7fELF') or
                any(segment.startswith(sig) for sig in [
                    b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe',
                    b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe'
                ])):
                return True
                
    return False


def safe_load_dicom_file(file_path: Union[str, PathLike]) -> Optional[pydicom.Dataset]:
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
            
            # Load DICOM dataset from file object
            dataset = pydicom.dcmread(f)
        
        # Additional post-load validation
        if not _validate_dicom_structure(dataset):
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
        LOG.warning("Skipping invalid or corrupted DICOM file: %s (%s)", file_path, ex)
        return None


def _validate_dicom_structure(dataset: pydicom.Dataset) -> bool:
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
            
        # Validate transfer syntax
        if hasattr(dataset.file_meta, 'TransferSyntaxUID'):
            transfer_syntax = str(dataset.file_meta.TransferSyntaxUID)
            # Ensure it's a known DICOM transfer syntax
            if not transfer_syntax.startswith('1.2.840.10008.1.2'):
                LOG.warning("Suspicious transfer syntax: %s", transfer_syntax)
                return False
        
        # Check suspicious private binary payloads while avoiding false positives
        # from legitimate large metadata values.
        for tag in dataset.keys():
            if not tag.is_private:
                continue

            value = dataset[tag].value
            if not isinstance(value, (bytes, bytearray)):
                continue

            if len(value) < 128:
                continue

            sample = bytes(value[:128])
            if _detect_executable_signatures(sample) or _detect_advanced_evasion(sample):
                LOG.warning("Suspicious private tag payload detected: %s", tag)
                return False
                
        return True
        
    except (AttributeError, KeyError, TypeError) as e:
        LOG.warning("DICOM structure validation error: %s", e)
        return False


def extract_basic_metadata(file_path: Union[str, PathLike]):
    dataset = safe_load_dicom_file(file_path)
    if dataset is None:
        return {
            'PatientID': None,
            'StudyDate': None,
            'Modality': None,
            'SeriesInstanceUID': None,
        }

    return {
        'PatientID': dataset.get('PatientID', None),
        'StudyDate': dataset.get('StudyDate', None),
        'Modality': dataset.get('Modality', None),
        'SeriesInstanceUID': dataset.get('SeriesInstanceUID', None),
    }
