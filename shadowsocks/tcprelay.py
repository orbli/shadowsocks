#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright (c) 2014 clowwindy
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import time
import socket
import errno
import struct
import logging
import encrypt
import eventloop
from common import parse_header


TIMEOUTS_CLEAN_SIZE = 512
TIMEOUT_PRECISION = 4

CMD_CONNECT = 1
CMD_BIND = 2
CMD_UDP_ASSOCIATE = 3

# local:
# stage 0 init
# stage 1 hello received, hello sent
# stage 2 UDP assoc
# stage 4 addr received, reply sent
# stage 5 remote connected

# remote:
# stage 0 init
# stage 4 addr received, reply sent
# stage 5 remote connected

STAGE_INIT = 0
STAGE_HELLO = 1
STAGE_UDP_ASSOC = 2
STAGE_REPLY = 4
STAGE_STREAM = 5

# stream direction
STREAM_UP = 0
STREAM_DOWN = 1

# stream wait status
WAIT_STATUS_INIT = 0
WAIT_STATUS_READING = 1
WAIT_STATUS_WRITING = 2
WAIT_STATUS_READWRITING = WAIT_STATUS_READING | WAIT_STATUS_WRITING

BUF_SIZE = 8 * 1024


class TCPRelayHandler(object):
    def __init__(self, server, fd_to_handlers, loop, local_sock, config,
                 is_local):
        self._server = server
        self._fd_to_handlers = fd_to_handlers
        self._loop = loop
        self._local_sock = local_sock
        self._remote_sock = None
        self._config = config
        self._is_local = is_local
        self._stage = STAGE_INIT
        self._encryptor = encrypt.Encryptor(config['password'],
                                            config['method'])
        self._data_to_write_to_local = []
        self._data_to_write_to_remote = []
        self._upstream_status = WAIT_STATUS_READING
        self._downstream_status = WAIT_STATUS_INIT
        self._remote_address = None
        fd_to_handlers[local_sock.fileno()] = self
        local_sock.setblocking(False)
        local_sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        loop.add(local_sock, eventloop.POLL_IN | eventloop.POLL_ERR)
        self.last_activity = 0
        self.update_activity()

    def __hash__(self):
        # default __hash__ is id / 16
        # we want to eliminate collisions
        return id(self)

    @property
    def remote_address(self):
        return self._remote_address

    def update_activity(self):
        self._server.update_activity(self)

    def update_stream(self, stream, status):
        dirty = False
        if stream == STREAM_DOWN:
            if self._downstream_status != status:
                self._downstream_status = status
                dirty = True
        elif stream == STREAM_UP:
            if self._upstream_status != status:
                self._upstream_status = status
                dirty = True
        if dirty:
            if self._local_sock:
                event = eventloop.POLL_ERR
                if self._downstream_status & WAIT_STATUS_WRITING:
                    event |= eventloop.POLL_OUT
                if self._upstream_status & WAIT_STATUS_READING:
                    event |= eventloop.POLL_IN
                self._loop.modify(self._local_sock, event)
            if self._remote_sock:
                event = eventloop.POLL_ERR
                if self._downstream_status & WAIT_STATUS_READING:
                    event |= eventloop.POLL_IN
                if self._upstream_status & WAIT_STATUS_WRITING:
                    event |= eventloop.POLL_OUT
                self._loop.modify(self._remote_sock, event)

    def write_to_sock(self, data, sock):
        if not data or not sock:
            return
        uncomplete = False
        try:
            l = len(data)
            s = sock.send(data)
            if s < l:
                data = data[s:]
                uncomplete = True
        except (OSError, IOError) as e:
            error_no = eventloop.errno_from_exception(e)
            if error_no in (errno.EAGAIN, errno.EINPROGRESS):
                uncomplete = True
            else:
                logging.error(e)
                self.destroy()
        if uncomplete:
            if sock == self._local_sock:
                self._data_to_write_to_local.append(data)
                self.update_stream(STREAM_DOWN, WAIT_STATUS_WRITING)
            elif sock == self._remote_sock:
                self._data_to_write_to_remote.append(data)
                self.update_stream(STREAM_UP, WAIT_STATUS_WRITING)
            else:
                logging.error('write_all_to_sock:unknown socket')
        else:
            if sock == self._local_sock:
                self.update_stream(STREAM_DOWN, WAIT_STATUS_READING)
            elif sock == self._remote_sock:
                self.update_stream(STREAM_UP, WAIT_STATUS_READING)
            else:
                logging.error('write_all_to_sock:unknown socket')

    def on_local_read(self):
        self.update_activity()
        if not self._local_sock:
            return
        is_local = self._is_local
        data = None
        try:
            data = self._local_sock.recv(BUF_SIZE)
        except (OSError, IOError) as e:
            if eventloop.errno_from_exception(e) in \
                    (errno.ETIMEDOUT, errno.EAGAIN):
                return
        if not data:
            self.destroy()
            return
        if not is_local:
            data = self._encryptor.decrypt(data)
            if not data:
                return
        if self._stage == STAGE_STREAM:
            if self._is_local:
                data = self._encryptor.encrypt(data)
            self.write_to_sock(data, self._remote_sock)
            return
        elif is_local and self._stage == STAGE_INIT:
            # TODO check auth method
            self.write_to_sock('\x05\00', self._local_sock)
            self._stage = STAGE_HELLO
            return
        elif self._stage == STAGE_REPLY:
            if is_local:
                data = self._encryptor.encrypt(data)
            self._data_to_write_to_remote.append(data)
        elif (is_local and self._stage == STAGE_HELLO) or \
                (not is_local and self._stage == STAGE_INIT):
            try:
                if is_local:
                    cmd = ord(data[1])
                    if cmd == CMD_UDP_ASSOCIATE:
                        logging.debug('UDP associate')
                        if self._local_sock.family == socket.AF_INET6:
                            header = '\x05\x00\x00\x04'
                        else:
                            header = '\x05\x00\x00\x01'
                        addr, port = self._local_sock.getsockname()
                        addr_to_send = socket.inet_pton(self._local_sock.family,
                                                        addr)
                        port_to_send = struct.pack('>H', port)
                        self.write_to_sock(header + addr_to_send + port_to_send,
                                           self._local_sock)
                        self._stage = STAGE_UDP_ASSOC
                        # just wait for the client to disconnect
                        return
                    elif cmd == CMD_CONNECT:
                        # just trim VER CMD RSV
                        data = data[3:]
                    else:
                        logging.error('unknown command %d', cmd)
                        self.destroy()
                        return
                header_result = parse_header(data)
                if header_result is None:
                    raise Exception('can not parse header')
                addrtype, remote_addr, remote_port, header_length =\
                    header_result
                logging.debug('connecting %s:%d' % (remote_addr, remote_port))
                self._remote_address = (remote_addr, remote_port)
                if is_local:
                    # forward address to remote
                    self.write_to_sock('\x05\x00\x00\x01' +
                                       '\x00\x00\x00\x00\x10\x10',
                                       self._local_sock)
                    data_to_send = self._encryptor.encrypt(data)
                    self._data_to_write_to_remote.append(data_to_send)
                    remote_addr = self._config['server']
                    remote_port = self._config['server_port']
                else:
                    if len(data) > header_length:
                        self._data_to_write_to_remote.append(
                            data[header_length:])

                # TODO async DNS
                addrs = socket.getaddrinfo(remote_addr, remote_port, 0,
                                           socket.SOCK_STREAM, socket.SOL_TCP)
                if len(addrs) == 0:
                    raise Exception("can't get addrinfo for %s:%d" %
                                    (remote_addr, remote_port))
                af, socktype, proto, canonname, sa = addrs[0]
                remote_sock = socket.socket(af, socktype, proto)
                self._remote_sock = remote_sock
                self._fd_to_handlers[remote_sock.fileno()] = self
                remote_sock.setblocking(False)
                remote_sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
                # TODO support TCP fast open
                try:
                    remote_sock.connect(sa)
                except (OSError, IOError) as e:
                    if eventloop.errno_from_exception(e) == errno.EINPROGRESS:
                        pass
                self._loop.add(remote_sock,
                               eventloop.POLL_ERR | eventloop.POLL_OUT)

                self._stage = STAGE_REPLY
                self.update_stream(STREAM_UP, WAIT_STATUS_READWRITING)
                self.update_stream(STREAM_DOWN, WAIT_STATUS_READING)
                return
            except Exception:
                import traceback
                traceback.print_exc()
                # TODO use logging when debug completed
                self.destroy()

    def on_remote_read(self):
        self.update_activity()
        data = None
        try:
            data = self._remote_sock.recv(BUF_SIZE)
        except (OSError, IOError) as e:
            if eventloop.errno_from_exception(e) in \
                    (errno.ETIMEDOUT, errno.EAGAIN):
                return
        if not data:
            self.destroy()
            return
        if self._is_local:
            data = self._encryptor.decrypt(data)
        else:
            data = self._encryptor.encrypt(data)
        try:
            self.write_to_sock(data, self._local_sock)
        except Exception:
            import traceback
            traceback.print_exc()
            # TODO use logging when debug completed
            self.destroy()

    def on_local_write(self):
        if self._data_to_write_to_local:
            data = ''.join(self._data_to_write_to_local)
            self._data_to_write_to_local = []
            self.write_to_sock(data, self._local_sock)
        else:
            self.update_stream(STREAM_DOWN, WAIT_STATUS_READING)

    def on_remote_write(self):
        self._stage = STAGE_STREAM
        if self._data_to_write_to_remote:
            data = ''.join(self._data_to_write_to_remote)
            self._data_to_write_to_remote = []
            self.write_to_sock(data, self._remote_sock)
        else:
            self.update_stream(STREAM_UP, WAIT_STATUS_READING)

    def on_local_error(self):
        logging.error(eventloop.get_sock_error(self._local_sock))
        self.destroy()

    def on_remote_error(self):
        logging.error(eventloop.get_sock_error(self._remote_sock))
        self.destroy()

    def handle_event(self, sock, event):
        # order is important
        if sock == self._remote_sock:
            if event & eventloop.POLL_IN:
                self.on_remote_read()
            if event & eventloop.POLL_OUT:
                self.on_remote_write()
            if event & eventloop.POLL_ERR:
                self.on_remote_error()
        elif sock == self._local_sock:
            if event & eventloop.POLL_IN:
                self.on_local_read()
            if event & eventloop.POLL_OUT:
                self.on_local_write()
            if event & eventloop.POLL_ERR:
                self.on_local_error()
        else:
            logging.warn('unknown socket')

    def destroy(self):
        if self._remote_address:
            logging.debug('destroy: %s:%d' %
                          self._remote_address)
        else:
            logging.debug('destroy')
        if self._remote_sock:
            self._loop.remove(self._remote_sock)
            del self._fd_to_handlers[self._remote_sock.fileno()]
            self._remote_sock.close()
            self._remote_sock = None
        if self._local_sock:
            self._loop.remove(self._local_sock)
            del self._fd_to_handlers[self._local_sock.fileno()]
            self._local_sock.close()
            self._local_sock = None
        self._server.remove_handler(self)


class TCPRelay(object):
    def __init__(self, config, is_local):
        self._config = config
        self._is_local = is_local
        self._closed = False
        self._eventloop = None
        self._fd_to_handlers = {}
        self._last_time = time.time()

        self._timeout = config['timeout']
        self._timeouts = []  # a list for all the handlers
        self._timeout_offset = 0  # last checked position for timeout
                                  # we trim the timeouts once a while
        self._handler_to_timeouts = {}  # key: handler value: index in timeouts

        if is_local:
            listen_addr = config['local_address']
            listen_port = config['local_port']
        else:
            listen_addr = config['server']
            listen_port = config['server_port']

        addrs = socket.getaddrinfo(listen_addr, listen_port, 0,
                                   socket.SOCK_STREAM, socket.SOL_TCP)
        if len(addrs) == 0:
            raise Exception("can't get addrinfo for %s:%d" %
                            (listen_addr, listen_port))
        af, socktype, proto, canonname, sa = addrs[0]
        server_socket = socket.socket(af, socktype, proto)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(sa)
        server_socket.setblocking(False)
        server_socket.listen(1024)
        self._server_socket = server_socket

    def add_to_loop(self, loop):
        if self._closed:
            raise Exception('already closed')
        self._eventloop = loop
        loop.add_handler(self._handle_events)

        self._eventloop.add(self._server_socket,
                            eventloop.POLL_IN | eventloop.POLL_ERR)

    def remove_handler(self, handler):
        index = self._handler_to_timeouts.get(hash(handler), -1)
        if index >= 0:
            # delete is O(n), so we just set it to None
            self._timeouts[index] = None
            del self._handler_to_timeouts[hash(handler)]

    def update_activity(self, handler):
        """ set handler to active """
        now = int(time.time())
        if now - handler.last_activity < TIMEOUT_PRECISION:
            # thus we can lower timeout modification frequency
            return
        handler.last_activity = now
        index = self._handler_to_timeouts.get(hash(handler), -1)
        if index >= 0:
            # delete is O(n), so we just set it to None
            self._timeouts[index] = None
        length = len(self._timeouts)
        self._timeouts.append(handler)
        self._handler_to_timeouts[hash(handler)] = length

    def _sweep_timeout(self):
        # tornado's timeout memory management is more flexible that we need
        # we just need a sorted last_activity queue and it's faster that heapq
        # in fact we can do O(1) insertion/remove so we invent our own
        if self._timeouts:
            now = time.time()
            length = len(self._timeouts)
            pos = self._timeout_offset
            while pos < length:
                handler = self._timeouts[pos]
                if handler:
                    if now - handler.last_activity < self._timeout:
                        break
                    else:
                        if handler.remote_address:
                            logging.warn('timed out: %s:%d' %
                                         handler.remote_address)
                        else:
                            logging.warn('timed out')
                        handler.destroy()
                        self._timeouts[pos] = None  # free memory
                        pos += 1
                else:
                    pos += 1
            if pos > TIMEOUTS_CLEAN_SIZE and pos > length >> 1:
                # clean up the timeout queue when it gets larger than half
                # of the queue
                self._timeouts = self._timeouts[pos:]
                for key in self._handler_to_timeouts:
                    self._handler_to_timeouts[key] -= pos
                pos = 0
            self._timeout_offset = pos

    def _handle_events(self, events):
        for sock, fd, event in events:
            # if sock:
            #     logging.debug('fd %d %s', fd,
            #                   eventloop.EVENT_NAMES.get(event, event))
            if sock == self._server_socket:
                if event & eventloop.POLL_ERR:
                    # TODO
                    raise Exception('server_socket error')
                try:
                    # logging.debug('accept')
                    conn = self._server_socket.accept()
                    TCPRelayHandler(self, self._fd_to_handlers, self._eventloop,
                                    conn[0], self._config, self._is_local)
                except (OSError, IOError) as e:
                    error_no = eventloop.errno_from_exception(e)
                    if error_no in (errno.EAGAIN, errno.EINPROGRESS):
                        continue
                    else:
                        logging.error(e)
            else:
                if sock:
                    handler = self._fd_to_handlers.get(fd, None)
                    if handler:
                        handler.handle_event(sock, event)
                else:
                    logging.warn('poll removed fd')

        now = time.time()
        if now - self._last_time > TIMEOUT_PRECISION:
            self._sweep_timeout()
            self._last_time = now

    def close(self):
        self._closed = True
        self._server_socket.close()
