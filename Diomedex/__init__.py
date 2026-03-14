from .albums.routes import bp as albums_bp

def create_app():
    app = Flask(__name__)
    app.register_blueprint(albums_bp)
    return app