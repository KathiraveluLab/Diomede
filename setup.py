from setuptools import setup, find_packages

setup(
    name="diomede",
    version="0.1.0",
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        'flask>=2.0.0',
        'pydicom>=2.3.0',
        'flask-sqlalchemy>=2.5.0',
        'python-dotenv>=0.19.0',
        'requests>=2.26.0'
    ],
    entry_points={
        'console_scripts': [
            'diomede=diomede.cli:main',
        ],
    },
)