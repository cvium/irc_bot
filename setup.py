from distutils.core import setup

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
  classifiers=[],
  install_requires=[
    'future'
  ],
)
