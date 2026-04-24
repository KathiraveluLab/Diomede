import csv
import pytest
from Diomedex.albums.niffler_reader import load_niffler_csv, filter_metadata

SAMPLE_ROWS = [
    {
        "PatientID": "P001",
        "StudyInstanceUID": "1.2.840.10008.1",
        "Modality": "CT",
        "StudyDate": "20240101",
        "filepath": "/data/dicom/p001_ct.dcm",
    },
    {
        "PatientID": "P002",
        "StudyInstanceUID": "1.2.840.10008.2",
        "Modality": "MR",
        "StudyDate": "20240102",
        "filepath": "/data/dicom/p002_mr.dcm",
    },
    {
        "PatientID": "P001",
        "StudyInstanceUID": "1.2.840.10008.3",
        "Modality": "CT",
        "StudyDate": "20240103",
        "filepath": "/data/dicom/p001_ct2.dcm",
    },
    {
        "PatientID": "P003",
        "StudyInstanceUID": "1.2.840.10008.4",
        "Modality": "MR",
        "StudyDate": "20240104",
        "filepath": "/data/dicom/p003_mr.dcm",
    },
]


@pytest.fixture
def sample_csv(tmp_path):
    """Write SAMPLE_ROWS to a temp CSV file and return its path."""
    path = tmp_path / "niffler_output.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SAMPLE_ROWS[0].keys())
        writer.writeheader()
        writer.writerows(SAMPLE_ROWS)
    return str(path)


@pytest.fixture
def empty_csv(tmp_path):
    """CSV file with headers but no rows."""
    path = tmp_path / "empty.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SAMPLE_ROWS[0].keys())
        writer.writeheader()
    return str(path)

class TestLoadNifflerCsv:

    def test_loads_correct_number_of_records(self, sample_csv):
        records = list(load_niffler_csv(sample_csv))
        assert len(records) == 4

    def test_each_record_has_expected_fields(self, sample_csv):
        records = load_niffler_csv(sample_csv)
        for record in records:
            assert "PatientID" in record
            assert "StudyInstanceUID" in record
            assert "Modality" in record
            assert "StudyDate" in record
            assert "filepath" in record

    def test_first_record_values_are_correct(self, sample_csv):
        records = load_niffler_csv(sample_csv)
        assert records[0]["PatientID"] == "P001"
        assert records[0]["Modality"] == "CT"

    def test_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_niffler_csv("/does/not/exist/niffler.csv")

    def test_raises_value_error_for_missing_columns(self, tmp_path):
        bad_csv = tmp_path / "bad.csv"
        with open(bad_csv, "w") as f:
            f.write("SomeOtherColumn,AnotherColumn\nfoo,bar\n")
        with pytest.raises(ValueError, match="missing expected columns"):
            load_niffler_csv(str(bad_csv))

    def test_empty_csv_returns_empty_list(self, empty_csv):
        records = load_niffler_csv(empty_csv)
        assert records == []

class TestFilterMetadata:

    def test_filter_by_modality_ct(self, sample_csv):
        records = load_niffler_csv(sample_csv)
        result = filter_metadata(records, {"Modality": "CT"})
        assert len(result) == 2
        assert all(r["Modality"] == "CT" for r in result)

    def test_filter_by_modality_mr(self, sample_csv):
        records = load_niffler_csv(sample_csv)
        result = filter_metadata(records, {"Modality": "MR"})
        assert len(result) == 2
        assert all(r["Modality"] == "MR" for r in result)

    def test_filter_by_patient_id(self, sample_csv):
        records = load_niffler_csv(sample_csv)
        result = filter_metadata(records, {"PatientID": "P001"})
        assert len(result) == 2
        assert all(r["PatientID"] == "P001" for r in result)

    def test_filter_by_multiple_fields(self, sample_csv):
        records = load_niffler_csv(sample_csv)
        result = filter_metadata(records, {"PatientID": "P001", "Modality": "CT"})
        assert len(result) == 2

    def test_filter_no_matches_returns_empty(self, sample_csv):
        records = load_niffler_csv(sample_csv)
        result = filter_metadata(records, {"Modality": "XR"})
        assert result == []

    def test_filter_empty_filters_returns_all(self, sample_csv):
        records = load_niffler_csv(sample_csv)
        result = filter_metadata(records, {})
        assert len(result) == 4

    def test_filter_on_empty_records(self):
        result = filter_metadata([], {"Modality": "CT"})
        assert result == []

from Diomedex.albums.niffler_reader import to_album_index_format

class TestToAlbumIndexFormat:

    def test_maps_filepath_to_path(self):
        records = [{"filepath": "/data/p001.dcm", "PatientID": "P001",
                    "StudyInstanceUID": "1.2.3", "Modality": "CT", "StudyDate": "20240101"}]
        result = to_album_index_format(records)
        assert result[0]['path'] == '/data/p001.dcm'

    def test_maps_all_fields_correctly(self):
        records = [{"filepath": "/data/p001.dcm", "PatientID": "P001",
                    "StudyInstanceUID": "1.2.3", "Modality": "CT", "StudyDate": "20240101"}]
        result = to_album_index_format(records)
        assert result[0]['patient_id'] == 'P001'
        assert result[0]['study_uid'] == '1.2.3'
        assert result[0]['modality'] == 'CT'

    def test_skips_records_with_empty_filepath(self):
        records = [{"filepath": "", "PatientID": "P001",
                    "StudyInstanceUID": "1.2.3", "Modality": "CT", "StudyDate": "20240101"}]
        result = to_album_index_format(records)
        assert result == []

    def test_handles_empty_records(self):
    def test_handles_empty_records(self):
        assert list(to_album_index_format([])) == []

    def test_converts_multiple_records(self):
        records = [
            {"filepath": "/data/p001.dcm", "PatientID": "P001",
             "StudyInstanceUID": "1.2.3", "Modality": "CT", "StudyDate": "20240101"},
            {"filepath": "/data/p002.dcm", "PatientID": "P002",
             "StudyInstanceUID": "1.2.4", "Modality": "MR", "StudyDate": "20240102"},
        ]
        result = to_album_index_format(records)
        assert len(result) == 2