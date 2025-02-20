# this code based on Daniel Krech's RDFLib HTTP client code (see rdflib.net)

import sys
import socket

from supervisor.compat import as_bytes
from supervisor.compat import as_string
from supervisor.compat import encodestring
from supervisor.compat import PY2
from supervisor.compat import urlparse
from supervisor.medusa import asynchat_25 as asynchat

CR = b'\x0d'
LF = b'\x0a'
CRLF = CR+LF

class Listener(object):

    def status(self, url, status):
        pass

    def error(self, url, error):
        sys.stderr.write("%s %s\n" % (url, error))

    def response_header(self, url, name, value):
        pass

    def done(self, url):
        pass

    def feed(self, url, data):
        try:
            sdata = as_string(data)
        except UnicodeDecodeError:
            sdata = 'Undecodable: %r' % data
        # We've got Unicode data in sdata now, but writing to stdout sometimes
        # fails - see issue #1231.
        try:
            sys.stdout.write(sdata)
        except UnicodeEncodeError:
            if PY2:
                # This might seem like The Wrong Thing To Do (writing bytes
                # rather than text to an output stream), but it seems to work
                # OK for Python 2.7.
                sys.stdout.write(data)
            else:
                s = ('Unable to write Unicode to stdout because it has '
                     'encoding %s' % sys.stdout.encoding)
                raise ValueError(s)
        sys.stdout.flush()

    def close(self, url):
        pass

class HTTPHandler(asynchat.async_chat):
    def __init__(
        self,
        listener,
        username='',
        password=None,
        conn=None,
        map=None
        ):
        asynchat.async_chat.__init__(self, conn, map)
        self.listener = listener
        self.user_agent = 'Supervisor HTTP Client'
        self.buffer = b''
        self.set_terminator(CRLF)
        self.connected = 0
        self.part = self.status_line
        self.chunk_size = 0
        self.chunk_read = 0
        self.length_read = 0
        self.length = 0
        self.encoding = None
        self.username = username
        self.password = password
        self.url = None
        self.error_handled = False

    def get(self, serverurl, path=''):
        if self.url is not None:
            raise AssertionError('Already doing a get')
        self.url = serverurl + path
        scheme, host, path_ignored, params, query, fragment = urlparse.urlparse(
            self.url)
        if not scheme in ("http", "unix"):
            raise NotImplementedError
        self.host = host
        if ":" in host:
            hostname, port = host.split(":", 1)
            port = int(port)
        else:
            hostname = host
            port = 80

        self.path = path
        self.port = port

        if scheme == "http":
            ip = hostname
            # socket.AF_INET: 服务器之间的网络通讯
            # socket.SOCK_STREAM: 流式socket,for TCP
            self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
            self.connect((ip, self.port))
        elif scheme == "unix":
            socketname = serverurl[7:]
            # socket.AF_UNIX:只能够用于单一的Unix系统进程间通讯
            self.create_socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.connect(socketname)

    def close(self):
        self.listener.close(self.url)
        self.connected = 0
        self.del_channel()
        self.socket.close()
        self.url = "CLOSED"

    def header(self, name, value):
        self.push('%s: %s' % (name, value))
        self.push(CRLF)

    def handle_error(self):
        if self.error_handled:
            return
        if 1 or self.connected:
            # sys.exc_info()，此函数返回一个三元组，提供有关当前正在处理
            # 的异常的信息 (type, value, traceback)
            # 返回的信息特定于当前线程和当前的堆栈帧，如果当前堆栈帧未处理
            # 异常，则从调用堆栈帧或其调用者获取信息，以此类推
            t,v,tb = sys.exc_info()
            msg = 'Cannot connect, error: %s (%s)' % (t, v)
            self.listener.error(self.url, msg)
            self.part = self.ignore
            self.close()
            self.error_handled = True
            del t
            del v
            del tb

    def handle_connect(self):
        self.connected = 1
        method = "GET"
        version = "HTTP/1.1"
        self.push("%s %s %s" % (method, self.path, version))
        self.push(CRLF)
        self.header("Host", self.host)

        self.header('Accept-Encoding', 'chunked')
        self.header('Accept', '*/*')
        self.header('User-agent', self.user_agent)
        if self.password:
            auth = '%s:%s' % (self.username, self.password)
            auth = as_string(encodestring(as_bytes(auth))).strip()
            self.header('Authorization', 'Basic %s' % auth)
        self.push(CRLF)
        self.push(CRLF)


    def feed(self, data):
        self.listener.feed(self.url, data)

    def collect_incoming_data(self, bytes):
        self.buffer = self.buffer + bytes
        if self.part==self.body:
            self.feed(self.buffer)
            self.buffer = b''

    def found_terminator(self):
        self.part()
        self.buffer = b''

    def ignore(self):
        self.buffer = b''

    def status_line(self):
        line = self.buffer

        version, status, reason = line.split(None, 2)
        status = int(status)
        if not version.startswith(b'HTTP/'):
            raise ValueError(line)

        self.listener.status(self.url, status)

        if status == 200:
            self.part = self.headers
        else:
            self.part = self.ignore
            msg = 'Cannot read, status code %s' % status
            self.listener.error(self.url, msg)
            self.close()
        return version, status, reason

    def headers(self):
        line = self.buffer
        if not line:
            if self.encoding == b'chunked':
                self.part = self.chunked_size
            else:
                self.part = self.body
                self.set_terminator(self.length)
        else:
            name, value = line.split(b':', 1)
            if name and value:
                name = name.lower()
                value = value.strip()
                if name == b'transfer-encoding':
                    self.encoding = value
                elif name == b'content-length':
                    self.length = int(value)
                self.response_header(name, value)

    def response_header(self, name, value):
        self.listener.response_header(self.url, name, value)

    def body(self):
        self.done()
        self.close()

    def done(self):
        self.listener.done(self.url)

    def chunked_size(self):
        line = self.buffer
        if not line:
            return
        chunk_size = int(line.split()[0], 16)
        if chunk_size==0:
            self.part = self.trailer
        else:
            self.set_terminator(chunk_size)
            self.part = self.chunked_body
        self.length += chunk_size

    def chunked_body(self):
        line = self.buffer
        self.set_terminator(CRLF)
        self.part = self.chunked_size
        self.feed(line)

    def trailer(self):
        # http://www.w3.org/Protocols/rfc2616/rfc2616-sec3.html#sec3.6.1
        # trailer        = *(entity-header CRLF)
        line = self.buffer
        if line == CRLF:
            self.done()
            self.close()
