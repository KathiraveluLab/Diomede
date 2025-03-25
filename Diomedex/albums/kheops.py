import requests
from typing import Optional, Dict
from flask import current_app

class KheopsAdapter:
    def __init__(self):
        self.base_url = current_app.config['KHEOPS_BASE_URL']
        self.auth = (
            current_app.config['KHEOPS_CLIENT_ID'],
            current_app.config['KHEOPS_CLIENT_SECRET']
        )
        
    def create_album(self, name: str, description: str = "") -> Optional[Dict]:
        """Create a new album in Kheops"""
        try:
            response = requests.post(
                f"{self.base_url}/albums",
                json={'name': name, 'description': description},
                auth=self.auth,
                timeout=30
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            current_app.logger.error(f"Kheops API error: {str(e)}")
            return None

    def add_to_album(self, album_id: str, dicom_files: list) -> Optional[Dict]:
        """Add DICOM files to Kheops album"""
        try:
            # This is a simplified implementation
            # Actual implementation would upload each DICOM file
            response = requests.post(
                f"{self.base_url}/albums/{album_id}/add",
                json={'files': dicom_files},
                auth=self.auth,
                timeout=30
            )
            response.raise_for_status()
            return {
                'album_id': album_id,
                'added_files': len(dicom_files),
                'viewer_url': f"{self.base_url}/viewer?album={album_id}"
            }
        except requests.exceptions.RequestException as e:
            current_app.logger.error(f"Kheops add files error: {str(e)}")
            return None