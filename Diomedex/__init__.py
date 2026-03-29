import os
import secrets

from dotenv import load_dotenv
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

load_dotenv()

db = SQLAlchemy()

from .albums.routes import albums_bp
from .routing.routes import routing_bp
from .routing.anonymization_routes import anonymization_bp
from .routing import DICOMRouter

def create_app(enable_routing=False):
    app = Flask(__name__)

    secret_key = os.environ.get('DIOMEDE_SECRET_KEY')
    if not secret_key:
        if os.environ.get('FLASK_ENV') == 'production':
            raise ValueError('DIOMEDE_SECRET_KEY is not set for a production environment')
        # For development, a random key is acceptable.
        secret_key = secrets.token_hex(32)
    app.config['SECRET_KEY'] = secret_key

    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///diomede.db')
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
