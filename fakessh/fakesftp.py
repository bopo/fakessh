# from __future__ import with_statement
import os
from pathlib import Path

import paramiko
import paramiko as ssh

from .filesystem import FakeFile
from .filesystem import FakeFilesystem

HOME = "/"

FILES = FakeFilesystem({
    "/file.txt": "contents",
    "/file2.txt": "contents2",
    "/folder/file3.txt": "contents3",
    "/empty_folder": None,
    "/tree/file1.txt": "x",
    "/tree/file2.txt": "y",
    "/tree/subfolder/file3.txt": "z",
    "/etc/apache2/apache2.conf": "Include other.conf",
    HOME: None,  # So $HOME is a directory
})


def _equalize(lists, fillval=None):
    """
    Pad all given list items in ``lists`` to be the same length.
    """
    lists = list(map(list, lists))
    upper = max(len(x) for x in lists)

    for lst in lists:
        diff = upper - len(lst)

        if diff:
            lst.extend([fillval] * diff)

    return lists


class FakeSFTPHandle(ssh.SFTPHandle):
    """
    Extremely basic way to get SFTPHandle working with our fake setup.
    """

    def __init__(self, flags: int = ...):
        super().__init__(flags)
        self.readfile = None

    def chattr(self, attr):  # noqa
        self.readfile.attributes = attr
        return ssh.sftp.SFTP_OK

    def stat(self):
        return self.readfile.attributes


class PrependList(list):
    def prepend(self, val):
        self.insert(0, val)


def expand(path):
    """
    '/foo/bar/biz' => ('/', 'foo', 'bar', 'biz')
    'relative/path' => ('relative', 'path')
    """
    # Base case
    if path in ["", os.sep]:
        return [path]

    ret = PrependList()
    directory, filename = os.path.split(path)

    while directory and directory != os.sep:
        ret.prepend(filename)
        directory, filename = os.path.split(directory)

    ret.prepend(filename)
    # Handle absolute vs relative paths
    ret.prepend(directory if directory == os.sep else "")

    return ret


def contains(folder, path):
    """
    contains(('a', 'b', 'c'), ('a', 'b')) => True
    contains('a', 'b', 'c'), ('f',)) => False
    """
    return False if len(path) >= len(folder) else folder[: len(path)] == path


def missing_folders(paths):
    """
    missing_folders(['a/b/c']) => ['a', 'a/b', 'a/b/c']
    """
    ret = []
    pool = set(paths)

    for path in paths:
        expanded = expand(path)

        for i in range(len(expanded)):
            # folder = os.path.join(*expanded[: len(expanded) - i])
            folder = Path(*expanded[: len(expanded) - i])
            folder = str(folder)

            if folder and folder not in pool:
                pool.add(folder)
                ret.append(folder)

    return ret


def canonicalize(path, home):
    ret = path

    if not Path(path).is_absolute():
        ret = Path(home, path)

    # if not os.path.isabs(path):
    #     ret = os.path.normpath(os.path.join(home, path))

    return str(ret)


class FakeSFTPServerInterface(ssh.SFTPServerInterface):
    def __init__(self, server, *args, **kwargs):
        super().__init__(server, *args, **kwargs)

        self.server = server
        # files = self.server.files  # noqa
        files = FILES

        # Expand such that omitted, implied folders get added explicitly
        for folder in missing_folders(files.keys()):
            files[folder] = None

        self.files = files

    def session_started(self):
        pass

    def session_ended(self):
        pass

    def canonicalize(self, path):
        """
        Make non-absolute paths relative to $HOME.
        """
        # return canonicalize(path, self.server.home)  # noqa
        return canonicalize(path, HOME)  # noqa

    def list_folder(self, path):
        path = self.files.normalize(path)

        expanded_files = map(expand, self.files)
        expanded_path = expand(path)

        candidates = [x for x in expanded_files if contains(x, expanded_path)]
        children = []

        for candidate in candidates:
            cut = candidate[: len(expanded_path) + 1]

            if cut not in children:
                children.append(cut)

        results = [self.stat(os.path.join(*x)) for x in children]
        bad = not results or any(x == ssh.sftp.SFTP_NO_SUCH_FILE for x in results)

        return ssh.sftp.SFTP_NO_SUCH_FILE if bad else results

    def open(self, path, flags, attr):
        path = self.files.normalize(path)

        try:
            fobj = self.files[path]
        except KeyError:
            if flags & os.O_WRONLY:
                # Only allow writes to files in existing directories.
                if os.path.dirname(path) not in self.files:
                    return ssh.sftp.SFTP_NO_SUCH_FILE

                self.files[path] = fobj = FakeFile("", path)
            # No write flag means a read, which means they tried to read a
            # nonexistent file.
            else:
                return ssh.sftp.SFTP_NO_SUCH_FILE

        f = FakeSFTPHandle()
        f.readfile = f.writefile = fobj

        return f

    def stat(self, path):
        path = self.files.normalize(path)

        try:
            fobj = self.files[path]
        except KeyError:
            return ssh.sftp.SFTP_NO_SUCH_FILE

        return fobj.attributes

    # Don't care about links right now
    lstat = stat

    def chattr(self, path, attr):
        path = self.files.normalize(path)

        if path not in self.files:
            return ssh.sftp.SFTP_NO_SUCH_FILE

        # Attempt to gracefully update instead of overwrite, since things like
        # chmod will call us with an SFTPAttributes object that only exhibits
        # e.g. st_mode, and we don't want to lose our filename or size...
        for which in "size uid gid mode atime mtime".split():
            attname = "st_" + which
            incoming = getattr(attr, attname)

            if incoming is not None:
                setattr(self.files[path].attributes, attname, incoming)

        return ssh.sftp.SFTP_OK

    def mkdir(self, path, attr):
        self.files[path] = None
        return ssh.sftp.SFTP_OK


class FakeSFTPServer(paramiko.SFTPServer):
    def __init__(self, channel, name, server, sftp_si=FakeSFTPServerInterface, *largs, **kwargs):
        super().__init__(channel, name, server, sftp_si=sftp_si, *largs, **kwargs)
