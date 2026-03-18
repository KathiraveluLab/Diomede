import os
import secrets

from dotenv import load_dotenv
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

load_dotenv()

# Initialize SQLAlchemy instance before importing blueprints, since the
# models module (models.py) does `from .. import db` at import time.
db = SQLAlchemy()

from .albums.routes import albums_bp


def create_app():
    app = Flask(__name__)

    secret_key = os.environ.get('DIOMEDE_SECRET_KEY')
    if not secret_key:
        if os.environ.get('FLASK_ENV') == 'production':
            raise ValueError('DIOMEDE_SECRET_KEY is not set for a production environment')
        # For development, a random key is acceptable.
        secret_key = secrets.token_hex(32)
    app.config['SECRET_KEY'] = secret_key

    # Initialize db with app
    db.init_app(app)

    app.register_blueprint(albums_bp)
    return app
