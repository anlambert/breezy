# Copyright (C) 2005-2011 Canonical Ltd
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

"""Tree classes, representing directory at point in time.
"""

from __future__ import absolute_import

import os
import re

from . import (
    controldir,
    errors,
    inventory as _mod_inventory,
    lazy_import,
    mutabletree,
    osutils,
    trace,
    )
lazy_import.lazy_import(globals(), """
from breezy import (
    add,
    revisiontree,
    transport as _mod_transport,
    )
""")
from .decorators import needs_read_lock
from .sixish import (
    viewvalues,
    )
from .tree import Tree


class InventoryTree(Tree):
    """A tree that relies on an inventory for its metadata.

    Trees contain an `Inventory` object, and also know how to retrieve
    file texts mentioned in the inventory, either from a working
    directory or from a store.

    It is possible for trees to contain files that are not described
    in their inventory or vice versa; for this use `filenames()`.

    Subclasses should set the _inventory attribute, which is considered
    private to external API users.
    """

    def get_canonical_inventory_paths(self, paths):
        """Like get_canonical_inventory_path() but works on multiple items.

        :param paths: A sequence of paths relative to the root of the tree.
        :return: A list of paths, with each item the corresponding input path
        adjusted to account for existing elements that match case
        insensitively.
        """
        return list(self._yield_canonical_inventory_paths(paths))

    def get_canonical_inventory_path(self, path):
        """Returns the first inventory item that case-insensitively matches path.

        If a path matches exactly, it is returned. If no path matches exactly
        but more than one path matches case-insensitively, it is implementation
        defined which is returned.

        If no path matches case-insensitively, the input path is returned, but
        with as many path entries that do exist changed to their canonical
        form.

        If you need to resolve many names from the same tree, you should
        use get_canonical_inventory_paths() to avoid O(N) behaviour.

        :param path: A paths relative to the root of the tree.
        :return: The input path adjusted to account for existing elements
        that match case insensitively.
        """
        return next(self._yield_canonical_inventory_paths([path]))

    def _yield_canonical_inventory_paths(self, paths):
        for path in paths:
            # First, if the path as specified exists exactly, just use it.
            if self.path2id(path) is not None:
                yield path
                continue
            # go walkin...
            cur_id = self.get_root_id()
            cur_path = ''
            bit_iter = iter(path.split("/"))
            for elt in bit_iter:
                lelt = elt.lower()
                new_path = None
                for child in self.iter_children(cur_id):
                    try:
                        # XXX: it seem like if the child is known to be in the
                        # tree, we shouldn't need to go from its id back to
                        # its path -- mbp 2010-02-11
                        #
                        # XXX: it seems like we could be more efficient
                        # by just directly looking up the original name and
                        # only then searching all children; also by not
                        # chopping paths so much. -- mbp 2010-02-11
                        child_base = os.path.basename(self.id2path(child))
                        if (child_base == elt):
                            # if we found an exact match, we can stop now; if
                            # we found an approximate match we need to keep
                            # searching because there might be an exact match
                            # later.  
                            cur_id = child
                            new_path = osutils.pathjoin(cur_path, child_base)
                            break
                        elif child_base.lower() == lelt:
                            cur_id = child
                            new_path = osutils.pathjoin(cur_path, child_base)
                    except errors.NoSuchId:
                        # before a change is committed we can see this error...
                        continue
                if new_path:
                    cur_path = new_path
                else:
                    # got to the end of this directory and no entries matched.
                    # Return what matched so far, plus the rest as specified.
                    cur_path = osutils.pathjoin(cur_path, elt, *list(bit_iter))
                    break
            yield cur_path
        # all done.

    def _get_root_inventory(self):
        return self._inventory

    root_inventory = property(_get_root_inventory,
        doc="Root inventory of this tree")

    def _unpack_file_id(self, file_id):
        """Find the inventory and inventory file id for a tree file id.

        :param file_id: The tree file id, as bytestring or tuple
        :return: Inventory and inventory file id
        """
        if isinstance(file_id, tuple):
            if len(file_id) != 1:
                raise ValueError("nested trees not yet supported: %r" % file_id)
            file_id = file_id[0]
        return self.root_inventory, file_id

    @needs_read_lock
    def path2id(self, path):
        """Return the id for path in this tree."""
        return self._path2inv_file_id(path)[1]

    def _path2inv_file_id(self, path):
        """Lookup a inventory and inventory file id by path.

        :param path: Path to look up
        :return: tuple with inventory and inventory file id
        """
        # FIXME: Support nested trees
        return self.root_inventory, self.root_inventory.path2id(path)

    def id2path(self, file_id):
        """Return the path for a file id.

        :raises NoSuchId:
        """
        inventory, file_id = self._unpack_file_id(file_id)
        return inventory.id2path(file_id)

    def has_id(self, file_id):
        inventory, file_id = self._unpack_file_id(file_id)
        return inventory.has_id(file_id)

    def has_or_had_id(self, file_id):
        inventory, file_id = self._unpack_file_id(file_id)
        return inventory.has_id(file_id)

    def all_file_ids(self):
        return {entry.file_id for path, entry in self.iter_entries_by_dir()}

    def filter_unversioned_files(self, paths):
        """Filter out paths that are versioned.

        :return: set of paths.
        """
        # NB: we specifically *don't* call self.has_filename, because for
        # WorkingTrees that can indicate files that exist on disk but that
        # are not versioned.
        return set((p for p in paths if self.path2id(p) is None))

    @needs_read_lock
    def iter_entries_by_dir(self, specific_file_ids=None, yield_parents=False):
        """Walk the tree in 'by_dir' order.

        This will yield each entry in the tree as a (path, entry) tuple.
        The order that they are yielded is:

        See Tree.iter_entries_by_dir for details.

        :param yield_parents: If True, yield the parents from the root leading
            down to specific_file_ids that have been requested. This has no
            impact if specific_file_ids is None.
        """
        if specific_file_ids is None:
            inventory_file_ids = None
        else:
            inventory_file_ids = []
            for tree_file_id in specific_file_ids:
                inventory, inv_file_id = self._unpack_file_id(tree_file_id)
                if not inventory is self.root_inventory: # for now
                    raise AssertionError("%r != %r" % (
                        inventory, self.root_inventory))
                inventory_file_ids.append(inv_file_id)
        # FIXME: Handle nested trees
        return self.root_inventory.iter_entries_by_dir(
            specific_file_ids=inventory_file_ids, yield_parents=yield_parents)

    @needs_read_lock
    def iter_child_entries(self, file_id, path=None):
        inv, inv_file_id = self._unpack_file_id(file_id)
        return iter(viewvalues(inv[inv_file_id].children))

    def iter_children(self, file_id, path=None):
        """See Tree.iter_children."""
        entry = self.iter_entries_by_dir([file_id]).next()[1]
        for child in viewvalues(getattr(entry, 'children', {})):
            yield child.file_id


class MutableInventoryTree(mutabletree.MutableTree, InventoryTree):

    @mutabletree.needs_tree_write_lock
    def apply_inventory_delta(self, changes):
        """Apply changes to the inventory as an atomic operation.

        :param changes: An inventory delta to apply to the working tree's
            inventory.
        :return None:
        :seealso Inventory.apply_delta: For details on the changes parameter.
        """
        self.flush()
        inv = self.root_inventory
        inv.apply_delta(changes)
        self._write_inventory(inv)

    def _fix_case_of_inventory_path(self, path):
        """If our tree isn't case sensitive, return the canonical path"""
        if not self.case_sensitive:
            path = self.get_canonical_inventory_path(path)
        return path

    @mutabletree.needs_tree_write_lock
    def smart_add(self, file_list, recurse=True, action=None, save=True):
        """Version file_list, optionally recursing into directories.

        This is designed more towards DWIM for humans than API clarity.
        For the specific behaviour see the help for cmd_add().

        :param file_list: List of zero or more paths.  *NB: these are 
            interpreted relative to the process cwd, not relative to the 
            tree.*  (Add and most other tree methods use tree-relative
            paths.)
        :param action: A reporter to be called with the inventory, parent_ie,
            path and kind of the path being added. It may return a file_id if
            a specific one should be used.
        :param save: Save the inventory after completing the adds. If False
            this provides dry-run functionality by doing the add and not saving
            the inventory.
        :return: A tuple - files_added, ignored_files. files_added is the count
            of added files, and ignored_files is a dict mapping files that were
            ignored to the rule that caused them to be ignored.
        """
        # Not all mutable trees can have conflicts
        if getattr(self, 'conflicts', None) is not None:
            # Collect all related files without checking whether they exist or
            # are versioned. It's cheaper to do that once for all conflicts
            # than trying to find the relevant conflict for each added file.
            conflicts_related = set()
            for c in self.conflicts():
                conflicts_related.update(c.associated_filenames())
        else:
            conflicts_related = None
        adder = _SmartAddHelper(self, action, conflicts_related)
        adder.add(file_list, recurse=recurse)
        if save:
            invdelta = adder.get_inventory_delta()
            self.apply_inventory_delta(invdelta)
        return adder.added, adder.ignored

    def update_basis_by_delta(self, new_revid, delta):
        """Update the parents of this tree after a commit.

        This gives the tree one parent, with revision id new_revid. The
        inventory delta is applied to the current basis tree to generate the
        inventory for the parent new_revid, and all other parent trees are
        discarded.

        All the changes in the delta should be changes synchronising the basis
        tree with some or all of the working tree, with a change to a directory
        requiring that its contents have been recursively included. That is,
        this is not a general purpose tree modification routine, but a helper
        for commit which is not required to handle situations that do not arise
        outside of commit.

        See the inventory developers documentation for the theory behind
        inventory deltas.

        :param new_revid: The new revision id for the trees parent.
        :param delta: An inventory delta (see apply_inventory_delta) describing
            the changes from the current left most parent revision to new_revid.
        """
        # if the tree is updated by a pull to the branch, as happens in
        # WorkingTree2, when there was no separation between branch and tree,
        # then just clear merges, efficiency is not a concern for now as this
        # is legacy environments only, and they are slow regardless.
        if self.last_revision() == new_revid:
            self.set_parent_ids([new_revid])
            return
        # generic implementation based on Inventory manipulation. See
        # WorkingTree classes for optimised versions for specific format trees.
        basis = self.basis_tree()
        basis.lock_read()
        # TODO: Consider re-evaluating the need for this with CHKInventory
        # we don't strictly need to mutate an inventory for this
        # it only makes sense when apply_delta is cheaper than get_inventory()
        inventory = _mod_inventory.mutable_inventory_from_tree(basis)
        basis.unlock()
        inventory.apply_delta(delta)
        rev_tree = revisiontree.InventoryRevisionTree(self.branch.repository,
                                             inventory, new_revid)
        self.set_parent_trees([(new_revid, rev_tree)])


class _SmartAddHelper(object):
    """Helper for MutableTree.smart_add."""

    def get_inventory_delta(self):
        # GZ 2016-06-05: Returning view would probably be fine but currently
        # Inventory.apply_delta is documented as requiring a list of changes.
        return list(viewvalues(self._invdelta))

    def _get_ie(self, inv_path):
        """Retrieve the most up to date inventory entry for a path.

        :param inv_path: Normalized inventory path
        :return: Inventory entry (with possibly invalid .children for
            directories)
        """
        entry = self._invdelta.get(inv_path)
        if entry is not None:
            return entry[3]
        # Find a 'best fit' match if the filesystem is case-insensitive
        inv_path = self.tree._fix_case_of_inventory_path(inv_path)
        file_id = self.tree.path2id(inv_path)
        if file_id is not None:
            return self.tree.iter_entries_by_dir([file_id]).next()[1]
        return None

    def _convert_to_directory(self, this_ie, inv_path):
        """Convert an entry to a directory.

        :param this_ie: Inventory entry
        :param inv_path: Normalized path for the inventory entry
        :return: The new inventory entry
        """
        # Same as in _add_one below, if the inventory doesn't
        # think this is a directory, update the inventory
        this_ie = _mod_inventory.InventoryDirectory(
            this_ie.file_id, this_ie.name, this_ie.parent_id)
        self._invdelta[inv_path] = (inv_path, inv_path, this_ie.file_id,
            this_ie)
        return this_ie

    def _add_one_and_parent(self, parent_ie, path, kind, inv_path):
        """Add a new entry to the inventory and automatically add unversioned parents.

        :param parent_ie: Parent inventory entry if known, or None.  If
            None, the parent is looked up by name and used if present, otherwise it
            is recursively added.
        :param path: 
        :param kind: Kind of new entry (file, directory, etc)
        :param inv_path:
        :return: Inventory entry for path and a list of paths which have been added.
        """
        # Nothing to do if path is already versioned.
        # This is safe from infinite recursion because the tree root is
        # always versioned.
        inv_dirname = osutils.dirname(inv_path)
        dirname, basename = osutils.split(path)
        if parent_ie is None:
            # slower but does not need parent_ie
            this_ie = self._get_ie(inv_path)
            if this_ie is not None:
                return this_ie
            # its really not there : add the parent
            # note that the dirname use leads to some extra str copying etc but as
            # there are a limited number of dirs we can be nested under, it should
            # generally find it very fast and not recurse after that.
            parent_ie = self._add_one_and_parent(None,
                dirname, 'directory', 
                inv_dirname)
        # if the parent exists, but isn't a directory, we have to do the
        # kind change now -- really the inventory shouldn't pretend to know
        # the kind of wt files, but it does.
        if parent_ie.kind != 'directory':
            # nb: this relies on someone else checking that the path we're using
            # doesn't contain symlinks.
            parent_ie = self._convert_to_directory(parent_ie, inv_dirname)
        file_id = self.action(self.tree, parent_ie, path, kind)
        entry = _mod_inventory.make_entry(kind, basename, parent_ie.file_id,
            file_id=file_id)
        self._invdelta[inv_path] = (None, inv_path, entry.file_id, entry)
        self.added.append(inv_path)
        return entry

    def _gather_dirs_to_add(self, user_dirs):
        # only walk the minimal parents needed: we have user_dirs to override
        # ignores.
        prev_dir = None

        is_inside = osutils.is_inside_or_parent_of_any
        for path in sorted(user_dirs):
            if (prev_dir is None or not is_inside([prev_dir], path)):
                inv_path, this_ie = user_dirs[path]
                yield (path, inv_path, this_ie, None)
            prev_dir = path

    def __init__(self, tree, action, conflicts_related=None):
        self.tree = tree
        if action is None:
            self.action = add.AddAction()
        else:
            self.action = action
        self._invdelta = {}
        self.added = []
        self.ignored = {}
        if conflicts_related is None:
            self.conflicts_related = frozenset()
        else:
            self.conflicts_related = conflicts_related

    def add(self, file_list, recurse=True):
        from breezy.inventory import InventoryEntry
        if not file_list:
            # no paths supplied: add the entire tree.
            # FIXME: this assumes we are running in a working tree subdir :-/
            # -- vila 20100208
            file_list = [u'.']

        # expand any symlinks in the directory part, while leaving the
        # filename alone
        # only expanding if symlinks are supported avoids windows path bugs
        if osutils.has_symlinks():
            file_list = list(map(osutils.normalizepath, file_list))

        user_dirs = {}
        # validate user file paths and convert all paths to tree
        # relative : it's cheaper to make a tree relative path an abspath
        # than to convert an abspath to tree relative, and it's cheaper to
        # perform the canonicalization in bulk.
        for filepath in osutils.canonical_relpaths(self.tree.basedir, file_list):
            # validate user parameters. Our recursive code avoids adding new
            # files that need such validation
            if self.tree.is_control_filename(filepath):
                raise errors.ForbiddenControlFileError(filename=filepath)

            abspath = self.tree.abspath(filepath)
            kind = osutils.file_kind(abspath)
            # ensure the named path is added, so that ignore rules in the later
            # directory walk dont skip it.
            # we dont have a parent ie known yet.: use the relatively slower
            # inventory probing method
            inv_path, _ = osutils.normalized_filename(filepath)
            this_ie = self._get_ie(inv_path)
            if this_ie is None:
                this_ie = self._add_one_and_parent(None, filepath, kind, inv_path)
            if kind == 'directory':
                # schedule the dir for scanning
                user_dirs[filepath] = (inv_path, this_ie)

        if not recurse:
            # no need to walk any directories at all.
            return

        things_to_add = list(self._gather_dirs_to_add(user_dirs))

        illegalpath_re = re.compile(r'[\r\n]')
        for directory, inv_path, this_ie, parent_ie in things_to_add:
            # directory is tree-relative
            abspath = self.tree.abspath(directory)

            # get the contents of this directory.

            # find the kind of the path being added, and save stat_value
            # for reuse
            stat_value = None
            if this_ie is None:
                stat_value = osutils.file_stat(abspath)
                kind = osutils.file_kind_from_stat_mode(stat_value.st_mode)
            else:
                kind = this_ie.kind
            
            # allow AddAction to skip this file
            if self.action.skip_file(self.tree,  abspath,  kind,  stat_value):
                continue
            if not InventoryEntry.versionable_kind(kind):
                trace.warning("skipping %s (can't add file of kind '%s')",
                              abspath, kind)
                continue
            if illegalpath_re.search(directory):
                trace.warning("skipping %r (contains \\n or \\r)" % abspath)
                continue
            if directory in self.conflicts_related:
                # If the file looks like one generated for a conflict, don't
                # add it.
                trace.warning(
                    'skipping %s (generated to help resolve conflicts)',
                    abspath)
                continue

            if kind == 'directory' and directory != '':
                try:
                    transport = _mod_transport.get_transport_from_path(abspath)
                    controldir.ControlDirFormat.find_format(transport)
                    sub_tree = True
                except errors.NotBranchError:
                    sub_tree = False
                except errors.UnsupportedFormatError:
                    sub_tree = True
            else:
                sub_tree = False

            if this_ie is not None:
                pass
            elif sub_tree:
                # XXX: This is wrong; people *might* reasonably be trying to
                # add subtrees as subtrees.  This should probably only be done
                # in formats which can represent subtrees, and even then
                # perhaps only when the user asked to add subtrees.  At the
                # moment you can add them specially through 'join --reference',
                # which is perhaps reasonable: adding a new reference is a
                # special operation and can have a special behaviour.  mbp
                # 20070306
                trace.warning("skipping nested tree %r", abspath)
            else:
                this_ie = self._add_one_and_parent(parent_ie, directory, kind,
                    inv_path)

            if kind == 'directory' and not sub_tree:
                if this_ie.kind != 'directory':
                    this_ie = self._convert_to_directory(this_ie, inv_path)

                for subf in sorted(os.listdir(abspath)):
                    inv_f, _ = osutils.normalized_filename(subf)
                    # here we could use TreeDirectory rather than
                    # string concatenation.
                    subp = osutils.pathjoin(directory, subf)
                    # TODO: is_control_filename is very slow. Make it faster.
                    # TreeDirectory.is_control_filename could also make this
                    # faster - its impossible for a non root dir to have a
                    # control file.
                    if self.tree.is_control_filename(subp):
                        trace.mutter("skip control directory %r", subp)
                        continue
                    sub_invp = osutils.pathjoin(inv_path, inv_f)
                    entry = self._invdelta.get(sub_invp)
                    if entry is not None:
                        sub_ie = entry[3]
                    else:
                        sub_ie = this_ie.children.get(inv_f)
                    if sub_ie is not None:
                        # recurse into this already versioned subdir.
                        things_to_add.append((subp, sub_invp, sub_ie, this_ie))
                    else:
                        # user selection overrides ignores
                        # ignore while selecting files - if we globbed in the
                        # outer loop we would ignore user files.
                        ignore_glob = self.tree.is_ignored(subp)
                        if ignore_glob is not None:
                            self.ignored.setdefault(ignore_glob, []).append(subp)
                        else:
                            things_to_add.append((subp, sub_invp, None, this_ie))
