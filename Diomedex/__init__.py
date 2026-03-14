from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from .albums.routes import albums_bp

# Initialize SQLAlchemy instance
db = SQLAlchemy()

def create_app():
    app = Flask(__name__)
    
    # Initialize db with app
    db.init_app(app)
    
    app.register_blueprint(albums_bp)
    return app