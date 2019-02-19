from setuptools import setup, find_packages

setup(
    name="blessclient",
    version="0.4.2",
    packages=find_packages(exclude=["test*"]),
    install_requires=[
        'boto3>=1.4.0,<2.0.0',
        'psutil>=4.3',
        'kmsauth>=0.1.8',
        'six',
        'hvac',
        'requests_aws_sign',
        'pycryptodomex',
        'requests'
    ],
    author="Chris Steipp",
    author_email="csteipp@lyft.com",
    description="Basefarm modified blessclient. Forked from lyft",
    license="apache2",
    url="https://github.com/basefarm/python-blessclient",
    entry_points={
        "console_scripts": [
            "blessclient = blessclient.client:main",
            "bssh = blesswrapper.sshclient:main"
        ],
    },
)
