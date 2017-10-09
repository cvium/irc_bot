from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

from future.utils import python_2_unicode_compatible

import re

from irc_bot.utils import printable_unicode_list
from irc_bot.numeric_replies import REPLY_CODES


@python_2_unicode_compatible
class IRCMessage(object):
    def __init__(self, msg):
        rfc_1459 = "^(@(?P<tags>[^ ]*) )?(:(?P<prefix>[^ ]+) +)?(?P<command>[^ ]+)( *(?P<arguments> .+))?"
        msg_contents = re.match(rfc_1459, msg)

        self.raw = msg
        self.tags = msg_contents.group('tags')
        self.prefix = msg_contents.group('prefix')
        self.from_nick = self.prefix.split('!')[0] if self.prefix and '!' in self.prefix else None
        self.command = msg_contents.group('command')
        if self.command.isdigit() and self.command in REPLY_CODES.keys():
            self.command = REPLY_CODES[self.command]
        if msg_contents.group('arguments'):
            args, sep, ext = msg_contents.group('arguments').partition(' :')
            self.arguments = args.split()
            if sep:
                self.arguments.append(ext)
        else:
            self.arguments = []

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        printable_arguments = printable_unicode_list(self.arguments)
        tmpl = (
            "command: {}, "
            "prefix: {}, "
            "tags: {}, "
            "arguments: {}, "
            "from_nick: {}, "
            "raw: {}"
        )
        return tmpl.format(self.command, self.prefix, self.tags, printable_arguments, self.from_nick, self.raw)