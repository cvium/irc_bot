from setuptools import setup
import os


def read(fname):
    return open(os.path.join(os.path.dirname(__file__), fname)).read()


setup(
    name='irc_bot',
    packages=['irc_bot'],
    version='1.0.1',
    description='A small library to create an IRC bot. Uses asyncore to ensure compatibility with Python 2.7.',
    author='Claus Vium',
    author_email='clausvium@gmail.com',
    url='https://github.com/cvium/irc_bot',
    download_url='https://github.com/cvium/irc_bot/tarball/1.0.1',  # I'll explain this in a second
    keywords=['irc', 'bot'],  # arbitrary keywords
    classifiers=[
        'Intended Audience :: Developers',

        'License :: OSI Approved :: MIT License',

        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 2.6',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.2',
        'Programming Language :: Python :: 3.3',
        'Programming Language :: Python :: 3.4',
    ],
    install_requires=[
        'future'
    ],
    license='MIT',
    long_description=read('README.md'),
    zip_safe=False
)
