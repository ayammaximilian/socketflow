#!/usr/bin/env python3
"""
TCP networking for Python, without the boilerplate.
"""

from setuptools import setup, find_packages
import os


# Read the README file for long description
def read_readme():
    readme_path = os.path.join(os.path.dirname(__file__), "README.md")
    if os.path.exists(readme_path):
        with open(readme_path, "r", encoding="utf-8") as f:
            return f.read()


# Read version from __init__.py
def get_version():
    init_path = os.path.join(os.path.dirname(__file__), "socketflow", "__init__.py")
    with open(init_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("__version__"):
                return line.split("=")[1].strip().strip("\"'")


setup(
    name="socketflow",
    version=get_version(),
    author="SocketFlow Team",
    author_email="contact@socketflow.dev",
    description="TCP networking for Python, without the boilerplate.",
    long_description=read_readme(),
    long_description_content_type="text/markdown",
    url="https://github.com/ayammaximilian/socketflow",
    project_urls={
        "Bug Reports": "https://github.com/ayammaximilian/socketflow/issues",
        "Source": "https://github.com/ayammaximilian/socketflow",
        "Documentation": "https://socketflow.dev",
    },
    packages=find_packages(),
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Internet",
        "Topic :: Internet :: WWW/HTTP",
        "Topic :: Internet :: WWW/HTTP :: HTTP Servers",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Software Development :: Libraries :: Application Frameworks",
        "Topic :: System :: Networking",
        "Topic :: Communications",
        "Topic :: Security :: Cryptography",
    ],
    python_requires=">=3.7",
    # The core library uses only the Python standard library, so there is
    # nothing to install here. The compression codecs below are optional and
    # are installed on demand, e.g. `pip install socketflow[zstd]`.
    install_requires=[],
    extras_require={
        "zstd": ["zstandard>=0.15"],
        "brotli": ["Brotli>=1.0"],
        "all": ["zstandard>=0.15", "Brotli>=1.0"],
    },
    keywords=[
        "networking",
        "tcp",
        "socket",
        "server",
        "client",
        "real-time",
        "messaging",
        "rpc",
        "events",
        "middleware",
        "blueprint",
        "ssl",
        "tls",
        "encryption",
        "mtls",
        "authentication",
        "compression",
        "backpressure",
        "async",
        "observability",
    ],
    entry_points={},
    include_package_data=True,
    package_data={
        "socketflow": [
            "*.md",
            "*.txt",
            "*.yml",
            "*.yaml",
        ],
    },
    zip_safe=False,
    platforms=["any"],
    license="MIT",
)
