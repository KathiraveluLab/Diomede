from setuptools import find_packages, setup

setup(
    name="diomede",
    version="0.1.0",
   description="DICOM-based telemedicine and imaging framework",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    include_package_data=True,
    include_package_data=True,
    install_requires=[
         "fastapi>=0.116.0",
        "uvicorn>=0.35.0",
        "pydicom>=3.0.0",
        "PyYAML>=6.0.0",
        "httpx>=0.28.0",
        "python-multipart>=0.0.20",
        "pydantic>=2.11.0",
    ],
    
)
