import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

load_dotenv()

# Define db at module level before any submodule imports so that albums/models.py can resolve
# `from .. import db` without a circular import.
db = SQLAlchemy()

def create_app(enable_routing=False, test_config=None):
    # Blueprint imports are deferred here to avoid triggering the circular
    # import chain at module load time.
    from .albums.routes import albums_bp
    from .routing.routes import routing_bp
    from .routing import DICOMRouter
    from .anonymization.routes import anonymization_bp

    app = Flask(__name__)

    if test_config is not None:
        app.config.update(test_config)
    else:
        db_uri = os.environ.get('DATABASE_URL', 'sqlite:///diomede.db')
        if db_uri:
            app.config['SQLALCHEMY_DATABASE_URI'] = db_uri

    secret_key = os.environ.get('DIOMEDE_SECRET_KEY')
    if not secret_key:
        if os.environ.get('FLASK_ENV') == 'production':
            raise ValueError('DIOMEDE_SECRET_KEY is not set for a production environment')
        # For development, a random key is acceptable.
        secret_key = secrets.token_hex(32)
    app.config['SECRET_KEY'] = secret_key

    # Configure STORAGE_PATH for the DICOM sandbox.
    if 'STORAGE_PATH' not in app.config:
        app.config['STORAGE_PATH'] = os.environ.get(
            'STORAGE_PATH',
            str(Path(app.root_path).parent / "storage")
        )

    os.makedirs(app.config['STORAGE_PATH'], exist_ok=True)

    # Initialize db with app
    db.init_app(app)

    # Register blueprints
    app.register_blueprint(albums_bp)
    app.register_blueprint(routing_bp)
    app.register_blueprint(anonymization_bp)

    # Initialize DICOM router if enabled
    if enable_routing:
        router = DICOMRouter()
        app.dicom_router = router

    return app
