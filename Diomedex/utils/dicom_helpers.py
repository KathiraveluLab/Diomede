import logging
import math
import string
import struct
from os import PathLike
from typing import Optional, Union

import pydicom

LOG = logging.getLogger(__name__)

_METADATA_KEYS = ('PatientID', 'StudyDate', 'Modality', 'SeriesInstanceUID')

MACHO_SIGNATURES = (
    (b'\xfe\xed\xfa\xce', 'Mach-O 32-bit big-endian'),
    (b'\xce\xfa\xed\xfe', 'Mach-O 32-bit little-endian'),
    (b'\xfe\xed\xfa\xcf', 'Mach-O 64-bit big-endian'),
    (b'\xcf\xfa\xed\xfe', 'Mach-O 64-bit little-endian'),
    (b'\xca\xfe\xba\xbe', 'Mach-O universal binary or Java class file'),
)

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

SCANNABLE_PRIVATE_VRS = {'OB', 'OW', 'UT', 'ST', 'LT', 'UN'}


class MaliciousDicomError(Exception):
    """Raised when malicious executable content is detected in DICOM data."""


def validate_dicom_preamble_from_data(header: bytes) -> bool:
    """
    Validate DICOM preamble bytes for malicious executable headers.

    Args:
        header: First 132 bytes of file (128-byte preamble + 4-byte DICM magic)

    Returns:
        True if preamble is safe, False if malformed/non-DICOM header

    Raises:
        MaliciousDicomError: If malicious executable content is detected
    """
    if len(header) < 132:
        return False

    preamble = header[:128]
    magic = header[128:132]

    if magic != b'DICM':
        return False

    malicious_signatures = _detect_executable_signatures(preamble)
    if malicious_signatures:
        error_msg = (
            'Malicious executable content detected in DICOM preamble: '
            + ', '.join(malicious_signatures)
        )
        LOG.error('SECURITY ALERT: %s', error_msg)
        raise MaliciousDicomError(error_msg)

    if _detect_advanced_evasion(preamble):
        error_msg = 'Advanced evasion technique detected in DICOM preamble'
        LOG.error('SECURITY ALERT: %s', error_msg)
        raise MaliciousDicomError(error_msg)

    return True


def validate_dicom_preamble(file_path: Union[str, PathLike]) -> bool:
    """
    Validate DICOM file preamble for malicious executable headers.

    Args:
        file_path: Path to DICOM file

    Returns:
        True if preamble is safe, False for malformed/non-DICOM header

    Raises:
        OSError: If file cannot be read
        MaliciousDicomError: If malicious content is detected
    """
    try:
        with open(file_path, 'rb') as f:
            header = f.read(132)
            return validate_dicom_preamble_from_data(header)
    except OSError as e:
        LOG.error('Failed to read DICOM file for preamble validation: %s (%s)', file_path, e)
        raise


def _detect_executable_signatures(preamble: bytes) -> list[str]:
    """Detect known executable signatures in preamble bytes."""
    detected: list[str] = []

    # Windows PE executable signatures
    if preamble.startswith(b'MZ'):
        if len(preamble) >= 64:
            try:
                e_lfanew = struct.unpack('<I', preamble[60:64])[0]
                if 0 < e_lfanew < 1024:
                    detected.append('Windows PE (MZ, DOS header)')
                else:
                    detected.append('Windows PE (MZ)')
            except struct.error:
                detected.append('Windows PE (MZ)')
        else:
            detected.append('Windows PE (MZ)')
    else:
        for offset in range(1, len(preamble) - 1):
            if preamble[offset : offset + 2] == b'MZ':
                # Reduce false positives for non-zero offsets by requiring either
                # DOS header structure plausibility or common marker bytes.
                if len(preamble) >= offset + 64:
                    try:
                        e_lfanew = struct.unpack('<I', preamble[offset + 60 : offset + 64])[0]
                        if 0 < e_lfanew < 1024:
                            detected.append(f'Windows PE (MZ, DOS header at offset {offset})')
                            break
                    except struct.error:
                        pass

                if preamble[offset + 2 : offset + 4] in (b'\x90\x00', b'\x00\x00'):
                    detected.append(f'Windows PE (MZ at offset {offset})')
                    break

    # Linux ELF executable signatures
    if preamble.startswith(b'\x7fELF'):
        detected.append('Linux ELF')
    else:
        for offset in range(1, len(preamble) - 3):
            if preamble[offset : offset + 4] == b'\x7fELF':
                detected.append(f'Linux ELF (at offset {offset})')
                break

    # macOS Mach-O signatures at any offset
    for signature, name in MACHO_SIGNATURES:
        offset = preamble.find(signature)
        if offset != -1:
            if offset == 0:
                detected.append(name)
            else:
                detected.append(f'{name} (at offset {offset})')

    if preamble.startswith(b'#!/'):
        detected.append('Shell script')

    if any(preamble.startswith(magic) for magic in PYTHON_BYTECODE_MAGICS):
        detected.append('Python bytecode')

    return detected


def _detect_advanced_evasion(preamble: bytes) -> bool:
    """Detect high-entropy and obfuscation patterns in the preamble."""
    entropy = _calculate_entropy(preamble)
    if entropy > 6.0:
        return True

    if _has_suspicious_patterns(preamble):
        return True

    return False


def _calculate_entropy(data: bytes) -> float:
    """Calculate Shannon entropy of bytes."""
    if not data:
        return 0.0

    byte_counts = [0] * 256
    for byte in data:
        byte_counts[byte] += 1

    entropy = 0.0
    data_len = len(data)
    for count in byte_counts:
        if count > 0:
            probability = count / data_len
            entropy -= probability * math.log(probability, 2)

    return entropy


def _has_suspicious_patterns(preamble: bytes) -> bool:
    """Check for suspicious obfuscation/base64-like patterns."""
    # XOR-obfuscated ELF header at any offset.
    elf_limit = len(preamble) - 3
    for i in range(elf_limit):
        key = preamble[i] ^ 0x7F
        if (
            key != 0
            and preamble[i + 1] == (ord('E') ^ key)
            and preamble[i + 2] == (ord('L') ^ key)
            and preamble[i + 3] == (ord('F') ^ key)
        ):
            return True

    # Base64-like dense payload in non-null bytes.
    non_null_bytes = [b for b in preamble if b != 0]
    if non_null_bytes:
        b64_count = sum(1 for b in non_null_bytes if b in BASE64_CHAR_BYTES)
        if len(non_null_bytes) > 32 and b64_count > len(non_null_bytes) * 0.8:
            return True

    return False


def safe_load_dicom_file(file_path: Union[str, PathLike]) -> Optional[pydicom.Dataset]:
    """
    Safely load a DICOM file from disk with preamble and structure validation.

    Returns:
        pydicom.Dataset when file is safe/valid, otherwise None

    Raises:
        MaliciousDicomError: For explicit malicious signature detections
    """
    try:
        with open(file_path, 'rb') as f:
            header = f.read(132)
            if not validate_dicom_preamble_from_data(header):
                LOG.warning('DICOM preamble validation failed for file: %s', file_path)
                return None

            f.seek(0)
            dataset = pydicom.dcmread(f)

        if not _validate_dicom_structure(dataset):
            LOG.warning('DICOM structure validation failed for file: %s', file_path)
            return None

        return dataset

    except MaliciousDicomError:
        raise
    except (pydicom.errors.InvalidDicomError, EOFError, ValueError, OSError) as ex:
        LOG.warning('Skipping invalid or corrupted DICOM file: %s (%s)', file_path, ex)
        return None


def _validate_dicom_structure(dataset: pydicom.Dataset) -> bool:
    """
    Validate DICOM dataset structure for security checks.

    Returns:
        True when structure looks acceptable, otherwise False
    """
    try:
        if not hasattr(dataset, 'file_meta'):
            return False

        # Keep transfer syntax compatibility broad to support private syntaxes.
        # Scan top-level private tags for explicit executable signatures only.
        for elem in dataset:
            if not elem.tag.is_private:
                continue

            if getattr(elem, 'VR', None) not in SCANNABLE_PRIVATE_VRS:
                continue

            value = elem.value
            if isinstance(value, str):
                value = value[:128].encode('utf-8', errors='ignore')
            if not isinstance(value, (bytes, bytearray)):
                continue
            if len(value) < 2:
                continue

            sample = value[:128]
            malicious_sigs = _detect_executable_signatures(sample)
            if malicious_sigs:
                error_msg = (
                    f'Malicious content detected in private tag {elem.tag}: '
                    + ', '.join(malicious_sigs)
                )
                LOG.error('SECURITY ALERT: %s', error_msg)
                raise MaliciousDicomError(error_msg)

        return True

    except (AttributeError, KeyError, TypeError) as e:
        LOG.warning('DICOM structure validation error: %s', e)
        return False


def normalize_metadata(dataset_or_dict):
    """Normalize DICOM metadata for Dataset/dict inputs."""
    if dataset_or_dict is None:
        return {key: None for key in _METADATA_KEYS}

    getter = dataset_or_dict.get if hasattr(dataset_or_dict, 'get') else None
    if getter is None:
        return {key: None for key in _METADATA_KEYS}

    return {key: getter(key, None) for key in _METADATA_KEYS}


def extract_basic_metadata(file_path: Union[str, PathLike]):
    return normalize_metadata(safe_load_dicom_file(file_path))
