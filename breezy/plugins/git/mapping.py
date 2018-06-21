# Copyright (C) 2008-2018 Jelmer Vernooij <jelmer@jelmer.uk>
# Copyright (C) 2007 Canonical Ltd
# Copyright (C) 2008 John Carr
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

"""Converters, etc for going between Bazaar and Git ids."""

from __future__ import absolute_import

import base64
import stat

from ... import (
    bencode,
    errors,
    foreign,
    trace,
    )
from ...bzr.inventory import (
    ROOT_ID,
    )
from ...foreign import (
    ForeignVcs,
    VcsMappingRegistry,
    ForeignRevision,
    )
from ...revision import (
    NULL_REVISION,
    )
from ...sixish import text_type
from .errors import (
    NoPushSupport,
    UnknownCommitExtra,
    UnknownMercurialCommitExtra,
    )
from .hg import (
    format_hg_metadata,
    extract_hg_metadata,
    )
from .roundtrip import (
    extract_bzr_metadata,
    inject_bzr_metadata,
    CommitSupplement,
    deserialize_fileid_map,
    serialize_fileid_map,
    )

try:
    from urllib.parse import quote
except ImportError:
    from urllib import quote

DEFAULT_FILE_MODE = stat.S_IFREG | 0o644
HG_RENAME_SOURCE = "HG:rename-source"
HG_EXTRA = "HG:extra"

# This HG extra is used to indicate the commit that this commit was based on.
HG_EXTRA_AMEND_SOURCE = "amend_source"

FILE_ID_PREFIX = b'git:'


def escape_file_id(file_id):
    return file_id.replace('_', '__').replace(' ', '_s').replace('\x0c', '_c')


def unescape_file_id(file_id):
    ret = []
    i = 0
    while i < len(file_id):
        if file_id[i] != '_':
            ret.append(file_id[i])
        else:
            if file_id[i+1] == '_':
                ret.append("_")
            elif file_id[i+1] == 's':
                ret.append(" ")
            elif file_id[i+1] == 'c':
                ret.append("\x0c")
            else:
                raise ValueError("unknown escape character %s" %
                    file_id[i+1])
            i += 1
        i += 1
    return "".join(ret)


def fix_person_identifier(text):
    if not "<" in text and not ">" in text:
        username = text
        email = text
    else:
        if text.rindex(">") < text.rindex("<"):
            raise ValueError(text)
        username, email = text.split("<", 2)[-2:]
        email = email.split(">", 1)[0]
        if username.endswith(" "):
            username = username[:-1]
    return "%s <%s>" % (username, email)


def warn_escaped(commit, num_escaped):
    trace.warning("Escaped %d XML-invalid characters in %s. Will be unable "
                  "to regenerate the SHA map.", num_escaped, commit)


def warn_unusual_mode(commit, path, mode):
    trace.mutter("Unusual file mode %o for %s in %s. Storing as revision "
                 "property. ", mode, path, commit)


class BzrGitMapping(foreign.VcsMapping):
    """Class that maps between Git and Bazaar semantics."""
    experimental = False

    BZR_FILE_IDS_FILE = None

    BZR_DUMMY_FILE = None

    def is_special_file(self, filename):
        return (filename in (self.BZR_FILE_IDS_FILE, self.BZR_DUMMY_FILE))

    def __init__(self):
        super(BzrGitMapping, self).__init__(foreign_vcs_git)

    def __eq__(self, other):
        return (type(self) == type(other) and
                self.revid_prefix == other.revid_prefix)

    @classmethod
    def revision_id_foreign_to_bzr(cls, git_rev_id):
        """Convert a git revision id handle to a Bazaar revision id."""
        from dulwich.protocol import ZERO_SHA
        if git_rev_id == ZERO_SHA:
            return NULL_REVISION
        return "%s:%s" % (cls.revid_prefix, git_rev_id)

    @classmethod
    def revision_id_bzr_to_foreign(cls, bzr_rev_id):
        """Convert a Bazaar revision id to a git revision id handle."""
        if not bzr_rev_id.startswith("%s:" % cls.revid_prefix):
            raise errors.InvalidRevisionId(bzr_rev_id, cls)
        return bzr_rev_id[len(cls.revid_prefix)+1:], cls()

    def generate_file_id(self, path):
        # Git paths are just bytestrings
        # We must just hope they are valid UTF-8..
        if path == "":
            return ROOT_ID
        if isinstance(path, text_type):
            path = path.encode("utf-8")
        return FILE_ID_PREFIX + escape_file_id(path)

    def parse_file_id(self, file_id):
        if file_id == ROOT_ID:
            return ""
        if not file_id.startswith(FILE_ID_PREFIX):
            raise ValueError
        return unescape_file_id(file_id[len(FILE_ID_PREFIX):])

    def revid_as_refname(self, revid):
        return "refs/bzr/%s" % quote(revid)

    def import_unusual_file_modes(self, rev, unusual_file_modes):
        if unusual_file_modes:
            ret = [(path, unusual_file_modes[path])
                   for path in sorted(unusual_file_modes.keys())]
            rev.properties['file-modes'] = bencode.bencode(ret)

    def export_unusual_file_modes(self, rev):
        try:
            file_modes = rev.properties['file-modes']
        except KeyError:
            return {}
        else:
            return dict(bencode.bdecode(file_modes.encode("utf-8")))

    def _generate_git_svn_metadata(self, rev, encoding):
        try:
            git_svn_id = rev.properties["git-svn-id"]
        except KeyError:
            return ""
        else:
            return "\ngit-svn-id: %s\n" % git_svn_id.encode(encoding)

    def _generate_hg_message_tail(self, rev):
        extra = {}
        renames = []
        branch = 'default'
        for name in rev.properties:
            if name == 'hg:extra:branch':
                branch = rev.properties['hg:extra:branch']
            elif name.startswith('hg:extra'):
                extra[name[len('hg:extra:'):]] = base64.b64decode(
                    rev.properties[name])
            elif name == 'hg:renames':
                renames = bencode.bdecode(base64.b64decode(
                    rev.properties['hg:renames']))
            # TODO: Export other properties as 'bzr:' extras?
        ret = format_hg_metadata(renames, branch, extra)
        if type(ret) is not str:
            raise TypeError(ret)
        return ret

    def _extract_git_svn_metadata(self, rev, message):
        lines = message.split("\n")
        if not (lines[-1] == "" and len(lines) >= 2 and lines[-2].startswith("git-svn-id:")):
            return message
        git_svn_id = lines[-2].split(": ", 1)[1]
        rev.properties['git-svn-id'] = git_svn_id
        (url, rev, uuid) = parse_git_svn_id(git_svn_id)
        # FIXME: Convert this to converted-from property somehow..
        return "\n".join(lines[:-2])

    def _extract_hg_metadata(self, rev, message):
        (message, renames, branch, extra) = extract_hg_metadata(message)
        if branch is not None:
            rev.properties['hg:extra:branch'] = branch
        for name, value in extra.iteritems():
            rev.properties['hg:extra:' + name] = base64.b64encode(value)
        if renames:
            rev.properties['hg:renames'] = base64.b64encode(bencode.bencode(
                [(new, old) for (old, new) in renames.iteritems()]))
        return message

    def _extract_bzr_metadata(self, rev, message):
        (message, metadata) = extract_bzr_metadata(message)
        return message, metadata

    def _decode_commit_message(self, rev, message, encoding):
        return message.decode(encoding), CommitSupplement()

    def _encode_commit_message(self, rev, message, encoding):
        return message.encode(encoding)

    def export_fileid_map(self, fileid_map):
        """Export a file id map to a fileid map.

        :param fileid_map: File id map, mapping paths to file ids
        :return: A Git blob object (or None if there are no entries)
        """
        from dulwich.objects import Blob
        b = Blob()
        b.set_raw_chunks(serialize_fileid_map(fileid_map))
        return b

    def export_commit(self, rev, tree_sha, parent_lookup, lossy,
                      verifiers):
        """Turn a Bazaar revision in to a Git commit

        :param tree_sha: Tree sha for the commit
        :param parent_lookup: Function for looking up the GIT sha equiv of a
            bzr revision
        :param lossy: Whether to store roundtripping information.
        :param verifiers: Verifiers info
        :return dulwich.objects.Commit represent the revision:
        """
        from dulwich.objects import Commit, Tag
        commit = Commit()
        commit.tree = tree_sha
        if not lossy:
            metadata = CommitSupplement()
            metadata.verifiers = verifiers
        else:
            metadata = None
        parents = []
        for p in rev.parent_ids:
            try:
                git_p = parent_lookup(p)
            except KeyError:
                git_p = None
                if metadata is not None:
                    metadata.explicit_parent_ids = rev.parent_ids
            if git_p is not None:
                if len(git_p) != 40:
                    raise AssertionError("unexpected length for %r" % git_p)
                parents.append(git_p)
        commit.parents = parents
        try:
            encoding = rev.properties['git-explicit-encoding']
        except KeyError:
            encoding = rev.properties.get('git-implicit-encoding', 'utf-8')
        try:
            commit.encoding = rev.properties['git-explicit-encoding'].encode('ascii')
        except KeyError:
            pass
        commit.committer = fix_person_identifier(rev.committer.encode(
            encoding))
        commit.author = fix_person_identifier(
            rev.get_apparent_authors()[0].encode(encoding))
        commit.commit_time = long(rev.timestamp)
        if 'author-timestamp' in rev.properties:
            commit.author_time = long(rev.properties['author-timestamp'])
        else:
            commit.author_time = commit.commit_time
        commit._commit_timezone_neg_utc = "commit-timezone-neg-utc" in rev.properties
        commit.commit_timezone = rev.timezone
        commit._author_timezone_neg_utc = "author-timezone-neg-utc" in rev.properties
        if 'author-timezone' in rev.properties:
            commit.author_timezone = int(rev.properties['author-timezone'])
        else:
            commit.author_timezone = commit.commit_timezone
        if 'git-gpg-signature' in rev.properties:
            commit.gpgsig = rev.properties['git-gpg-signature'].encode('ascii')
        commit.message = self._encode_commit_message(rev, rev.message,
            encoding)
        if type(commit.message) is not str:
            raise TypeError(commit.message)
        if metadata is not None:
            try:
                mapping_registry.parse_revision_id(rev.revision_id)
            except errors.InvalidRevisionId:
                metadata.revision_id = rev.revision_id
            mapping_properties = set(
                ['author', 'author-timezone', 'author-timezone-neg-utc',
                 'commit-timezone-neg-utc', 'git-implicit-encoding',
                 'git-gpg-signature', 'git-explicit-encoding',
                 'author-timestamp', 'file-modes'])
            for k, v in rev.properties.iteritems():
                if not k in mapping_properties:
                    metadata.properties[k] = v
        if not lossy and metadata:
            if self.roundtripping:
                commit.message = inject_bzr_metadata(commit.message, metadata,
                                                     encoding)
            else:
                raise NoPushSupport()
        if type(commit.message) is not str:
            raise TypeError(commit.message)
        i = 0
        propname = 'git-mergetag-0'
        while propname in rev.properties:
            commit.mergetag.append(Tag.from_string(rev.properties[propname].encode(encoding)))
            i += 1
            propname = 'git-mergetag-%d' % i
        if 'git-extra' in rev.properties:
            commit.extra.extend([l.split(' ', 1) for l in rev.properties['git-extra'].splitlines()])
        return commit

    def import_fileid_map(self, blob):
        """Convert a git file id map blob.

        :param blob: Git blob object with fileid map
        :return: Dictionary mapping paths to file ids
        """
        return deserialize_fileid_map(blob.data)

    def import_commit(self, commit, lookup_parent_revid):
        """Convert a git commit to a bzr revision.

        :return: a `breezy.revision.Revision` object, foreign revid and a
            testament sha1
        """
        if commit is None:
            raise AssertionError("Commit object can't be None")
        rev = ForeignRevision(commit.id, self,
                self.revision_id_foreign_to_bzr(commit.id))
        rev.git_metadata = None
        def decode_using_encoding(rev, commit, encoding):
            rev.committer = str(commit.committer).decode(encoding)
            if commit.committer != commit.author:
                rev.properties['author'] = str(commit.author).decode(encoding)
            rev.message, rev.git_metadata = self._decode_commit_message(
                rev, commit.message, encoding)
        if commit.encoding is not None:
            rev.properties['git-explicit-encoding'] = commit.encoding
            decode_using_encoding(rev, commit, commit.encoding)
        else:
            for encoding in ('utf-8', 'latin1'):
                try:
                    decode_using_encoding(rev, commit, encoding)
                except UnicodeDecodeError:
                    pass
                else:
                    if encoding != 'utf-8':
                        rev.properties['git-implicit-encoding'] = encoding
                    break
        if commit.commit_time != commit.author_time:
            rev.properties['author-timestamp'] = str(commit.author_time)
        if commit.commit_timezone != commit.author_timezone:
            rev.properties['author-timezone'] = "%d" % commit.author_timezone
        if commit._author_timezone_neg_utc:
            rev.properties['author-timezone-neg-utc'] = ""
        if commit._commit_timezone_neg_utc:
            rev.properties['commit-timezone-neg-utc'] = ""
        if commit.gpgsig:
            rev.properties['git-gpg-signature'] = commit.gpgsig.decode('ascii')
        if commit.mergetag:
            for i, tag in enumerate(commit.mergetag):
                rev.properties['git-mergetag-%d' % i] = tag.as_raw_string()
        rev.timestamp = commit.commit_time
        rev.timezone = commit.commit_timezone
        rev.parent_ids = None
        if rev.git_metadata is not None:
            md = rev.git_metadata
            roundtrip_revid = md.revision_id
            if md.explicit_parent_ids:
                rev.parent_ids = md.explicit_parent_ids
            rev.properties.update(md.properties)
            verifiers = md.verifiers
        else:
            roundtrip_revid = None
            verifiers = {}
        if rev.parent_ids is None:
            parents = []
            for p in commit.parents:
                try:
                    parents.append(lookup_parent_revid(p))
                except KeyError:
                    parents.append(self.revision_id_foreign_to_bzr(p))
            rev.parent_ids = tuple(parents)
        unknown_extra_fields = []
        extra_lines = []
        for k, v in commit.extra:
            if k == HG_RENAME_SOURCE:
                extra_lines.append(k + ' ' + v + '\n')
            elif k == HG_EXTRA:
                hgk, hgv = v.split(':', 1)
                if hgk not in (HG_EXTRA_AMEND_SOURCE, ):
                    raise UnknownMercurialCommitExtra(commit, hgk)
                extra_lines.append(k + ' ' + v + '\n')
            else:
                unknown_extra_fields.append(k)
        if unknown_extra_fields:
            raise UnknownCommitExtra(commit, unknown_extra_fields)
        if extra_lines:
            rev.properties['git-extra'] = ''.join(extra_lines)
        return rev, roundtrip_revid, verifiers

    def get_fileid_map(self, lookup_object, tree_sha):
        """Obtain a fileid map for a particular tree.

        :param lookup_object: Function for looking up an object
        :param tree_sha: SHA of the root tree
        :return: GitFileIdMap instance
        """
        try:
            file_id_map_sha = lookup_object(tree_sha)[self.BZR_FILE_IDS_FILE][1]
        except KeyError:
            file_ids = {}
        else:
            file_ids = self.import_fileid_map(lookup_object(file_id_map_sha))
        return GitFileIdMap(file_ids, self)


class BzrGitMappingv1(BzrGitMapping):
    revid_prefix = 'git-v1'
    experimental = False

    def __str__(self):
        return self.revid_prefix


class BzrGitMappingExperimental(BzrGitMappingv1):
    revid_prefix = 'git-experimental'
    experimental = True
    roundtripping = True

    BZR_FILE_IDS_FILE = '.bzrfileids'

    BZR_DUMMY_FILE = '.bzrdummy'

    def _decode_commit_message(self, rev, message, encoding):
        message = self._extract_hg_metadata(rev, message)
        message = self._extract_git_svn_metadata(rev, message)
        message, metadata = self._extract_bzr_metadata(rev, message)
        return message.decode(encoding), metadata

    def _encode_commit_message(self, rev, message, encoding):
        ret = message.encode(encoding)
        ret += self._generate_hg_message_tail(rev)
        ret += self._generate_git_svn_metadata(rev, encoding)
        return ret

    def import_commit(self, commit, lookup_parent_revid):
        rev, roundtrip_revid, verifiers = super(BzrGitMappingExperimental, self).import_commit(commit, lookup_parent_revid)
        rev.properties['converted_revision'] = "git %s\n" % commit.id
        return rev, roundtrip_revid, verifiers


class GitMappingRegistry(VcsMappingRegistry):
    """Registry with available git mappings."""

    def revision_id_bzr_to_foreign(self, bzr_revid):
        if bzr_revid == NULL_REVISION:
            from dulwich.protocol import ZERO_SHA
            return ZERO_SHA, None
        if not bzr_revid.startswith("git-"):
            raise errors.InvalidRevisionId(bzr_revid, None)
        (mapping_version, git_sha) = bzr_revid.split(":", 1)
        mapping = self.get(mapping_version)
        return mapping.revision_id_bzr_to_foreign(bzr_revid)

    parse_revision_id = revision_id_bzr_to_foreign


mapping_registry = GitMappingRegistry()
mapping_registry.register_lazy('git-v1', "breezy.plugins.git.mapping",
    "BzrGitMappingv1")
mapping_registry.register_lazy('git-experimental',
    "breezy.plugins.git.mapping", "BzrGitMappingExperimental")
# Uncomment the next line to enable the experimental bzr-git mappings.
# This will make sure all bzr metadata is pushed into git, allowing for
# full roundtripping later.
# NOTE: THIS IS EXPERIMENTAL. IT MAY EAT YOUR DATA OR CORRUPT
# YOUR BZR OR GIT REPOSITORIES. USE WITH CARE.
#mapping_registry.set_default('git-experimental')
mapping_registry.set_default('git-v1')


class ForeignGit(ForeignVcs):
    """The Git Stupid Content Tracker"""

    @property
    def branch_format(self):
        from .branch import LocalGitBranchFormat
        return LocalGitBranchFormat()

    @property
    def repository_format(self):
        from .repository import GitRepositoryFormat
        return GitRepositoryFormat()

    def __init__(self):
        super(ForeignGit, self).__init__(mapping_registry)
        self.abbreviation = "git"

    @classmethod
    def serialize_foreign_revid(self, foreign_revid):
        return foreign_revid

    @classmethod
    def show_foreign_revid(cls, foreign_revid):
        return { "git commit": foreign_revid }


foreign_vcs_git = ForeignGit()
default_mapping = mapping_registry.get_default()()


def symlink_to_blob(symlink_target):
    from dulwich.objects import Blob
    blob = Blob()
    if isinstance(symlink_target, text_type):
        symlink_target = symlink_target.encode('utf-8')
    blob.data = symlink_target
    return blob


def mode_is_executable(mode):
    """Check if mode should be considered executable."""
    return bool(mode & 0o111)


def mode_kind(mode):
    """Determine the Bazaar inventory kind based on Unix file mode."""
    if mode is None:
        return None
    entry_kind = (mode & 0o700000) / 0o100000
    if entry_kind == 0:
        return 'directory'
    elif entry_kind == 1:
        file_kind = (mode & 0o70000) / 0o10000
        if file_kind == 0:
            return 'file'
        elif file_kind == 2:
            return 'symlink'
        elif file_kind == 6:
            return 'tree-reference'
        else:
            raise AssertionError(
                "Unknown file kind %d, perms=%o." % (file_kind, mode,))
    else:
        raise AssertionError(
            "Unknown kind, perms=%r." % (mode,))


def object_mode(kind, executable):
    if kind == 'directory':
        return stat.S_IFDIR
    elif kind == 'symlink':
        mode = stat.S_IFLNK
        if executable:
            mode |= 0o111
        return mode
    elif kind == 'file':
        mode = stat.S_IFREG | 0o644
        if executable:
            mode |= 0o111
        return mode
    elif kind == 'tree-reference':
        from dulwich.objects import S_IFGITLINK
        return S_IFGITLINK
    else:
        raise AssertionError


def entry_mode(entry):
    """Determine the git file mode for an inventory entry."""
    return object_mode(entry.kind, getattr(entry, 'executable', False))


def extract_unusual_modes(rev):
    try:
        foreign_revid, mapping = mapping_registry.parse_revision_id(
            rev.revision_id)
    except errors.InvalidRevisionId:
        return {}
    else:
        return mapping.export_unusual_file_modes(rev)


def parse_git_svn_id(text):
    (head, uuid) = text.rsplit(" ", 1)
    (full_url, rev) = head.rsplit("@", 1)
    return (full_url, int(rev), uuid)


class GitFileIdMap(object):

    def __init__(self, file_ids, mapping):
        self.file_ids = file_ids
        self.paths = None
        self.mapping = mapping

    def all_file_ids(self):
        return self.file_ids.values()

    def set_file_id(self, path, file_id):
        if type(path) is not str:
            raise TypeError(path)
        if type(file_id) is not str:
            raise TypeError(file_id)
        self.file_ids[path] = file_id

    def lookup_file_id(self, path):
        if type(path) is not str:
            raise TypeError(path)
        try:
            file_id = self.file_ids[path]
        except KeyError:
            file_id = self.mapping.generate_file_id(path)
        if type(file_id) is not str:
            raise TypeError(file_id)
        return file_id

    def lookup_path(self, file_id):
        if self.paths is None:
            self.paths = {}
            for k, v in self.file_ids.iteritems():
                self.paths[v] = k
        try:
            path = self.paths[file_id]
        except KeyError:
            return self.mapping.parse_file_id(file_id)
        else:
            if type(path) is not str:
                raise TypeError(path)
            return path

    def copy(self):
        return self.__class__(dict(self.file_ids), self.mapping)


def needs_roundtripping(repo, revid):
    try:
        mapping_registry.parse_revision_id(revid)
    except errors.InvalidRevisionId:
        return True
    else:
        return False
