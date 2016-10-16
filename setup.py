from setuptools import setup
import os
import sys
import io


def read(fname):
    return open(os.path.join(os.path.dirname(__file__), fname)).read()


# Populates __version__ without importing the package
__version__ = None
with io.open('irc_bot/__version__.py', encoding='utf-8')as ver_file:
    exec(ver_file.read())  # pylint: disable=W0122
if not __version__:
    print('Could not find __version__ from irc_bot/__version__.py')
    sys.exit(1)


setup(
    name='irc_bot',
    packages=['irc_bot'],
    version=__version__,
    description='A small library to create an IRC bot. Uses asyncore to ensure compatibility with Python 2.7+.',
    author='Claus Vium',
    author_email='clausvium@gmail.com',
    url='https://github.com/cvium/irc_bot',
    #download_url='https://github.com/cvium/irc_bot/tarball/__version__',
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
        'future',
        'six'
    ],
    license='MIT',
    long_description=read('README.md'),
    zip_safe=False
)
