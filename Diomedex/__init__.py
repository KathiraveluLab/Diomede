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

    app.config['SECRET_KEY'] = os.environ.get('DIOMEDE_SECRET_KEY') or secrets.token_hex(32)

    # Initialize db with app
    db.init_app(app)

    app.register_blueprint(albums_bp)
    return app
