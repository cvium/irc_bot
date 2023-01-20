from __future__ import unicode_literals, division, absolute_import

import asynchat
import asyncore
import errno
import functools
import hashlib
import logging
import re
import socket
import ssl
import sys
import time
import uuid

from builtins import *  # pylint: disable=unused-import, redefined-builtin

from irc_bot.command_scheduler import CommandScheduler
from irc_bot.irc_message import IRCMessage
from irc_bot.utils import printable_unicode_list, is_channel, strip_irc_colors, strip_invisible, decode_html

log = logging.getLogger('irc_bot')


def partial(func, *args, **kwargs):
    """helper function to make sure __name__ is set"""
    f = functools.partial(func, *args, **kwargs)
    functools.update_wrapper(f, func)
    return f


class EventHandler(object):
    def __init__(self, func, command=None):
        self.func = func
        self.command = command


# Enum sort of
class IRCChannelStatus(object):
    IGNORE = -1
    NOT_CONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2
    PARTING = 3

    @property
    def enum_dict(self):
        return {
            -1: 'IGNORE',
            0: 'NOT_CONNECTED',
            1: 'CONNECTING',
            2: 'CONNECTED',
            3: 'PARTING'
        }


class SimpleIRCBot(asynchat.async_chat):
    ac_in_buffer_size = 8192
    ac_out_buffer_size = 8192

    def __init__(self, config):
        asynchat.async_chat.__init__(self)
        self.servers = config['servers']
        self.port = config['port']
        self.channels = {}
        self.channel_keys = {}
        for channel in config['channels']:
            parts = channel.split(' ', 1)
            if len(parts) > 1:
                self.add_irc_channel(parts[0])
                self.add_irc_channel_key(parts[0], parts[1])
            else:
                self.add_irc_channel(channel)
        self.nickname = config.get('nickname', 'SimpleIRCBot-%s' % uuid.uuid4())
        self.invite_nickname = config.get('invite_nickname')
        self.invite_message = config.get('invite_message')
        self.nickserv_password = config.get('nickserv_password')
        self.use_ssl = config.get('use_ssl', False)
        self.password = config.get('password')
        self.timeout = 0

        self.real_nickname = self.nickname
        self.set_terminator(b'\r\n')
        self.buffer = ''
        self.connection_attempts = 1
        self.max_connection_delay = 300  # 5 minutes
        self.throttled = False
        self.schedule = CommandScheduler()
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

    # some IRC channels have keys (channel passwords, +k mode)
    # you can provide one separated by a space after the channel name if necessary, and it will be used when joining the channel
    def add_irc_channel_key(self, name, key):
        name = name.lower() # doing this lower casing because other things here do it, it isn't relevant to keys specifically
        if name in self.channel_keys:
            return
        self.channel_keys[name] = key

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
            if self.password:
                self.write('PASS %s' % self.password)
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
        self.reset_channels()
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
            self.channels[channel] = None

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

            if channel in self.channel_keys:
                log.info('Joining channel with key: %s', channel)
                self.write('JOIN %s %s' % (channel, self.channel_keys[channel]))
            else:    
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
            self.request_channel_invite()
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
            log.info('PARTing from channels: %s', ','.join(channels))
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
        # sockets are garbage collected, but if the connection isn't closed it might fail
        try:
            self.socket.shutdown(socket.SHUT_WR)
            self.socket.close()
            del self.socket
        except Exception:
            pass
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
        self.socket.settimeout(30)
        self.socket.connect(server)
        self.handle_connect_event()

    def start(self):
        """The main loop keeping the bot alive"""
        self.connect(self.current_server)
        while self.running:
            # No need to busy-wait
            time.sleep(0.2)
            self.schedule.execute()
            # Skip polling etc. if we're reconnecting
            if self.reconnecting and not self.connected:
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

    def add_event_handler(self, func, commands=None):
        """

        :param func: Event handler function
        :param command: Command that triggers func
        :type func: function
        :return:
        """
        if not isinstance(commands, list):
            commands = [commands] if commands else []

        log.debug('Adding event handler %s for commands=%s', func.__name__, commands)
        hash = hashlib.md5('.'.join(sorted(commands)).encode('utf-8'))
        self.event_handlers[hash] = EventHandler(func, commands)

    def handle_event(self, msg):
        """Call the proper function that has been set to handle the input message type eg. RPLMOTD"""
        for regexp, event in self.event_handlers.items():
            if event.command:
                for cmd in event.command:
                    cmd = cmd.rstrip('$') + '$'
                    if re.match(cmd, msg.command, re.IGNORECASE):
                        event.func(msg)
