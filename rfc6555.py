# Copyright 2021 Seth Michael Larson
# Copyright 2024 Julien Castiaux
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Python implementation of the Happy Eyeballs Algorithm described in RFC 6555."""

__all__ = ["cache", "create_connection"]

import atexit
import concurrent.futures
import contextlib
import errno
import socket
from asyncio.base_event import _ipaddr_info
from selectors import EVENT_WRITE, DefaultSelector
from time import perf_counter

RFC6555_ENABLED = None  # True: always, False: never, None: if host supports ipv6
_HAS_IPv6 = None

# These are error numbers for asynchronous operations which can
# be safely ignored by RFC 6555 as being non-errors.
_ASYNC_ERRNOS = {errno.EINPROGRESS, errno.EAGAIN, errno.EWOULDBLOCK}
if hasattr(errno, "WSAWOULDBLOCK"):
    _ASYNC_ERRNOS.add(errno.WSAWOULDBLOCK)

_DEFAULT_CACHE_DURATION = 60 * 10  # 10 minutes according to the RFC.


class _RFC6555CacheManager:
    def __init__(self):
        self.validity_duration = _DEFAULT_CACHE_DURATION
        self.entries = {}

    def add_entry(self, address, family):
        current_time = perf_counter()

        # Don't over-write old entries to reset their expiry.
        if address not in self.entries or self.entries[address][1] > current_time:
            self.entries[address] = (family, current_time + self.validity_duration)

    def get_entry(self, address):
        if address not in self.entries:
            return None

        family, expiry = self.entries[address]
        if perf_counter() > expiry:
            del self.entries[address]
            return None

        return family


cache = _RFC6555CacheManager()
thread_pool = concurrent.futures.ThreadPoolExecutor(2, "rfc6555")
atexit.register(thread_pool.shutdown)


class _RFC6555ConnectionManager:
    def __init__(
        self, *addresses, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None
    ):
        self.addresses = addresses
        self.timeout = timeout
        self.source_address = source_address

        self._error = None
        self._selector = DefaultSelector()
        self._sockets = []
        self._start_time = None

    def create_connection(self):
        self._start_time = perf_counter()

        addr_info = self._resolve(self.addresses)

        ret = self._connect_with_cached_family(addr_info)

        # If it's a list, then these are the remaining values to try.
        if isinstance(ret, list):
            addr_info = ret
        elif cache is not None:
            for address in self.addresses:
                cache.add_entry(address, ret.family)
            return ret

        # If we don't get any results back then just skip to the end.
        if not addr_info:
            e = "getaddrinfo returns an empty list"
            raise OSError(e)

        sock = self._attempt_connect_with_addr_info(addr_info)

        if sock:
            if cache is not None:
                for address in self.addresses:
                    cache.add_entry(address, sock.family)
            return sock
        if self._error:
            raise self._error
        raise TimeoutError

    def _resolve(self, addresses):
        resolved = []
        to_resolve = []

        # separate the address already resolved
        for address in addresses:
            kw = {"family": socket.AF_UNSPEC, "type": socket.SOCK_STREAM}
            kw["host"], kw["port"], flowinfo, scopeid, *_ = (*address, 0, 0)
            if info := _ipaddr_info(**kw, flowinfo=flowinfo, scopeid=scopeid):
                resolved.append(info)
            else:
                to_resolve.append(kw)

        if to_resolve:
            futures = [thread_pool.submit(socket.getaddrinfo, **kw) for kw in to_resolve]
            # resolve as many addresses as possible in .2 seconds
            with contextlib.suppress(TimeoutError):
                concurrent.futures.wait(
                    futures,
                    timeout=self._get_select_time(),
                    return_when=concurrent.futures.ALL_COMPLETED
                )
            if not resolved and not any(f.done() and not f.exception() for f in futures):
                # no address resolved so far, w
                with contextlib.suppress(TimeoutError):
                    concurrent.futures.wait(
                        futures,
                        timeout=self._get_remaining_time(),
                        return_when=concurrent.futures.FIRST_COMPLETED
                    )

            resolved.extend(f.result() for f in futures if f.done() and not f.exception())

        return resolved


    def _attempt_connect_with_addr_info(self, addr_info):
        sock = None
        try:
            for family, socktype, proto, _, sockaddr in addr_info:
                self._create_socket(family, socktype, proto, sockaddr)
                sock = self._wait_for_connection(last_wait=False)
                if sock:
                    break
            if sock is None:
                sock = self._wait_for_connection(last_wait=True)
        finally:
            self._remove_all_sockets()
        return sock

    def _connect_with_cached_family(self, addr_info):
        if cache is not None:
            for address in self.addresses:
                family = cache.get_entry(address)
                if family is None:
                    return addr_info

        is_family = []
        not_family = []

        for value in addr_info:
            if value[0] == family:
                is_family.append(value)
            else:
                not_family.append(value)

        sock = self._attempt_connect_with_addr_info(is_family)
        if sock is not None:
            return sock

        return not_family

    def _create_socket(self, family, socktype, proto, sockaddr):
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)

            # If we're using the 'default' socket timeout we have
            # to set it to a real value here as this is the earliest
            # opportunity to without pre-allocating a socket just for
            # this purpose.
            if self.timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
                self.timeout = sock.gettimeout()

            if self.source_address:
                sock.bind(self.source_address)

            # Make the socket non-blocking so we can use our selector.
            sock.settimeout(0.0)

            if self._is_acceptable_errno(sock.connect_ex(sockaddr)):
                self._selector.register(sock, EVENT_WRITE)
                self._sockets.append(sock)

        except OSError as e:
            self._error = e
            if sock is not None:
                _RFC6555ConnectionManager._close_socket(sock)

    def _wait_for_connection(self, last_wait):
        self._remove_all_errored_sockets()

        # This is a safe-guard to make sure sock.gettimeout() is called in the
        # case that the default socket timeout is used. If there are no
        # sockets then we may not have called sock.gettimeout() yet.
        if not self._sockets:
            return None

        # If this is the last time we're waiting for connections
        # then we should wait until we should raise a timeout
        # error, otherwise we should only wait >0.2 seconds as
        # recommended by RFC 6555.
        select_timeout = (
                 None if self.timeout is None
            else self._get_remaining_time() if last_wait
            else self._get_select_time()
        )

        # Wait for any socket to become writable as a sign of being connected.
        for key, _ in self._selector.select(select_timeout):
            sock = key.fileobj

            if not self._is_socket_errored(sock):

                # Restore the old proper timeout of the socket.
                sock.settimeout(self.timeout)

                # Remove it from this list to exempt the socket from cleanup.
                self._sockets.remove(sock)
                self._selector.unregister(sock)
                return sock

        return None

    def _get_remaining_time(self):
        if self.timeout in (None, socket._GLOBAL_DEFAULT_TIMEOUT):
            return None
        return max(self.timeout - (perf_counter() - self._start_time), 0.0)

    def _get_select_time(self):
        if self.timeout is None:
            return 0.2
        return min(0.2, self._get_remaining_time())

    def _remove_all_errored_sockets(self):
        for sock in list(filter(self._is_socket_errored, self._sockets)):
            self._selector.unregister(sock)
            self._sockets.remove(sock)
            _RFC6555ConnectionManager._close_socket(sock)

    @staticmethod
    def _close_socket(sock):
        with contextlib.suppress(OSError):
            sock.close()

    def _is_acceptable_errno(self, errno):
        if errno == 0 or errno in _ASYNC_ERRNOS:
            return True
        self._error = OSError()
        self._error.errno = errno
        return False

    def _is_socket_errored(self, sock):
        errno = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        return not self._is_acceptable_errno(errno)

    def _remove_all_sockets(self):
        for sock in self._sockets:
            self._selector.unregister(sock)
            _RFC6555ConnectionManager._close_socket(sock)
        self._sockets = []


def create_connection(
    *addresses, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None
):
    global RFC6555_ENABLED, _HAS_IPv6  # noqa: PLW0603
    if RFC6555_ENABLED is None:
        if _HAS_IPv6 is None:
            _HAS_IPv6 = _detect_ipv6()
        RFC6555_ENABLED = _HAS_IPv6
    if RFC6555_ENABLED:
        manager = _RFC6555ConnectionManager(
            *addresses,
            timeout=timeout,
            source_address=source_address
        )
        return manager.create_connection()
    return socket.create_connection(addresses[0], timeout, source_address)


def _detect_ipv6():
    """Detect whether an IPv6 socket can be allocated."""
    if getattr(socket, "has_ipv6", False) and hasattr(socket, "AF_INET6"):
        _sock = None
        try:
            _sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            _sock.bind(("::1", 0))
        except OSError:
            if _sock:
                _sock.close()
        else:
            return True
    return False
