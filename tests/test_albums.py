import pytest
from diomede.albums.core import DICOMAlbumCreator
from diomede.albums.models import DICOMFile, Album, db
from diomede import create_app

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
    # Create test DICOM file (simplified)
    dcm_file = tmp_path / "test.dcm"
    dcm_file.write_bytes(b'DICM_TEST_DATA')
    
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