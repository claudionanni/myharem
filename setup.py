from setuptools import setup, find_packages

setup(
    name='myharem',
    version='0.1.0',
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        'click',
    ],
    entry_points={
        'console_scripts': [
            'mh = mh.cli:main',
        ],
    },
)
