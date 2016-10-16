from __future__ import unicode_literals, division, absolute_import
from builtins import *  # pylint: disable=unused-import, redefined-builtin
from future.utils import python_2_unicode_compatible
# -*- coding: utf-8 -*-

import asynchat
import asyncore
import bisect
import datetime
import errno
import functools
import hashlib
import logging
import re
import ssl
import socket
import uuid
import time

from .numeric_replies import REPLY_CODES

from six.moves.html_parser import HTMLParser

log = logging.getLogger('irc_bot')


def partial(func, *args, **kwargs):
    """helper function to make sure __name__ is set"""
    f = functools.partial(func, *args, **kwargs)
    functools.update_wrapper(f, func)
    return f


class EventHandler(object):
    def __init__(self, func, command=None, msg=None):
        self.func = func
        self.command = command
        self.msg = msg


def printable_unicode_list(unicode_list):
    return '[{}]'.format(', '.join(str(x) for x in unicode_list))


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


class Schedule(object):
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


def is_channel(string):
    """
    Return True if input string is a channel
    """
    return string and string[0] in "#&+!"


def strip_irc_colors(data):
    """Strip mirc colors from string. Expects data to be decoded."""
    return re.sub('[\x02\x0F\x16\x1D\x1F]|\x03(\d{1,2}(,\d{1,2})?)?', '', data)


def strip_invisible(data):
    """Strip stupid characters that have been colored 'invisible'. Assumes data has been decoded"""
    stripped_data = ''
    i = 0
    while i < len(data):
        c = data[i]
        if c == '\x03':
            match = re.match('^(\x03(?:(\d{1,2})(?:,(\d{1,2}))(.?)?)?)', data[i:])
            # if the colors match eg. \x031,1a, then "a" has same foreground and background color -> invisible
            if match and match.group(2) == match.group(3):
                if match.group(4) and ord(match.group(4)) > 31:
                    c = match.group(0)[:-1] + ' '
                else:
                    c = match.group(0)
                i += len(c) - 1
        i += 1
        stripped_data += c
    return stripped_data


def decode_html(data):
    """Decode dumb html"""
    h = HTMLParser()
    return h.unescape(data)


# Enum sort of
class IRCChannelStatus(object):
    IGNORE = -1
    NOT_CONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2
    PARTING = 3


class IRCBot(asynchat.async_chat):
    ac_in_buffer_size = 8192
    ac_out_buffer_size = 8192

    def __init__(self, config):
        asynchat.async_chat.__init__(self)
        self.servers = config['servers']
        self.port = config['port']
        self.channels = {}
        for channel in config['channels']:
            self.add_irc_channel(channel)
        self.nickname = config.get('nickname', 'Flexget-%s' % uuid.uuid4())
        self.invite_nickname = config.get('invite_nickname')
        self.invite_message = config.get('invite_message')
        self.nickserv_password = config.get('nickserv_password')
        self.use_ssl = config.get('use_ssl', False)
        self.timeout = 0

        self.real_nickname = self.nickname
        self.set_terminator(b'\r\n')
        self.buffer = ''
        self.connection_attempts = 1
        self.max_connection_delay = 300  # 5 minutes
        self.throttled = False
        self.schedule = Schedule()
        self.running = True
        self.reconnecting = False
        self.event_handlers = {}

        # add event handlers
        self.add_event_handler(self.on_nicknotregistered, 'ERRNICKNOTREGISTERED')
        self.add_event_handler(self.on_inviteonly, 'ERRINVITEONLYCHAN')
        self.add_event_handler(self.on_error, 'ERROR')
        self.add_event_handler(self.on_part, 'PART')
        self.add_event_handler(self.on_ping, 'PING')
        self.add_event_handler(self.on_invite, 'INVITE')
        self.add_event_handler(self.on_rplmotdend, ['RPLMOTDEND', 'ERRNOMOTD'])
        self.add_event_handler(self.on_privmsg, 'PRIVMSG')
        self.add_event_handler(self.on_join, 'JOIN')
        self.add_event_handler(self.on_kick, 'KICK')
        self.add_event_handler(self.on_banned, 'ERRBANNEDFROMCHAN')
        self.add_event_handler(self.on_welcome, 'RPLWELCOME')
        self.add_event_handler(self.on_nickinuse, 'ERRNICKNAMEINUSE')
        self.add_event_handler(self.on_nosuchnick, 'ERRNOSUCHNICK')

    def add_irc_channel(self, name, status=None):
        name = name.lower()
        if name in self.channels:
            return
        self.channels[name] = status

    def handle_expt_event(self):
        error = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if error == errno.ECONNREFUSED:
            log.error('Connection refused. Check server connection config.')
            self.close()
            return
        elif error == errno.ETIMEDOUT:
            log.error('Connection timed out.')
        log.error(error)
        self.handle_error()

    def _handshake(self):
        try:
            self.socket.do_handshake(block=True)
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
            pass

    def handle_read(self):
        while True:
            try:
                asynchat.async_chat.handle_read(self)
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                self._handshake()
            else:
                break

    def handle_connect(self):
        # sometimes connection crashes but still causes a connect event. In that case reconnection has already
        # been scheduled, so there's no need to pretend everything is ok.
        if not self.reconnecting:
            self.timeout = 0
            log.info('Connected to server %s', self.servers[0])
            self.write('USER %s %s %s :%s' % (self.real_nickname, '8', '*', self.real_nickname))
            self.nick(self.real_nickname)

    def handle_error(self):
        # Just ignore ssl read errors, they don't seem to matter
        if sys.exc_info()[0] == ssl.SSLWantReadError:
            return
        if not self.reconnecting:
            log.error('Unknown error occurred.', exc_info=True)
            self.reconnect_with_delay()

    def reconnect_with_delay(self):
        self.reconnecting = True
        delay = min(self.connection_attempts ** 2, self.max_connection_delay)
        log.info('Attempting to restart connection to %s in %s seconds.', self.current_server, delay)
        # Clear the schedule since connection is dead
        self.schedule.clear()
        self.schedule.queue_command(delay, self.reconnect)

    def handle_close(self):
        # only handle close event if we're not actually just shutting down
        if self.running:
            self.handle_error()

    def exit(self):
        log.info('Shutting down connection to %s:%s', self.servers[0], self.port)
        self.running = False
        self.close()

    def reconnect(self):
        """Reconnects to the server"""
        try:
            self.reconnecting = False
            self.reset_channels()
            self.connection_attempts += 1
            # change server
            self.servers += [self.servers.pop(0)]
            self.connect(self.current_server)
        except IOError as e:
            log.error(e)
            self.handle_error()

    def reset_channels(self):
        """Sets the status to channels as NOT CONNECTED."""
        for channel in self.channels.keys():
            self.channels[channel] = IRCChannelStatus.NOT_CONNECTED

    def keepalive(self):
        """Used to send pings continually to the server to avoid timeouts"""
        self.write('PING %s' % self.servers[0])

    def join(self, channels, delay=None):
        """Joins the specified channel(s) with an optional delay.
        Will not join channel if status is CONNECTED, CONNECTING or IGNORE.
        Also resets the channel flag to None to indicate it has no status."""
        illegal_statuses = [IRCChannelStatus.CONNECTED, IRCChannelStatus.CONNECTING, IRCChannelStatus.IGNORE]
        for channel in channels:
            status = self.channels.get(channel.lower())
            if status is not None and status not in illegal_statuses:
                self.channels[channel.lower()] = None
        if delay:
            self.schedule.queue_command(delay, partial(self.join, channels))
            return
        for channel in channels:
            channel = channel.lower()
            if channel in self.channels and self.channels[channel] in illegal_statuses:
                continue
            log.info('Joining channel: %s', channel)
            self.write('JOIN %s' % channel)

    def found_terminator(self):
        lines = self._process_message(self.buffer)
        self.buffer = ''
        self.parse_message(lines)

    def collect_incoming_data(self, data):
        """Buffer the data"""
        try:
            data = data.decode('utf-8')
        except UnicodeDecodeError as e:
            log.debug('%s. Will attempt to use latin-1 decoding instead.', e)
            data = data.decode('latin-1')

        self.buffer += decode_html(strip_irc_colors(strip_invisible(data)))

    def _process_message(self, msg):
        messages = []
        for m in msg.split('\r\n'):
            messages.append(IRCMessage(m))
        return messages

    def parse_message(self, lines):
        log.debug('Received: %s', printable_unicode_list(lines))
        for line in lines:
            self.handle_event(line)

    def on_nicknotregistered(self, msg):
        """Simply logs the error message. Could also register with the specified password, but needs an email
        as well."""
        log.error(msg.arguments[2])

    def on_inviteonly(self, msg):
        """Try to join invite only channel if there is specified an invite nickname ie. a nickname that is capable
        of inviting the bot. Otherwise set the IGNORE flag on the channel."""
        if self.invite_nickname:
            log.error('Invite only channel %s', msg.arguments[1])
            self.join([msg.arguments[1]], delay=10)
        else:
            log.error('No invite nick specified. Cannot join invite-only channel %s', msg.arguments[1])
            self.channels[msg.arguments[1].lower()] = IRCChannelStatus.IGNORE

    def on_error(self, msg):
        """This method simply logs the error message received from the server and sets throttled flag if message
        contains 'throttled/throttling'"""
        log.error('Received error message from %s: %s', self.servers[0], msg.arguments[0])
        if 'throttled' in msg.raw or 'throttling' in msg.raw:
            self.throttled = True

    def on_part(self, msg):
        """This function assumes we parted on purpose, so it sets the channel status to ignore"""
        # Others leaving doesn't mean we did
        if msg.from_nick == self.real_nickname:
            channel = msg.arguments[0]
            log.info('Left channel %s', channel)
            if channel in self.channels.keys():
                self.channels[channel] = IRCChannelStatus.IGNORE

    def on_ping(self, msg):
        """Sends back a proper PONG message when receiving a PING"""
        self.write('PONG :%s' % msg.arguments[0])

    def on_invite(self, msg):
        """Joins the channel after receiving an invite"""
        if self.nickname == msg.arguments[0] and msg.arguments[1].lower() in self.channels:
            self.join([msg.arguments[1]])

    def on_rplmotdend(self, msg):
        """This bot defines receiving MOTD end message as being fully connected"""
        log.debug('Successfully connected to %s', self.servers[0])
        self.identify_with_nickserv()

    def on_privmsg(self, msg):
        """This method should be overridden by subclass"""
        if is_channel(msg.arguments[0]):
            log.debug('Public message in channel %s received', msg.arguments[0])
        else:
            log.debug('Private message from %s received', msg.from_nick)
        raise NotImplementedError()

    def on_join(self, msg):
        """Kind of an overloaded function in that it will leave channels it has been forced into without
        wanting to"""
        channel = msg.arguments[0].lower()
        if msg.from_nick == self.real_nickname:
            log.info('Joined channel %s', channel)
            if channel not in self.channels or self.channels[channel.lower()] == IRCChannelStatus.IGNORE:
                self.part([channel])
                return
            self.channels[msg.arguments[0].lower()] = IRCChannelStatus.CONNECTED

    def on_kick(self, msg):
        """Merely sets the flag to NOT CONNECTED for the channel"""
        if msg.arguments[1] == self.real_nickname:
            channel = msg.arguments[0]
            log.error('Kicked from channel %s by %s', channel, msg.from_nick)
            self.channels[channel.lower()] = IRCChannelStatus.NOT_CONNECTED

    def on_banned(self, msg):
        """Sets the ignore flag on the banned channel"""
        log.error('Banned from channel %s', msg.arguments[1])
        try:
            self.channels[msg.arguments[1].lower()] = IRCChannelStatus.IGNORE
        except ValueError:
            pass

    def on_welcome(self, msg):
        """Schedules keepalive and saves the nickname received from server"""
        # Save the nick from server as it may have changed it
        self.real_nickname = msg.arguments[0]
        self.throttled = False
        # Queue the heartbeat
        self.schedule.queue_command(2 * 60, self.keepalive, persists=True)

    def on_nickinuse(self, msg):
        """Appends an underscore to the nickname"""
        if self.real_nickname == msg.arguments[1]:
            log.warning('Nickname %s is in use', self.real_nickname)
            self.real_nickname += '_'
            self.write('NICK %s' % self.real_nickname)

    def on_nosuchnick(self, msg):
        """Logs the error. Should not happen."""
        log.error('%s: %s', msg.arguments[2], msg.arguments[1])

    def send_privmsg(self, to, msg):
        """Sends a PRIVMSG"""
        self.write('PRIVMSG %s :%s' % (to, msg))

    def nick(self, nickname):
        """Sets the nickname for the bot"""
        self.write('NICK %s' % nickname)

    def part(self, channels):
        """PARTs from the channel(s)"""
        if channels:
            log.verbose('PARTing from channels: %s', ','.join(channels))
            self.write('PART %s' % ','.join(channels))

    def write(self, msg):
        """Appends a newline to the message and encodes it to bytes before sending"""
        log.debug('SENT: %s', msg)
        unencoded_msg = msg + '\r\n'
        self.send(unencoded_msg.encode())

    def quit(self):
        self.write('QUIT :I\'m outta here!')
        self.exit()

    @property
    def current_server(self):
        return (self.servers[0], self.port)

    def connect(self, server):
        """Creates a (ssl) socket and connects to the server. Not using asyncore's connect-function because it sucks."""
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        if self.use_ssl:
            try:
                self.socket.setblocking(True)
                self.socket = ssl.wrap_socket(self.socket)
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError) as e:
                log.debug(e)
                self._handshake()
            except ssl.SSLError as e:
                log.error(e)
                self.exit()
                return
            finally:
                self.socket.setblocking(False)

        log.info('Connecting to %s', self.current_server)
        self.socket.setblocking(True)
        self.socket.connect(server)
        self.socket.setblocking(False)
        self.handle_connect_event()

    def start(self):
        """The main loop keeping the bot alive"""
        self.connect(self.current_server)
        while self.running:
            # No need to busy-wait
            time.sleep(0.2)
            self.schedule.execute()
            # Skip polling etc. if we're reconnecting
            if self.timeout >= 30:
                log.error('Establishing a connection to %s failed after 30 seconds.', self.current_server)
                self.timeout = 0
                self.reconnect_with_delay()
                continue
            if self.reconnecting or not self.connected:
                self.timeout += 0.2
                continue
            try:
                asyncore.poll(timeout=10, map={self.socket: self})
            except socket.error as e:
                log.error(e)
                self.reconnect_with_delay()
                continue
            dc_channels = self.disconnected_channels()
            if dc_channels:
                self.join(dc_channels, delay=5)

    @property
    def connected_channels(self):
        """Returns a list of channels with status CONNECTED"""
        res = []
        for name, status in self.channels.items():
            if status == IRCChannelStatus.CONNECTED:
                res.append(name)
        return res

    def disconnected_channels(self):
        """Returns a list of channels with status NOT CONNECTED"""
        res = []
        for name, status in self.channels.items():
            if status == IRCChannelStatus.NOT_CONNECTED:
                res.append(name)
        return res

    def identify_with_nickserv(self):
        """
        Identifies the connection with Nickserv, ghosting to recover the nickname if required

        :return:
        """
        if self.nickserv_password:
            # If we've not got our peferred nickname and NickServ is configured, ghost the connection
            if self.nickname != self.real_nickname:
                log.info('Ghosting old connection')
                self.send_privmsg('NickServ', 'GHOST %s %s' % (self.nickname, self.nickserv_password))
                self.nick(self.nickname)

            # Identify with NickServ
            log.info('Identifying with NickServ as %s', self.nickname)
            self.send_privmsg('NickServ', 'IDENTIFY %s' % self.nickserv_password)
        if self.invite_nickname:
            self.schedule.queue_command(5, self.request_channel_invite)
        else:
            self.join(list(self.channels.keys()), delay=5)

    def request_channel_invite(self):
        """
        Requests an invite from the configured invite user.
        :return:
        """
        channels = list(self.channels.keys())
        log.info('Requesting an invite to channels %s from %s', channels, self.invite_nickname)
        self.send_privmsg(self.invite_nickname, self.invite_message)
        self.join(channels, delay=5)

    def add_event_handler(self, func, command=None, msg=None):
        if not isinstance(command, list):
            command = [command] if command else []
        if not isinstance(msg, list):
            msg = [msg] if msg else []

        log.debug('Adding event handler %s for (command=%s, msg=%s)', func.__name__, command, msg)
        hash = hashlib.md5('.'.join(sorted(command) + sorted(msg)).encode('utf-8'))
        self.event_handlers[hash] = EventHandler(func, command, msg)

    def handle_event(self, msg):
        """Call the proper function that has been set to handle the input message type eg. RPLMOTD"""
        for regexp, event in self.event_handlers.items():
            if event.command:
                for cmd in event.command:
                    cmd = cmd.rstrip('$') + '$'
                    if re.match(cmd, msg.command, re.IGNORECASE):
                        event.func(msg)
            elif event.msg:
                for m in event.msg:
                    m = m.rstrip('$') + '$'
                    if re.match(m, msg.raw, re.IGNORECASE):
                        event.func(msg)


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
