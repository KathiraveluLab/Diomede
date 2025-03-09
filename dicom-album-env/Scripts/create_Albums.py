import shutil

def create_album(subset_df, album_name):
    """Create an album by copying the queried DICOM files to a new directory."""
    album_dir = os.path.join("albums", album_name)
    os.makedirs(album_dir, exist_ok=True)
    
    for _, row in subset_df.iterrows():
        shutil.copy(row["FilePath"], album_dir)
    
    print(f"Album '{album_name}' created with {len(subset_df)} files.")

# Example usage
create_album(subset_df, "CT_Studies_2022")