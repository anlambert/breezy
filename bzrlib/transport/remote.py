# Copyright (C) 2006 Canonical Ltd
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

"""RemoteTransport client for the smart-server.

This module shouldn't be accessed directly.  The classes defined here should be
imported from bzrlib.smart.
"""

__all__ = ['RemoteTransport', 'RemoteTCPTransport', 'RemoteSSHTransport']

from cStringIO import StringIO
import urllib
import urlparse

from bzrlib import (
    errors,
    transport,
    urlutils,
    )
from bzrlib.smart import client, medium, protocol

# must do this otherwise urllib can't parse the urls properly :(
for scheme in ['ssh', 'bzr', 'bzr+loopback', 'bzr+ssh', 'bzr+http']:
    transport.register_urlparse_netloc_protocol(scheme)
del scheme


# Port 4155 is the default port for bzr://, registered with IANA.
BZR_DEFAULT_INTERFACE = '0.0.0.0'
BZR_DEFAULT_PORT = 4155


class _SmartStat(object):

    def __init__(self, size, mode):
        self.st_size = size
        self.st_mode = mode


class RemoteTransport(transport.ConnectedTransport):
    """Connection to a smart server.

    The connection holds references to the medium that can be used to send
    requests to the server.

    The connection has a notion of the current directory to which it's
    connected; this is incorporated in filenames passed to the server.
    
    This supports some higher-level RPC operations and can also be treated 
    like a Transport to do file-like operations.

    The connection can be made over a tcp socket, an ssh pipe or a series of
    http requests.  There are concrete subclasses for each type:
    RemoteTCPTransport, etc.
    """

    # IMPORTANT FOR IMPLEMENTORS: RemoteTransport MUST NOT be given encoding
    # responsibilities: Put those on SmartClient or similar. This is vital for
    # the ability to support multiple versions of the smart protocol over time:
    # RemoteTransport is an adapter from the Transport object model to the 
    # SmartClient model, not an encoder.

    # FIXME: the medium parameter should be private, only the tests requires
    # it. It may be even clearer to define a TestRemoteTransport that handles
    # the specific cases of providing a _client and/or a _medium, and leave
    # RemoteTransport as an abstract class.
    def __init__(self, url, from_transport=None, medium=None, _client=None):
        """Constructor.

        :param from_transport: Another RemoteTransport instance that this
            one is being cloned from.  Attributes such as the medium will
            be reused.

        :param medium: The medium to use for this RemoteTransport. This must be
            supplied if from_transport is None.

        :param _client: Override the _SmartClient used by this transport.  This
            should only be used for testing purposes; normally this is
            determined from the medium.
        """
        super(RemoteTransport, self).__init__(url, from_transport)

        if medium is None:
            medium = self._build_medium(from_transport)
        else:
            self._set_connection(medium, None)
        self._medium = medium
        assert self._medium is not None
        # ConnectedTransport provides the connection sharing by requiring
        # daughter classes to always access the connection via _get_connection
        # and guarantee that the connection wil be shared across all transports
        # even if the server force a reconnection (i.e. the connection object
        # should be built again).

        # For RemoteTransport objects, the medium is the connection. But using
        # _get_connection is not enough because:
        # - self._client needs a medium to be built (and keep an internal
        #   copy).
        # - RemoteBzrDir RemoteRepository, RemoteRepositoryFormat RemoteBranch
        #   keep internal copies of _SmartClient objects.

        # Therefore, the needed refactoring (enhancing _SmartClient and its
        # users so that connection are still shared even in cases or
        # reconnections) is too much for this round -- vila20070607

        # On the other hand, except for the reconnection part, the sharing will
        # already reduce the number of connections.
        if _client is None:
            self._client = client._SmartClient(self._medium)
        else:
            self._client = _client

    def _build_medium(self, from_transport=None):
        """Create the medium if from_transport does not provide one.

        The medium is analogous to the connection for ConnectedTransport: it
        allows connection sharing.
        
        :param from_transport: provide the medium to reuse if not None
        """
        if from_transport is not None:
            _medium = from_transport._medium
        else:
            import pdb; pdb.set_trace()
            _medium = None
            # No credentials
            self._set_connection(_medium, None)
        return _medium

    def is_readonly(self):
        """Smart server transport can do read/write file operations."""
        resp = self._call2('Transport.is_readonly')
        if resp == ('yes', ):
            return True
        elif resp == ('no', ):
            return False
        elif (resp == ('error', "Generic bzr smart protocol error: "
                                "bad request 'Transport.is_readonly'") or
              resp == ('error', "Generic bzr smart protocol error: "
                                "bad request u'Transport.is_readonly'")):
            # XXX: nasty hack: servers before 0.16 don't have a
            # 'Transport.is_readonly' verb, so we do what clients before 0.16
            # did: assume False.
            return False
        else:
            self._translate_error(resp)
        assert False, 'weird response %r' % (resp,)

    def get_smart_client(self):
        return self._get_connection()

    def get_smart_medium(self):
        return self._get_connection()

    def _remote_path(self, relpath):
        """Returns the Unicode version of the absolute path for relpath."""
        return self._combine_paths(self._path, relpath)

    def _call(self, method, *args):
        resp = self._call2(method, *args)
        self._translate_error(resp)

    def _call2(self, method, *args):
        """Call a method on the remote server."""
        return self._client.call(method, *args)

    def _call_with_body_bytes(self, method, args, body):
        """Call a method on the remote server with body bytes."""
        return self._client.call_with_body_bytes(method, args, body)

    def has(self, relpath):
        """Indicate whether a remote file of the given name exists or not.

        :see: Transport.has()
        """
        resp = self._call2('has', self._remote_path(relpath))
        if resp == ('yes', ):
            return True
        elif resp == ('no', ):
            return False
        else:
            self._translate_error(resp)

    def get(self, relpath):
        """Return file-like object reading the contents of a remote file.
        
        :see: Transport.get_bytes()/get_file()
        """
        return StringIO(self.get_bytes(relpath))

    def get_bytes(self, relpath):
        remote = self._remote_path(relpath)
        request = self._medium.get_request()
        smart_protocol = protocol.SmartClientRequestProtocolOne(request)
        smart_protocol.call('get', remote)
        resp = smart_protocol.read_response_tuple(True)
        if resp != ('ok', ):
            smart_protocol.cancel_read_body()
            self._translate_error(resp, relpath)
        return smart_protocol.read_body_bytes()

    def _serialise_optional_mode(self, mode):
        if mode is None:
            return ''
        else:
            return '%d' % mode

    def mkdir(self, relpath, mode=None):
        resp = self._call2('mkdir', self._remote_path(relpath),
            self._serialise_optional_mode(mode))
        self._translate_error(resp)

    def put_bytes(self, relpath, upload_contents, mode=None):
        # FIXME: upload_file is probably not safe for non-ascii characters -
        # should probably just pass all parameters as length-delimited
        # strings?
        if type(upload_contents) is unicode:
            # Although not strictly correct, we raise UnicodeEncodeError to be
            # compatible with other transports.
            raise UnicodeEncodeError(
                'undefined', upload_contents, 0, 1,
                'put_bytes must be given bytes, not unicode.')
        resp = self._call_with_body_bytes('put',
            (self._remote_path(relpath), self._serialise_optional_mode(mode)),
            upload_contents)
        self._translate_error(resp)

    def put_bytes_non_atomic(self, relpath, bytes, mode=None,
                             create_parent_dir=False,
                             dir_mode=None):
        """See Transport.put_bytes_non_atomic."""
        # FIXME: no encoding in the transport!
        create_parent_str = 'F'
        if create_parent_dir:
            create_parent_str = 'T'

        resp = self._call_with_body_bytes(
            'put_non_atomic',
            (self._remote_path(relpath), self._serialise_optional_mode(mode),
             create_parent_str, self._serialise_optional_mode(dir_mode)),
            bytes)
        self._translate_error(resp)

    def put_file(self, relpath, upload_file, mode=None):
        # its not ideal to seek back, but currently put_non_atomic_file depends
        # on transports not reading before failing - which is a faulty
        # assumption I think - RBC 20060915
        pos = upload_file.tell()
        try:
            return self.put_bytes(relpath, upload_file.read(), mode)
        except:
            upload_file.seek(pos)
            raise

    def put_file_non_atomic(self, relpath, f, mode=None,
                            create_parent_dir=False,
                            dir_mode=None):
        return self.put_bytes_non_atomic(relpath, f.read(), mode=mode,
                                         create_parent_dir=create_parent_dir,
                                         dir_mode=dir_mode)

    def append_file(self, relpath, from_file, mode=None):
        return self.append_bytes(relpath, from_file.read(), mode)
        
    def append_bytes(self, relpath, bytes, mode=None):
        resp = self._call_with_body_bytes(
            'append',
            (self._remote_path(relpath), self._serialise_optional_mode(mode)),
            bytes)
        if resp[0] == 'appended':
            return int(resp[1])
        self._translate_error(resp)

    def delete(self, relpath):
        resp = self._call2('delete', self._remote_path(relpath))
        self._translate_error(resp)

    def readv(self, relpath, offsets):
        if not offsets:
            return

        offsets = list(offsets)

        sorted_offsets = sorted(offsets)
        # turn the list of offsets into a stack
        offset_stack = iter(offsets)
        cur_offset_and_size = offset_stack.next()
        coalesced = list(self._coalesce_offsets(sorted_offsets,
                               limit=self._max_readv_combine,
                               fudge_factor=self._bytes_to_read_before_seek))

        request = self._medium.get_request()
        smart_protocol = protocol.SmartClientRequestProtocolOne(request)
        smart_protocol.call_with_body_readv_array(
            ('readv', self._remote_path(relpath)),
            [(c.start, c.length) for c in coalesced])
        resp = smart_protocol.read_response_tuple(True)

        if resp[0] != 'readv':
            # This should raise an exception
            smart_protocol.cancel_read_body()
            self._translate_error(resp)
            return

        # FIXME: this should know how many bytes are needed, for clarity.
        data = smart_protocol.read_body_bytes()
        # Cache the results, but only until they have been fulfilled
        data_map = {}
        for c_offset in coalesced:
            if len(data) < c_offset.length:
                raise errors.ShortReadvError(relpath, c_offset.start,
                            c_offset.length, actual=len(data))
            for suboffset, subsize in c_offset.ranges:
                key = (c_offset.start+suboffset, subsize)
                data_map[key] = data[suboffset:suboffset+subsize]
            data = data[c_offset.length:]

            # Now that we've read some data, see if we can yield anything back
            while cur_offset_and_size in data_map:
                this_data = data_map.pop(cur_offset_and_size)
                yield cur_offset_and_size[0], this_data
                cur_offset_and_size = offset_stack.next()

    def rename(self, rel_from, rel_to):
        self._call('rename',
                   self._remote_path(rel_from),
                   self._remote_path(rel_to))

    def move(self, rel_from, rel_to):
        self._call('move',
                   self._remote_path(rel_from),
                   self._remote_path(rel_to))

    def rmdir(self, relpath):
        resp = self._call('rmdir', self._remote_path(relpath))

    def _translate_error(self, resp, orig_path=None):
        """Raise an exception from a response"""
        if resp is None:
            what = None
        else:
            what = resp[0]
        if what == 'ok':
            return
        elif what == 'NoSuchFile':
            if orig_path is not None:
                error_path = orig_path
            else:
                error_path = resp[1]
            raise errors.NoSuchFile(error_path)
        elif what == 'error':
            raise errors.SmartProtocolError(unicode(resp[1]))
        elif what == 'FileExists':
            raise errors.FileExists(resp[1])
        elif what == 'DirectoryNotEmpty':
            raise errors.DirectoryNotEmpty(resp[1])
        elif what == 'ShortReadvError':
            raise errors.ShortReadvError(resp[1], int(resp[2]),
                                         int(resp[3]), int(resp[4]))
        elif what in ('UnicodeEncodeError', 'UnicodeDecodeError'):
            encoding = str(resp[1]) # encoding must always be a string
            val = resp[2]
            start = int(resp[3])
            end = int(resp[4])
            reason = str(resp[5]) # reason must always be a string
            if val.startswith('u:'):
                val = val[2:].decode('utf-8')
            elif val.startswith('s:'):
                val = val[2:].decode('base64')
            if what == 'UnicodeDecodeError':
                raise UnicodeDecodeError(encoding, val, start, end, reason)
            elif what == 'UnicodeEncodeError':
                raise UnicodeEncodeError(encoding, val, start, end, reason)
        elif what == "ReadOnlyError":
            raise errors.TransportNotPossible('readonly transport')
        else:
            raise errors.SmartProtocolError('unexpected smart server error: %r' % (resp,))

    def disconnect(self):
        self._medium.disconnect()

    def delete_tree(self, relpath):
        raise errors.TransportNotPossible('readonly transport')

    def stat(self, relpath):
        resp = self._call2('stat', self._remote_path(relpath))
        if resp[0] == 'stat':
            return _SmartStat(int(resp[1]), int(resp[2], 8))
        else:
            self._translate_error(resp)

    ## def lock_read(self, relpath):
    ##     """Lock the given file for shared (read) access.
    ##     :return: A lock object, which should be passed to Transport.unlock()
    ##     """
    ##     # The old RemoteBranch ignore lock for reading, so we will
    ##     # continue that tradition and return a bogus lock object.
    ##     class BogusLock(object):
    ##         def __init__(self, path):
    ##             self.path = path
    ##         def unlock(self):
    ##             pass
    ##     return BogusLock(relpath)

    def listable(self):
        return True

    def list_dir(self, relpath):
        resp = self._call2('list_dir', self._remote_path(relpath))
        if resp[0] == 'names':
            return [name.encode('ascii') for name in resp[1:]]
        else:
            self._translate_error(resp)

    def iter_files_recursive(self):
        resp = self._call2('iter_files_recursive', self._remote_path(''))
        if resp[0] == 'names':
            return resp[1:]
        else:
            self._translate_error(resp)


class RemoteTCPTransport(RemoteTransport):
    """Connection to smart server over plain tcp.
    
    This is essentially just a factory to get 'RemoteTransport(url,
        SmartTCPClientMedium).
    """

    def _initial_split_url(self, base):
        scheme, user, password, host, port, path = self._split_url(base)
        if port is None:
            port = BZR_DEFAULT_PORT
        return (scheme, user, password, host, port, path)

    def _build_medium(self, from_transport=None):
        assert self.base.startswith('bzr://')
        if from_transport is not None:
            _medium = from_transport._medium
        else:
            _medium = medium.SmartTCPClientMedium(self._host, self._port)
            self._set_connection(_medium, None)
        return _medium


class RemoteSSHTransport(RemoteTransport):
    """Connection to smart server over SSH.

    This is essentially just a factory to get 'RemoteTransport(url,
        SmartSSHClientMedium).
    """

    def _build_medium(self, from_transport=None):
        assert self.base.startswith('bzr+ssh://')
        if from_transport is not None:
            _medium = from_transport._medium
        else:
            _medium = medium.SmartSSHClientMedium(self._host, self._port,
                                                  self._user, self._password)
            # ssh will prompt the user for a password if needed and if none is
            # provided but it will not give it back, so no credentials can be
            # stored.
            self._set_connection(_medium, None)
        return _medium


class RemoteHTTPTransport(RemoteTransport):
    """Just a way to connect between a bzr+http:// url and http://.
    
    This connection operates slightly differently than the RemoteSSHTransport.
    It uses a plain http:// transport underneath, which defines what remote
    .bzr/smart URL we are connected to. From there, all paths that are sent are
    sent as relative paths, this way, the remote side can properly
    de-reference them, since it is likely doing rewrite rules to translate an
    HTTP path into a local path.
    """

    def __init__(self, base, from_transport=None, http_transport=None):
        assert base.startswith('bzr+http://')

        if http_transport is None:
            http_url = base[len('bzr+'):]
            self._http_transport = transport.get_transport(http_url)
        else:
            self._http_transport = http_transport
        super(RemoteHTTPTransport, self).__init__(base, from_transport)

    def _build_medium(self, from_transport=None):
        if from_transport is not None:
            _medium = from_transport._medium
        else:
            _medium = self._http_transport.get_smart_medium()
            # We let http_transport take care of the credentials
            self._set_connection(_medium, None)
        return _medium

    def _remote_path(self, relpath):
        """After connecting, HTTP Transport only deals in relative URLs."""
        # Adjust the relpath based on which URL this smart transport is
        # connected to.
        http_base = urlutils.normalize_url(self._http_transport.base)
        url = urlutils.join(self.base[len('bzr+'):], relpath)
        url = urlutils.normalize_url(url)
        return urlutils.relative_url(http_base, url)

    def clone(self, relative_url):
        """Make a new RemoteHTTPTransport related to me.

        This is re-implemented rather than using the default
        RemoteTransport.clone() because we must be careful about the underlying
        http transport.

        Also, the cloned smart transport will POST to the same .bzr/smart
        location as this transport (although obviously the relative paths in the
        smart requests may be different).  This is so that the server doesn't
        have to handle .bzr/smart requests at arbitrary places inside .bzr
        directories, just at the initial URL the user uses.

        The exception is parent paths (i.e. relative_url of "..").
        """
        if relative_url:
            abs_url = self.abspath(relative_url)
        else:
            abs_url = self.base
        # We either use the exact same http_transport (for child locations), or
        # a clone of the underlying http_transport (for parent locations).  This
        # means we share the connection.
        norm_base = urlutils.normalize_url(self.base)
        norm_abs_url = urlutils.normalize_url(abs_url)
        normalized_rel_url = urlutils.relative_url(norm_base, norm_abs_url)
        if normalized_rel_url == ".." or normalized_rel_url.startswith("../"):
            http_transport = self._http_transport.clone(normalized_rel_url)
        else:
            http_transport = self._http_transport
        return RemoteHTTPTransport(abs_url, self, http_transport=http_transport)


def get_test_permutations():
    """Return (transport, server) permutations for testing."""
    ### We may need a little more test framework support to construct an
    ### appropriate RemoteTransport in the future.
    from bzrlib.smart import server
    return [(RemoteTCPTransport, server.SmartTCPServer_for_testing)]
