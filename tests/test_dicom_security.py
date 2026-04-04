"""
Security tests for DICOM preamble malware detection.

Tests the comprehensive security validation system that prevents
CVE-2019-11687 exploitation by detecting malicious executable
content in DICOM file preambles.
"""

import pytest
import tempfile
import os
from pathlib import Path
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import UID

from Diomedex.utils.dicom_helpers import (
    safe_load_dicom_file,
    validate_dicom_preamble,
    MaliciousDicomError,
    _detect_executable_signatures,
    _detect_advanced_evasion,
    _calculate_entropy,
    _has_suspicious_patterns,
    _has_embedded_content_in_nulls
)


class TestDicomPreambleSecurity:
    """Test DICOM preamble security validation."""

    def create_test_dicom_file(self, preamble_content: bytes, valid_dicom: bool = True) -> str:
        """Create a test DICOM file with custom preamble content."""
        fd, temp_path = tempfile.mkstemp(suffix='.dcm')
        os.close(fd)

        try:
            # Write custom preamble (must be exactly 128 bytes)
            if len(preamble_content) < 128:
                preamble_content += b'\x00' * (128 - len(preamble_content))
            elif len(preamble_content) > 128:
                preamble_content = preamble_content[:128]

            if valid_dicom:
                file_meta = FileMetaDataset()
                file_meta.MediaStorageSOPClassUID = UID('1.2.840.10008.5.1.4.1.1.2')
                file_meta.MediaStorageSOPInstanceUID = pydicom.uid.generate_uid()
                file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

                ds = FileDataset(temp_path, {}, file_meta=file_meta, preamble=preamble_content)
                ds.PatientID = "TEST1234"
                ds.StudyInstanceUID = "1.2.3.4.5.6"
                ds.Modality = "CT"
                ds.save_as(temp_path, little_endian=True, implicit_vr=False)
            else:
                with open(temp_path, 'wb') as f:
                    f.write(preamble_content)
                    f.write(b'XXXX')

        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

        return temp_path

    def test_clean_dicom_file_passes(self):
        """Test that clean DICOM files pass validation."""
        clean_preamble = b'\x00' * 128
        temp_file = self.create_test_dicom_file(clean_preamble)
        
        try:
            assert validate_dicom_preamble(temp_file) is True
            dataset = safe_load_dicom_file(temp_file)
            assert dataset is not None
        finally:
            os.unlink(temp_file)

    def test_windows_pe_malware_detected(self):
        """Test detection of Windows PE executable in preamble."""
        # Create PE header (MZ signature + minimal DOS header)
        pe_header = b'MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00'
        pe_header += b'\xb8\x00\x00\x00\x00\x00\x00\x00\x40\x00\x00\x00\x00\x00\x00\x00'
        pe_header += b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
        pe_header += b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x80\x00\x00\x00'
        
        temp_file = self.create_test_dicom_file(pe_header)
        
        try:
            with pytest.raises(MaliciousDicomError, match="Windows PE"):
                validate_dicom_preamble(temp_file)
                
            with pytest.raises(MaliciousDicomError):
                safe_load_dicom_file(temp_file)
        finally:
            os.unlink(temp_file)

    def test_linux_elf_malware_detected(self):
        """Test detection of Linux ELF executable in preamble."""
        # ELF header signature
        elf_header = b'\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00'
        elf_header += b'\x02\x00\x3e\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
        
        temp_file = self.create_test_dicom_file(elf_header)
        
        try:
            with pytest.raises(MaliciousDicomError, match="Linux ELF"):
                validate_dicom_preamble(temp_file)
                
            with pytest.raises(MaliciousDicomError):
                safe_load_dicom_file(temp_file)
        finally:
            os.unlink(temp_file)

    def test_macos_macho_malware_detected(self):
        """Test detection of macOS Mach-O executable in preamble."""
        # Mach-O 64-bit little-endian header
        macho_header = b'\xcf\xfa\xed\xfe\x07\x00\x00\x01\x03\x00\x00\x00\x02\x00\x00\x00'
        
        temp_file = self.create_test_dicom_file(macho_header)
        
        try:
            with pytest.raises(MaliciousDicomError, match="Mach-O"):
                validate_dicom_preamble(temp_file)
        finally:
            os.unlink(temp_file)

    def test_java_class_file_detected(self):
        """Test detection of Java class file in preamble."""
        java_header = b'\xca\xfe\xba\xbe\x00\x00\x00\x34'
        
        temp_file = self.create_test_dicom_file(java_header)
        
        try:
            with pytest.raises(MaliciousDicomError, match="Java class file"):
                validate_dicom_preamble(temp_file)
        finally:
            os.unlink(temp_file)

    def test_shell_script_detected(self):
        """Test detection of shell script in preamble."""
        shell_header = b'#!/bin/bash\necho "malicious code"\n'
        
        temp_file = self.create_test_dicom_file(shell_header)
        
        try:
            with pytest.raises(MaliciousDicomError, match="Shell script"):
                validate_dicom_preamble(temp_file)
        finally:
            os.unlink(temp_file)

    def test_pe_at_offset_detected(self):
        """Test detection of PE header at non-zero offset."""
        # PE header embedded at offset 16
        offset_pe = b'\x00' * 16 + b'MZ\x90\x00'
        
        temp_file = self.create_test_dicom_file(offset_pe)
        
        try:
            with pytest.raises(MaliciousDicomError, match="Windows PE.*offset"):
                validate_dicom_preamble(temp_file)
        finally:
            os.unlink(temp_file)

    def test_high_entropy_evasion_detected(self):
        """Test detection of high entropy content (possible encryption/packing)."""
        # Generate high entropy data (pseudo-random)
        import random
        random.seed(42)  # Reproducible test
        high_entropy = bytes(random.randint(0, 255) for _ in range(128))
        
        temp_file = self.create_test_dicom_file(high_entropy)
        
        try:
            with pytest.raises(MaliciousDicomError, match="Advanced evasion"):
                validate_dicom_preamble(temp_file)
        finally:
            os.unlink(temp_file)

    def test_xor_obfuscated_pe_detected(self):
        """Test detection of XOR-obfuscated PE header."""
        # XOR-encoded MZ header (key = 0x42)
        original_pe = b'MZ\x90\x00'
        xor_key = 0x42
        obfuscated = bytes(b ^ xor_key for b in original_pe)
        obfuscated += b'\x00' * (128 - len(obfuscated))
        
        temp_file = self.create_test_dicom_file(obfuscated)
        
        try:
            with pytest.raises(MaliciousDicomError, match="Advanced evasion"):
                validate_dicom_preamble(temp_file)
        finally:
            os.unlink(temp_file)

    def test_embedded_pe_in_nulls_detected(self):
        """Test detection of PE header embedded within null bytes."""
        # PE header surrounded by nulls
        embedded = b'\x00' * 32 + b'MZ\x90\x00\x03\x00' + b'\x00' * (128 - 38)
        
        temp_file = self.create_test_dicom_file(embedded)
        
        try:
            with pytest.raises(MaliciousDicomError, match="Windows PE.*offset"):
                validate_dicom_preamble(temp_file)
        finally:
            os.unlink(temp_file)

    def test_invalid_dicom_magic_rejected(self):
        """Test that files without proper DICM magic are rejected."""
        clean_preamble = b'\x00' * 128
        temp_file = self.create_test_dicom_file(clean_preamble, valid_dicom=False)
        
        try:
            assert validate_dicom_preamble(temp_file) is False
            dataset = safe_load_dicom_file(temp_file)
            assert dataset is None
        finally:
            os.unlink(temp_file)

    def test_file_too_small_rejected(self):
        """Test that files smaller than 132 bytes are rejected."""
        fd, temp_path = tempfile.mkstemp(suffix='.dcm')
        
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(b'small')  # Only 5 bytes
                
            assert validate_dicom_preamble(temp_path) is False
            dataset = safe_load_dicom_file(temp_path)
            assert dataset is None
        finally:
            os.unlink(temp_path)

    def test_nonexistent_file_raises_error(self):
        """Test that nonexistent files raise appropriate errors."""
        with pytest.raises(OSError):
            validate_dicom_preamble("/nonexistent/file.dcm")
            
        dataset = safe_load_dicom_file("/nonexistent/file.dcm")
        assert dataset is None


class TestExecutableSignatureDetection:
    """Test executable signature detection functions."""

    def test_detect_pe_signatures(self):
        """Test PE signature detection."""
        pe_preamble = b'MZ\x90\x00' + b'\x00' * 124
        signatures = _detect_executable_signatures(pe_preamble)
        assert any("Windows PE" in sig for sig in signatures)

    def test_detect_elf_signatures(self):
        """Test ELF signature detection."""
        elf_preamble = b'\x7fELF' + b'\x00' * 124
        signatures = _detect_executable_signatures(elf_preamble)
        assert any("Linux ELF" in sig for sig in signatures)

    def test_detect_macho_signatures(self):
        """Test Mach-O signature detection."""
        macho_preambles = [
            b'\xfe\xed\xfa\xce',  # 32-bit big-endian
            b'\xce\xfa\xed\xfe',  # 32-bit little-endian
            b'\xfe\xed\xfa\xcf',  # 64-bit big-endian
            b'\xcf\xfa\xed\xfe',  # 64-bit little-endian
        ]
        
        for macho_header in macho_preambles:
            preamble = macho_header + b'\x00' * 124
            signatures = _detect_executable_signatures(preamble)
            assert any("Mach-O" in sig for sig in signatures)

    def test_detect_multiple_signatures(self):
        """Test detection when multiple signatures are present."""
        # Unlikely but possible - PE header followed by ELF at offset
        mixed_preamble = b'MZ\x90\x00' + b'\x00' * 4 + b'\x7fELF' + b'\x00' * 116
        signatures = _detect_executable_signatures(mixed_preamble)
        assert len(signatures) >= 2
        assert any("Windows PE" in sig for sig in signatures)
        assert any("Linux ELF" in sig for sig in signatures)

    def test_clean_preamble_no_signatures(self):
        """Test that clean preamble returns no signatures."""
        clean_preamble = b'\x00' * 128
        signatures = _detect_executable_signatures(clean_preamble)
        assert len(signatures) == 0


class TestAdvancedEvasionDetection:
    """Test advanced evasion technique detection."""

    def test_entropy_calculation(self):
        """Test entropy calculation function."""
        # Low entropy (all zeros)
        low_entropy_data = b'\x00' * 128
        entropy = _calculate_entropy(low_entropy_data)
        assert entropy < 1.0
        
        # High entropy (random data)
        import random
        random.seed(42)
        high_entropy_data = bytes(random.randint(0, 255) for _ in range(128))
        entropy = _calculate_entropy(high_entropy_data)
        assert entropy > 6.0

    def test_high_entropy_detection(self):
        """Test high entropy evasion detection."""
        import random
        random.seed(42)
        high_entropy_data = bytes(random.randint(0, 255) for _ in range(128))
        assert _detect_advanced_evasion(high_entropy_data) is True
        
        low_entropy_data = b'\x00' * 128
        assert _detect_advanced_evasion(low_entropy_data) is False

    def test_xor_pattern_detection(self):
        """Test XOR obfuscation pattern detection."""
        # XOR-encoded MZ header
        original = b'MZ\x90\x00'
        xor_key = 0x42
        obfuscated = bytes(b ^ xor_key for b in original) + b'\x00' * 124
        
        assert _has_suspicious_patterns(obfuscated) is True

    def test_embedded_content_detection(self):
        """Test detection of executable content embedded in nulls."""
        # PE header embedded in null bytes
        embedded = b'\x00' * 32 + b'MZ\x90\x00' + b'\x00' * 92
        assert _has_embedded_content_in_nulls(embedded) is True
        
        # Clean null-padded content
        clean = b'\x00' * 128
        assert _has_embedded_content_in_nulls(clean) is False

    def test_base64_pattern_detection(self):
        """Test base64-like pattern detection."""
        # High concentration of base64 characters
        b64_like = b'SGVsbG8gV29ybGQhIFRoaXMgaXMgYSB0ZXN0IG1lc3NhZ2UgZm9yIGJhc2U2NCBkZXRlY3Rpb24='
        b64_like += b'\x00' * (128 - len(b64_like))
        
        assert _has_suspicious_patterns(b64_like) is True


class TestDicomStructureValidation:
    """Test DICOM structure validation."""

    def test_private_transfer_syntax_is_allowed(self):
        """Test that private transfer syntax UIDs are accepted for compatibility."""
        import pydicom
        from pydicom.dataset import FileDataset, FileMetaDataset
        from pydicom.uid import UID

        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = UID('1.2.840.10008.5.1.4.1.1.2')
        file_meta.MediaStorageSOPInstanceUID = UID('1.2.3.4.5.7')
        file_meta.TransferSyntaxUID = UID('1.2.840.113619.5.2')

        fd, temp_path = tempfile.mkstemp(suffix='.dcm')
        os.close(fd)

        try:
            ds = FileDataset(temp_path, {}, file_meta=file_meta, preamble=b'\x00' * 128)
            ds.PatientID = "TESTPRIVATE"
            ds.StudyInstanceUID = "1.2.3.4.5.6.7"
            ds.Modality = "CT"
            ds.save_as(temp_path)

            dataset = safe_load_dicom_file(temp_path)
            assert dataset is not None
            assert dataset.PatientID == "TESTPRIVATE"

        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_malicious_string_private_tag_detected(self):
        """Test detection when private tags contain suspicious string payloads."""
        import pydicom
        from pydicom.dataset import FileDataset, FileMetaDataset
        from pydicom.uid import UID

        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = UID('1.2.840.10008.5.1.4.1.1.2')
        file_meta.MediaStorageSOPInstanceUID = UID('1.2.3.4.5.8')
        file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

        fd, temp_path = tempfile.mkstemp(suffix='.dcm')
        os.close(fd)

        try:
            ds = FileDataset(temp_path, {}, file_meta=file_meta, preamble=b'\x00' * 128)
            ds.PatientID = "TESTSTR"
            ds.StudyInstanceUID = "1.2.3.4.5.6.8"
            ds.Modality = "CT"
            ds.add_new((0x0043, 0x1010), 'UT', 'MZ' + ('A' * 200))
            ds.save_as(temp_path)

            with pytest.raises(MaliciousDicomError, match="private tag"):
                safe_load_dicom_file(temp_path)

        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_valid_dicom_structure_passes(self):
        """Test that valid DICOM structure passes validation."""
        # This test requires a real DICOM file, so we'll create a minimal one
        import pydicom
        from pydicom.dataset import FileDataset, FileMetaDataset
        from pydicom.uid import UID
        
        # Create minimal valid DICOM
        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = UID('1.2.840.10008.5.1.4.1.1.2')
        file_meta.MediaStorageSOPInstanceUID = UID('1.2.3.4.5')
        file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
        
        fd, temp_path = tempfile.mkstemp(suffix='.dcm')
        os.close(fd)
        
        try:
            ds = FileDataset(temp_path, {}, file_meta=file_meta, preamble=b'\x00' * 128)
            ds.PatientID = "TEST123"
            ds.StudyInstanceUID = "1.2.3.4.5.6"
            ds.Modality = "CT"
            ds.save_as(temp_path)
            
            # Test loading with security validation
            dataset = safe_load_dicom_file(temp_path)
            assert dataset is not None
            assert dataset.PatientID == "TEST123"
            
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])