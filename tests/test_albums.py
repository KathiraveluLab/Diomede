import pytest
from Diomedex.albums.core import DICOMAlbumCreator
from Diomedex.albums.models import DICOMFile, Album, db
from Diomedex import create_app

@pytest.fixture
def app():
    app = create_app()
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()

def test_scan_directory(tmp_path, app):
    import pydicom
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import UID

    dcm_file = tmp_path / "test.dcm"
    
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = UID('1.2.840.10008.5.1.4.1.1.2')
    file_meta.MediaStorageSOPInstanceUID = UID('1.2.3')
    file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
    
    ds = FileDataset(str(dcm_file), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.PatientID = "12345"
    ds.StudyInstanceUID = "1.2.3.4"
    ds.Modality = "CT"
    ds.save_as(str(dcm_file), little_endian=True, implicit_vr=False)
    with app.app_context():
        creator = DICOMAlbumCreator(str(tmp_path))
        files = creator.scan_directory(str(tmp_path))
        assert len(files) == 1
        assert files[0]['path'] == str(dcm_file)

def test_create_album(app):
    with app.app_context():
        test_album = Album(name="Test Album", description="Test")
        db.session.add(test_album)
        db.session.commit()
        
        album = Album.query.first()
        assert album.name == "Test Album"