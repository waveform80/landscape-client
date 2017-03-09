"""File-system utils"""

import os
import time

from twisted.python.compat import long


def create_file(path, content):
    """Create a file with the given content.

    @param path: The path to the file.
    @param content: The content to be written in the file.
    """
    fd = open(path, "w")
    fd.write(content)
    fd.close()


def append_file(path, content):
    """Append a file with the given content.

    The file is created, if it doesn't exist already.

    @param path: The path to the file.
    @param content: The content to be written in the file at the end.
    """
    fd = open(path, "a")
    fd.write(content)
    fd.close()


def read_file(path, limit=None):
    """Return the content of the given file.

    @param path: The path to the file.
    @param limit: An optional read limit. If positive, read up to that number
        of bytes from the beginning of the file. If negative, read up to that
        number of bytes from the end of the file.
    @return content: The content of the file, possibly trimmed to C{limit}.
    """
    # In Python 3 opening a file in text mode does not permit an non-zero
    # offset seek from the end of the file, therefore we need binary mode. As
    # the function is used with the expectation of a string return value, we
    # have to decode. As a default encoding utf-8 was chosen, but this could be
    # made configurable later on.
    with open(path, "rb") as fd:
        if limit and os.path.getsize(path) > abs(limit):
            whence = 0
            if limit < 0:
                whence = 2
            fd.seek(limit, whence)
        content = fd.read()
    return content.decode('utf-8')


def touch_file(path, offset_seconds=None):
    """Touch a file, creating it if it doesn't exist.

    @param path: the path to the file to be touched.
    @param offset_seconds: a signed integer number of seconds to offset the
        atime and mtime of the file from the current time.

    """
    fd = open(path, "a")
    fd.close()
    if offset_seconds is not None:
        offset_time = long(time.time()) + offset_seconds
        touch_time = (offset_time, offset_time)
    else:
        touch_time = None
    os.utime(path, touch_time)
