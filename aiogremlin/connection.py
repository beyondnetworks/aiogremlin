"""
"""
import asyncio

import aiohttp

from .abc import AbstractBaseFactory, AbstractBaseConnection
from .exceptions import SocketClientError
from .log import INFO, conn_logger


class WebsocketPool:

    def __init__(self, uri='ws://localhost:8182/', factory=None, poolsize=10,
                 max_retries=10, timeout=None, loop=None, verbose=False):
        """
        """
        self.uri = uri
        self._factory = factory or AiohttpFactory
        self.poolsize = poolsize
        self.max_retries = max_retries
        self.timeout = timeout
        self._loop = loop or asyncio.get_event_loop()
        self.pool = asyncio.Queue(maxsize=self.poolsize, loop=self._loop)
        self.active_conns = set()
        self.num_connecting = 0
        self._closed = False
        if verbose:
            conn_logger.setLevel(INFO)

    @property
    def loop(self):
        return self._loop

    @property
    def factory(self):
        return self._factory

    @property
    def num_active_conns(self):
        return len(self.active_conns)

    def feed_pool(self, conn):
        self.active_conns.discard(conn)
        self._put(conn)

    @asyncio.coroutine
    def close(self):
        if not self._closed:
            if self.active_conns:
                yield from self._close_active_conns()
            yield from self._purge_pool()
            self._closed = True

    @asyncio.coroutine
    def _close_active_conns(self):
        tasks = [asyncio.async(conn.close(), loop=self.loop) for conn
            in self.active_conns]
        yield from asyncio.wait(tasks, loop=self.loop)

    @asyncio.coroutine
    def _purge_pool(self):
        while True:
            try:
                conn = self.pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                yield from conn.close()

    @asyncio.coroutine
    def connect(self, uri=None, loop=None, num_retries=None):
        if num_retries is None:
            num_retries = self.max_retries
        uri = uri or self.uri
        loop = loop or self.loop
        if not self.pool.empty():
            socket = self.pool.get_nowait()
            conn_logger.info("Reusing socket: {} at {}".format(socket, uri))
        elif (self.num_active_conns + self.num_connecting >= self.poolsize or
            not self.poolsize):
            conn_logger.info("Waiting for socket...")
            socket = yield from asyncio.wait_for(self.pool.get(),
                self.timeout, loop=loop)
            conn_logger.info("Socket acquired: {} at {}".format(socket, uri))
        else:
            self.num_connecting += 1
            try:
                socket = yield from self.factory.connect(uri, pool=self,
                    loop=loop)
            except:
                raise
            else:
                conn_logger.info("New connection on socket: {} at {}".format(
                    socket, uri))
            finally:
                self.num_connecting -= 1
        if not socket.closed:
            self.active_conns.add(socket)
        # Untested.
        elif num_retries > 0:
            socket = yield from self.connect(uri, loop, num_retries - 1)
        else:
            raise RuntimeError("Unable to connect, max retries exceeded.")
        return socket

    def _put(self, socket):
        try:
            self.pool.put_nowait(socket)
        except asyncio.QueueFull:
            pass


class AiohttpFactory(AbstractBaseFactory):

    @classmethod
    @asyncio.coroutine
    def connect(cls, uri='ws://localhost:8182/', pool=None, protocols=(),
                connector=None, autoclose=False, autoping=False, loop=None):
        if pool:
            loop = loop or pool.loop
        try:
            socket = yield from aiohttp.ws_connect(uri, protocols=protocols,
                connector=connector, autoclose=autoclose, autoping=autoping,
                loop=loop)
        except aiohttp.WSServerHandshakeError as e:
            raise SocketClientError(e.message)
        return AiohttpConnection(socket, pool)


class AiohttpConnection(AbstractBaseConnection):

    def __init__(self, socket, pool=None):
        super().__init__(socket, pool=pool)

    def __str__(self):
        return "{} wrapping {}".format(repr(self), repr(self.socket))

    @property
    def closed(self):
        return self.socket.closed

    @asyncio.coroutine
    def close(self):
        yield from self.socket.close()

    @asyncio.coroutine
    def send(self, message, binary=True):
        if binary:
            method = self.socket.send_bytes
        else:
            method = self.socket.send_str
        try:
            method(message)
        except RuntimeError:
            # Socket closed.
            yield from self.release()
            raise
        except TypeError:
            # Bytes/string input error.
            yield from self.release()
            raise

    @asyncio.coroutine
    def recv(self):
        while True:
            try:
                message = yield from self.socket.receive()
            except (asyncio.CancelledError, asyncio.TimeoutError):
                yield from self.release()
                raise
            except RuntimeError:
                yield from self.release()
                raise
            if message.tp == aiohttp.MsgType.binary:
                return message.data.decode()
            elif message.tp == aiohttp.MsgType.text:
                return message.data.strip()
            elif message.tp == aiohttp.MsgType.ping:
                conn_logger.warn("Ping received.")
                ws.pong()
                conn_logger.warn("Sent pong.")
            elif message.tp == aiohttp.MsgType.pong:
                conn_logger.warn('Pong received')
            else:
                try:
                    if message.tp == aiohttp.MsgType.release:
                        conn_logger.warn("Socket connection closed by server.")
                    elif message.tp == aiohttp.MsgType.error:
                        raise SocketClientError(self.socket.exception())
                    elif message.tp == aiohttp.MsgType.closed:
                        raise SocketClientError("Socket closed.")
                    break
                finally:
                    yield from self.release()
