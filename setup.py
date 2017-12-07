"""
RadIO is a framework for batch-processing of computational tomography (CT)-scans
for deep learning experiments.
Documentation - https://analysiscenter.github.io/radio/
"""

from setuptools import setup, find_packages
import re

with open('cardio/__init__.py', 'r') as f:
    version = re.search(r'^__version__\s*=\s*[\'"]([^\'"]*)[\'"]', f.read(), re.MULTILINE).group(1)


with open('docs/index.rst', 'r') as f:
    long_description = f.read()


setup(
    name='radio',
    packages=find_packages(exclude=['examples']),
    version=version,
    url='https://github.com/analysiscenter/radio',
    license='Apache License 2.0',
    author='Data Analysis Center team',
    author_email='radio@analysiscenter.ru',
    description='A framework for deep research of CT scans',
    long_description=long_description,
    zip_safe=False,
    platforms='any',
    install_requires=[
        'numpy>=1.10',
        'wfdb>=1.2.2.',
        'dill>=0.2.7.1',
        'scikit-learn>=0.19.0',
        'numba>=0.35.0',
        'blosc>=1.5.0'
    ],
    extras_require={
        'tensorflow': ['tensorflow>=1.4'],
        'tensorflow-gpu': ['tensorflow-gpu>=1.4'],
        'keras': ['keras>=2.0.0']
    },
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Intended Audience :: Science/Research',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Topic :: Scientific/Engineering'
    ],
)