from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from .albums.routes import albums_bp
from .routing.routes import routing_bp
from .routing import DICOMRouter

db = SQLAlchemy()

def create_app(enable_routing=False):
    app = Flask(__name__)
    
    # Initialize db with app
    db.init_app(app)
    
    # Register blueprints
    app.register_blueprint(albums_bp)
    app.register_blueprint(routing_bp)
    
    # Initialize DICOM router if enabled
    if enable_routing:
        router = DICOMRouter()
        app.dicom_router = router
    
    return app