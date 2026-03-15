"""
Tests for secret key configuration.

Verifies that the Diomedex application loads the Flask secret key from the DIOMEDE_SECRET_KEY
environment variable rather than using a hardcoded string.
"""

import os
import pytest
from unittest.mock import patch

from Diomedex import create_app


# Patch db.init_app so these config-focused tests don't require a real database.
_db_patch = patch('Diomedex.db.init_app')


class TestSecretKeyConfig:

    def test_DIOMEDE_SECRET_KEY_loaded_from_env(self):
        """When DIOMEDE_SECRET_KEY is set in the environment, create_app() must use it."""
        with _db_patch, patch.dict(os.environ, {'DIOMEDE_SECRET_KEY': 'my-secure-test-key'}):
            app = create_app()
        assert app.config['SECRET_KEY'] == 'my-secure-test-key'

    def test_DIOMEDE_SECRET_KEY_not_hardcoded_when_env_missing(self):
        """When DIOMEDE_SECRET_KEY is absent, the key must not be the old hardcoded string."""
        with _db_patch, patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="DIOMEDE_SECRET_KEY is not set for production environment"):
                create_app()

    def test_fallback_key_is_random_per_call(self):
        """Without DIOMEDE_SECRET_KEY, each create_app() call gets a different random key."""
        with _db_patch, patch.dict(os.environ, {'FLASK_DEBUG': '1'}, clear=True):
            app1 = create_app()
            app2 = create_app()
        assert app1.config['SECRET_KEY'] != app2.config['SECRET_KEY']
