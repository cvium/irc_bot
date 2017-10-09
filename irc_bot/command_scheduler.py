from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin

import bisect
import datetime
import logging

log = logging.getLogger('irc_bot')


class CommandScheduler(object):
    def __init__(self):
        self.queue = []

    def clear(self):
        self.queue = []

    def peek(self):
        if len(self.queue) > 0:
            return self.queue[0].scheduled_time
        return datetime.datetime.max

    def execute(self):
        while self.peek() <= datetime.datetime.now():
            queued_command = self.queue.pop(0)
            log.debug('Executing scheduled command %s', queued_command.command.__name__)
            queued_command.command()
            if queued_command.persists:
                self.queue_command(queued_command.after, queued_command.command, queued_command.persists)

    def queue_command(self, after, cmd, persists=False, unique=True):
        log.debug('Queueing command "%s" to execute in %s second(s)', cmd.__name__, after)
        timestamp = datetime.datetime.now() + datetime.timedelta(seconds=after)
        queued_command = QueuedCommand(after, timestamp, cmd, persists)
        if not unique or queued_command not in self.queue:
            bisect.insort(self.queue, queued_command)
        else:
            log.warning('Failed to queue command "%s" because it\'s already queued.', cmd.__name__)


class QueuedCommand(object):
    def __init__(self, after, scheduled_time, command, persists=False):
        self.after = after
        self.scheduled_time = scheduled_time
        self.command = command
        self.persists = persists

    def __lt__(self, other):
        return self.scheduled_time < other.scheduled_time

    def __eq__(self, other):
        commands_match = self.command.__name__ == other.command.__name__
        if hasattr(self.command, 'args') and hasattr(other.command, 'args'):
            return commands_match and self.command.args == other.command.args
        return commands_match
