import asynchat
import asyncore
import socket
import logging
import sys
import re
import uuid
import datetime
import bisect
import time
import errno
import threading
from functools import wraps

from numeric_replies import REPLY_CODES

log = logging.getLogger('irc_bot')
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

event_handlers = {}


class EventHandler(object):

    def __init__(self, regexp, func):
        self.regexp = regexp
        self.func = func.__name__


def event(*regexps):
    def decorator(func):
        for regexp in regexps:
            log.info('Adding event handler %s for %s', func.__name__, regexp)
            event_handlers[regexp] = EventHandler(regexp, func)
        return func
    return decorator


def handle_event(msg, obj):
    for regexp, _event in event_handlers.items():
        if re.search(regexp, msg.command, re.IGNORECASE) or re.search(regexp, msg.raw, re.IGNORECASE):
            if callable(getattr(obj, _event.func)):
                getattr(obj, _event.func)(msg)


class Schedule(object):

    def __init__(self):
        self.queue = []

    def execute(self):
        for (scheduled_time, cmd) in self.queue:
            if datetime.datetime.now() >= scheduled_time:
                cmd()
                self.queue.pop(0)
            else:
                break

    def queue_command(self, after, cmd):
        timestamp = datetime.datetime.now() + datetime.timedelta(seconds=after)
        bisect.insort(self.queue, (timestamp, cmd))


class IRCBot(asynchat.async_chat):
    ac_in_buffer_size = 8192
    ac_out_buffer_size = 8192

    def __init__(self, config):
        asynchat.async_chat.__init__(self)
        self.server = config['server']
        self.port = config['port']
        self.channels = config['channels']
        self.connected_channels= []
        self.nickname = config.get('nickname', 'Flexget-%s' % uuid.uuid4())
        self.set_terminator('\r\n')
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        log.info('Connecting to %s', (self.server, self.port))
        self.connect((self.server, self.port))
        self.buffer = ''
        self.connection_attempts = 0
        self.max_connection_delay = 300  # 5 minutes
        self.schedule = Schedule()
        self.shutdown_event = threading.Event()

    def handle_expt_event(self):
        error = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if error == errno.ECONNREFUSED:
            log.error('Connection refused. Check server connection config.')
            self.close()
            return
        elif error == errno.ETIMEDOUT:
            log.error('Connection timed out.')
        self.handle_error()

    def handle_connect(self):
        log.info('Connected to server %s', self.server)
        self.write('USER %s %s %s :%s' % (self.nickname, '8', '*', self.nickname))
        self.write('NICK %s' % self.nickname)

    def handle_error(self):
        if not self.connected:
            delay = min(self.connection_attempts ** 2, self.max_connection_delay)
            self.schedule.queue_command(delay, self.reconnect)

    def handle_close(self):
        log.info('Closing connection to %s', self.server)
        self.close()

    def reconnect(self):
        self.connect((self.server, self.port))

    def join(self, channels):
        for channel in channels:
            log.info('Joining channel: %s', channel)
            self.write('JOIN %s' % channel)

    def found_terminator(self):
        lines = [self._process_message(self.buffer)]
        self.buffer = ''
        self.parse_message(lines)

    def collect_incoming_data(self, data):
        """Buffer the data"""
        self.buffer += data

    def _process_message(self, msg):
        return IRCMessage(msg)

    def parse_message(self, lines):
        log.debug(lines)
        for line in lines:
            #on_handler = getattr(self, 'on_' + line.command.lower(), None)
            #if callable(on_handler):
            #    log.debug('Calling %s', on_handler.__name__)
            #    on_handler(line)
            handle_event(line, self)

    @event('PING')
    def on_ping(self, msg):
        self.write('PONG :%s' % msg.arguments[0])

    @event('INVITE')
    def on_invite(self, msg):
        if self.nickname == msg.arguments[0]:
            self.join([msg.arguments[1]])

    @event('RPLMOTDEND', 'ERRNOMOTD')
    def on_rplmotdend(self, msg):
        log.debug('Successfully connected to %s', self.server)
        self.join(self.channels)

    @event('PRIVMSG')
    def on_privmsg(self, msg):
        self.handle_privmsg(msg)

    @event('JOIN')
    def on_join(self, msg):
        log.info('Joined channel %s', msg.arguments[0])
        self.connected_channels.append(msg.arguments[0])

    @event('KICK')
    def on_kick(self, msg):
        pass

    @event('ERRBANNEDFROMCHAN')
    def on_banned(self, msg):
        log.error('Banned from channel %s', msg.arguments[1])
        self.channels.remove(msg.arguments[1])

    def handle_privmsg(self, msg):
        if msg.arguments[0] in self.channels:
            log.debug('Public message in channel %s received', msg.arguments[0])
        else:
            log.debug('Private message from %s received', msg.from_nick)

    def write(self, msg):
        log.debug('SENT: %s', msg)
        self.send(msg + '\r\n')

    def quit(self):
        self.write('QUIT :I\'m outta here!')
        self.shutdown_event.set()
        self.close()

    def start(self):
        while not self.shutdown_event.is_set():
            self.schedule.execute()
            asyncore.loop(timeout=1, map={self.socket: self})


class IRCMessage(object):

    def __init__(self, msg):
        rfc_1459 = "^(@(?P<tags>[^ ]*) )?(:(?P<prefix>[^ ]+) +)?(?P<command>[^ ]+)( *(?P<arguments> .+))?"
        msg_contents = re.match(rfc_1459, msg)

        self.raw = msg
        self.tags = msg_contents.group('tags')
        self.prefix = msg_contents.group('prefix')
        self.from_nick = self.prefix.split('!')[0] if self.prefix and '!' in self.prefix else None
        self.command = msg_contents.group('command')
        if self.command.isdigit() and int(self.command) in REPLY_CODES.keys():
            self.command = REPLY_CODES[int(self.command)]
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
        tmpl = (
            "command: {command}, "
            "prefix: {prefix}, "
            "tags: {tags}, "
            "arguments: {arguments}, "
            "from_nick: {from_nick}, "
            "raw: {raw}"
        )
        return tmpl.format(**vars(self))


if __name__ == "__main__":
    bot = None
    try:
        config = {'server': 'chat.freenode.net', 'channels': ['#flexgettest'], 'nickname': 'flexget_bot', 'port': 6667}
        bot = IRCBot(config)
        bot.start()
    except KeyboardInterrupt:
        log.info('Exiting')
        bot.quit()
