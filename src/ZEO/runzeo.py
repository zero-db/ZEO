#! /usr/bin/env python
##############################################################################
#
# Copyright (c) 2001, 2002 Zope Corporation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.0 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE
#
##############################################################################
"""Start the ZEO storage server.

Usage: %s [-C URL] [-a ADDRESS] [-f FILENAME] [-h]

Options:
-C/--configuration URL -- configuration file or URL
-a/--address ADDRESS -- server address of the form PORT, HOST:PORT, or PATH
                        (a PATH must contain at least one "/")
-f/--filename FILENAME -- filename for FileStorage
-h/--help -- print this usage message and exit
-m/--monitor ADDRESS -- address of monitor server ([HOST:]PORT or PATH)

Unless -C is specified, -a and -f are required.
"""

# The code here is designed to be reused by other, similar servers.
# For the forseeable future, it must work under Python 2.1 as well as
# 2.2 and above.

import os
import sys
import getopt
import signal
import socket

import ZConfig, ZConfig.datatypes
import zLOG
from zdaemon.zdoptions import ZDOptions

def parse_address(arg):
    # XXX Unclear whether this is an official part of the ZConfig API
    obj = ZConfig.datatypes.SocketAddress(arg)
    return obj.family, obj.address

class ZEOOptions(ZDOptions):

    storages = None

    def handle_address(self, arg):
        self.family, self.address = parse_address(arg)

    def handle_monitor_address(self, arg):
        self.monitor_family, self.monitor_address = parse_address(arg)

    def handle_filename(self, arg):
        from ZODB.config import FileStorage # That's a FileStorage *opener*!
        class FSConfig:
            def __init__(self, name, path):
                self._name = name
                self.path = path
                self.create = 0
                self.read_only = 0
                self.stop = None
                self.quota = None
            def getSectionName(self):
                return self._name
        if not self.storages:
            self.storages = []
        name = str(1 + len(self.storages))
        conf = FileStorage(FSConfig(name, arg))
        self.storages.append(conf)

    def __init__(self):
        self.schemadir = os.path.dirname(__file__)
        ZDOptions.__init__(self)
        self.add(None, None, "a:", "address=", self.handle_address)
        self.add(None, None, "f:", "filename=", self.handle_filename)
        self.add("storages", "storages",
                 required="no storages specified; use -f or -C")
        self.add("family", "zeo.address.family")
        self.add("address", "zeo.address.address",
                 required="no server address specified; use -a or -C")
        self.add("read_only", "zeo.read_only", default=0)
        self.add("invalidation_queue_size", "zeo.invalidation_queue_size",
                 default=100)
        self.add("transaction_timeout", "zeo.transaction_timeout")
        self.add("monitor_address", None, "m:", "monitor=",
                 self.handle_monitor_address)

    def realize(self, *args):
        ZDOptions.realize(self, *args)
        if self.args:
            self.usage("positional arguments are not supported")
        self.load_logconf()

    def load_logconf(self):
        if self.configroot.logger is not None:
            zLOG.set_initializer(self.log_initializer)
            zLOG.initialize()

    def log_initializer(self):
        from zLOG import EventLogger
        logger = self.configroot.logger()
        for handler in logger.handlers:
            if hasattr(handler, "reopen"):
                handler.reopen()
        EventLogger.event_logger.logger = logger
    

class ZEOServer:

    def __init__(self, options):
        self.options = options

    def main(self):
        self.check_socket()
        self.clear_socket()
        try:
            self.open_storages()
            self.setup_signals()
            self.create_server()
            self.loop_forever()
        finally:
            self.close_storages()
            self.clear_socket()

    def check_socket(self):
        if self.can_connect(self.options.family, self.options.address):
            self.options.usage("address %s already in use" %
                               repr(self.options.address))

    def can_connect(self, family, address):
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            s.connect(address)
        except socket.error:
            return 0
        else:
            s.close()
            return 1

    def clear_socket(self):
        if isinstance(self.options.address, type("")):
            try:
                os.unlink(self.options.address)
            except os.error:
                pass

    def open_storages(self):
        self.storages = {}
        for opener in self.options.storages:
            info("opening storage %r using %s"
                 % (opener.name, opener.__class__.__name__))
            self.storages[opener.name] = opener.open()

    def setup_signals(self):
        """Set up signal handlers.

        The signal handler for SIGFOO is a method handle_sigfoo().
        If no handler method is defined for a signal, the signal
        action is not changed from its initial value.  The handler
        method is called without additional arguments.
        """
        if os.name != "posix":
            return
        if hasattr(signal, 'SIGXFSZ'):
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN) # Special case
        init_signames()
        for sig, name in signames.items():
            method = getattr(self, "handle_" + name.lower(), None)
            if method is not None:
                def wrapper(sig_dummy, frame_dummy, method=method):
                    method()
                signal.signal(sig, wrapper)

    def create_server(self):
        from ZEO.StorageServer import StorageServer
        self.server = StorageServer(
            self.options.address,
            self.storages,
            read_only=self.options.read_only,
            invalidation_queue_size=self.options.invalidation_queue_size,
            transaction_timeout=self.options.transaction_timeout,
            monitor_address=self.options.monitor_address)

    def loop_forever(self):
        import ThreadedAsync.LoopCallback
        ThreadedAsync.LoopCallback.loop()

    def handle_sigterm(self):
        info("terminated by SIGTERM")
        sys.exit(0)

    def handle_sigint(self):
        info("terminated by SIGINT")
        sys.exit(0)

    def handle_sigusr2(self):
        # This requires a modern zLOG (from Zope 2.6 or later); older
        # zLOG packages don't have the initialize() method
        info("reinitializing zLOG")
        # XXX Shouldn't this be below with _log()?
        import zLOG
        zLOG.initialize()
        info("reinitialized zLOG")

    def close_storages(self):
        for name, storage in self.storages.items():
            info("closing storage %r" % name)
            try:
                storage.close()
            except: # Keep going
                exception("failed to close storage %r" % name)


# Signal names

signames = None

def signame(sig):
    """Return a symbolic name for a signal.

    Return "signal NNN" if there is no corresponding SIG name in the
    signal module.
    """

    if signames is None:
        init_signames()
    return signames.get(sig) or "signal %d" % sig

def init_signames():
    global signames
    signames = {}
    for name, sig in signal.__dict__.items():
        k_startswith = getattr(name, "startswith", None)
        if k_startswith is None:
            continue
        if k_startswith("SIG") and not k_startswith("SIG_"):
            signames[sig] = name


# Log messages with various severities.
# This uses zLOG, but the API is a simplified version of PEP 282

def critical(msg):
    """Log a critical message."""
    _log(msg, zLOG.PANIC)

def error(msg):
    """Log an error message."""
    _log(msg, zLOG.ERROR)

def exception(msg):
    """Log an exception (an error message with a traceback attached)."""
    _log(msg, zLOG.ERROR, error=sys.exc_info())

def warn(msg):
    """Log a warning message."""
    _log(msg, zLOG.PROBLEM)

def info(msg):
    """Log an informational message."""
    _log(msg, zLOG.INFO)

def debug(msg):
    """Log a debugging message."""
    _log(msg, zLOG.DEBUG)

def _log(msg, severity=zLOG.INFO, error=None):
    """Internal: generic logging function."""
    zLOG.LOG("RUNSVR", severity, msg, "", error)


# Main program

def main(args=None):
    options = ZEOOptions()
    options.realize(args)
    s = ZEOServer(options)
    s.main()

if __name__ == "__main__":
    __file__ = sys.argv[0]
    main()
