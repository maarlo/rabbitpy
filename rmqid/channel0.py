"""
Channel0 is used for connection level communication between RabbitMQ and the
client on channel 0.

"""
import locale
import logging
import Queue as queue
import sys
import threading

from pamqp import exceptions
from pamqp import header
from pamqp import specification

from rmqid import __version__
from rmqid import base

LOGGER = logging.getLogger(__name__)


class Channel0(threading.Thread, base.AMQPChannel):
    """Channel0 is used to negotiate a connection with RabbitMQ and for
    processing and dispatching events on channel 0 once connected.

    :param dict connection_args: Data required to negotiate the connection
    :param Queue.Queue inbound: Inbound frame queue
    :param Queue.Queue outbound: Outbound frame queue

    """
    CHANNEL = 0
    DEFAULT_CLOSE_CODE = 200
    DEFAULT_CLOSE_REASON = 'Normal Shutdown'

    def __init__(self, group=None, target=None, name=None, args=(), kwargs={}):
        super(Channel0, self).__init__(group, target, name, args, kwargs)
        self._channel_number = 0
        self._args = kwargs['connection_args']
        self._close_event = kwargs['close']
        self._on_connected = kwargs['connected']
        self._read_queue = kwargs['inbound']
        self._write_queue = kwargs['outbound']
        self._heartbeat = 0
        self._maximum_channels = 0
        self._state = self.CLOSED
        self.maximum_frame_size = specification.FRAME_MAX_SIZE
        self.minimum_frame_size = specification.FRAME_MIN_SIZE

    def close(self):
        self._write_frame(self._build_close_frame())
        self._set_state(self.CLOSED)

    def run(self):
        self._set_state(self.OPENING)
        self._write_protocol_header()
        if not self._connection_start():
            return
        self._connection_tune()
        self._connection_open()
        self._on_connected.set()
        self._set_state(self.OPEN)

        while self.open and not self._close_event.is_set():
            try:
                frame_value = self._read_queue.get(True, 0.5)
                LOGGER.debug('Received frame: %r', frame_value)
            except queue.Empty:
                pass
        self.close()

    def _build_close_frame(self):
        """Build and return the Connection.Close frame.

        :rtype: pamqp.specification.Connection.Close

        """
        return specification.Connection.Close(self.DEFAULT_CLOSE_CODE,
                                              self.DEFAULT_CLOSE_REASON)

    def _build_open_frame(self):
        """Build and return the Connection.Open frame.

        :rtype: pamqp.specification.Connection.Open

        """
        return specification.Connection.Open(self._args['virtual_host'])

    def _build_start_ok_frame(self):
        """Build and return the Connection.StartOk frame.

        :rtype: pamqp.specification.Connection.StartOk

        """
        version = sys.version_info
        properties = {'product': 'rmqid',
                      'platform': 'Python %s.%s.%s' % (version[0],
                                                       version[1],
                                                       version[2]),
                      'capabilities': {'basic.nack': True,
                                       'connection.blocked': True,
                                       'consumer_cancel_notify': True,
                                       'publisher_confirms': True},
                      'information': 'See https://github.com/gmr/rmqid',
                      'version': __version__}
        return specification.Connection.StartOk(client_properties=properties,
                                                response=self._credentials,
                                                locale=self._get_locale())

    def _build_tune_ok_frame(self):
        """Build and return the Connection.TuneOk frame.

        :rtype: pamqp.specification.Connection.TuneOk

        """
        return specification.Connection.TuneOk(self._maximum_channels,
                                               self.maximum_frame_size,
                                               self._heartbeat)

    def _connection_close(self):
        self._write_frame(self._build_close_frame())
        self._validate_frame(self._wait_on_frame(),
                             specification.Connection.CloseOk)

    def _connection_open(self):
        """Negotiate the Connection.Start process, writing out a
        Connection.StartOk frame when the Connection.Start frame is received.

        """
        self._write_frame(self._build_open_frame())
        frame_value = self._wait_on_frame()
        self._validate_frame(frame_value, specification.Connection.OpenOk)
        self._on_connected.set()

    def _connection_start(self):
        """Negotiate the Connection.Start process, writing out a
        Connection.StartOk frame when the Connection.Start frame is received.

        :rtype: bool

        """
        frame_value = self._wait_on_frame()
        self._validate_frame(frame_value, specification.Connection.Start)
        if not self._validate_connection_start(frame_value):
            LOGGER.error('Could not negotiate a connection, disconnecting')
            return False
        self._properties = frame_value.server_properties
        for key in self._properties:
            if key == 'capabilities':
                for capability in self._properties[key]:
                    LOGGER.debug('Server supports %s: %r',
                                 capability, self._properties[key][capability])
            else:
                LOGGER.debug('Server %s: %r', key, self._properties[key])
        self._write_frame(self._build_start_ok_frame())
        return True

    def _connection_tune(self):
        """Negotiate the Connection.Tune frames, waiting for the Connection.Tune
        frame from RabbitMQ and sending the Connection.TuneOk frame.

        """
        frame_value = self._wait_on_frame()
        self._validate_frame(frame_value, specification.Connection.Tune)

        self._maximum_channels = frame_value.channel_max
        if frame_value.frame_max != self.maximum_frame_size:
            self.maximum_frame_size = frame_value.frame_max
        if frame_value.heartbeat:
            self._heartbeat = frame_value.heartbeat
        self._write_frame(self._build_tune_ok_frame())

    @property
    def _credentials(self):
        """Return the marshaled credentials for the AMQP connection.

        :rtype: str

        """
        return '\0%s\0%s' % (self._args['username'], self._args['password'])

    def _get_locale(self):
        """Return the current locale for the python interpreter or the default
        locale.

        :rtype: str

        """
        if not self._args['locale']:
            return locale.getdefaultlocale()[0]
        return self._args['locale']

    def _process_server_rpc(self, value):
        """Process a RPC frame received from the server

        :param pamqp.message.Message value: The message value
        :rtype: rmqid.message.Message
        :raises: rmqid.exceptions.ChannelClosedException
        :raises: rmqid.exceptions.ConnectionClosedException

        """
        if value.name == 'Channel.Close':
            LOGGER.warning('Received remote close for channel %i', channel_id)
            self._channels[channel_id]._remote_close()
            raise exceptions.RemoteClosedChannelException(channel_id,
                                                          value.reply_code,
                                                          value.reply_text)
        elif value.name == 'Connection.Close':
            LOGGER.warning('Received remote close for the connection')
            self._set_state(self.CLOSED)
            raise exceptions.RemoteClosedException(value.reply_code,
                                                   value.reply_text)
        elif value.name == 'Connection.Blocked':
            LOGGER.warning('RabbitMQ has blocked the connection: %s',
                           value.reason)
            self._blocked = True

        elif value.name == 'Connection.Unblocked':
            LOGGER.warning('Connection is no longer blocked')
            self._blocked = False
        else:
            LOGGER.critical('Unhandled RPC request: %r', value)

    def _validate_connection_start(self, frame_value):
        """Validate the received Connection.Start frame

        :param specification.Connection.Start frame_value: The frame to validate
        :rtype: bool

        """
        if (frame_value.version_major,
            frame_value.version_minor) != (specification.VERSION[0],
                                           specification.VERSION[1]):
            LOGGER.warning('AMQP version mismatch, received %i.%i, expected %r',
                           frame_value.version_major, frame_value.version_minor,
                           specification.VERSION)
            return False
        return True

    def _write_protocol_header(self):
        """Send the protocol header to the connected server."""
        self._write_frame(header.ProtocolHeader())


