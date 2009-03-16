# Copyright (C) 2008 Canonical Ltd
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

"""An abstraction of a repository providing just the bits importing needs."""


from bzrlib import errors, inventory, knit, lru_cache, osutils
from bzrlib import revision as _mod_revision


class AbstractRevisionStore(object):

    def __init__(self, repo):
        """An object responsible for loading revisions into a repository.

        NOTE: Repository locking is not managed by this class. Clients
        should take a write lock, call load() multiple times, then release
        the lock.

        :param repository: the target repository
        """
        self.repo = repo
        self._supports_chks = getattr(repo._format, 'supports_chks', False)

    def expects_rich_root(self):
        """Does this store expect inventories with rich roots?"""
        return self.repo.supports_rich_root()

    def init_inventory(self, revision_id):
        """Generate an inventory for a parentless revision."""
        if self._supports_chks:
            inv = self._init_chk_inventory(revision_id, inventory.ROOT_ID)
        else:
            inv = inventory.Inventory(revision_id=revision_id)
            if self.expects_rich_root():
                # The very first root needs to have the right revision
                inv.root.revision = revision_id
        return inv

    def _init_chk_inventory(self, revision_id, root_id):
        """Generate a CHKInventory for a parentless revision."""
        from bzrlib import chk_map
        # Get the creation parameters
        chk_store = self.repo.chk_bytes
        serializer = self.repo._format._serializer
        search_key_name = serializer.search_key_name
        maximum_size = serializer.maximum_size

        # Maybe the rest of this ought to be part of the CHKInventory API?
        inv = inventory.CHKInventory(search_key_name)
        inv.revision_id = revision_id
        inv.root_id = root_id
        search_key_func = chk_map.search_key_registry.get(search_key_name)
        inv.id_to_entry = chk_map.CHKMap(chk_store, None, search_key_func)
        inv.id_to_entry._root_node.set_maximum_size(maximum_size)
        inv.parent_id_basename_to_file_id = chk_map.CHKMap(chk_store,
            None, search_key_func)
        inv.parent_id_basename_to_file_id._root_node.set_maximum_size(
            maximum_size)
        inv.parent_id_basename_to_file_id._root_node._key_width = 2
        return inv

    def get_inventory(self, revision_id):
        """Get a stored inventory."""
        return self.repo.get_inventory(revision_id)

    def get_file_text(self, revision_id, file_id):
        """Get the text stored for a file in a given revision."""
        revtree = self.repo.revision_tree(revision_id)
        return revtree.get_file_text(file_id)

    def get_file_lines(self, revision_id, file_id):
        """Get the lines stored for a file in a given revision."""
        revtree = self.repo.revision_tree(revision_id)
        return osutils.split_lines(revtree.get_file_text(file_id))

    def start_new_revision(self, revision, parents, parent_invs):
        """Init the metadata needed for get_parents_and_revision_for_entry().

        :param revision: a Revision object
        """
        self._current_rev_id = revision.revision_id
        self._rev_parents = parents
        self._rev_parent_invs = parent_invs
        # We don't know what the branch will be so there's no real BranchConfig.
        # That means we won't be triggering any hooks and that's a good thing.
        # Without a config though, we must pass in the committer below so that
        # the commit builder doesn't try to look up the config.
        config = None
        # We can't use self.repo.get_commit_builder() here because it starts a
        # new write group. We want one write group around a batch of imports
        # where the default batch size is currently 10000. IGC 20090312
        self._commit_builder = self.repo._commit_builder_class(self.repo,
            parents, config, timestamp=revision.timestamp,
            timezone=revision.timezone, committer=revision.committer,
            revprops=revision.properties, revision_id=revision.revision_id)

    def get_parents_and_revision_for_entry(self, ie):
        """Get the parents and revision for an inventory entry.
 
        :param ie: the inventory entry
        :return parents, revision_id where
            parents is the tuple of parent revision_ids for the per-file graph
            revision_id is the revision_id to use for this entry
        """
        # Check for correct API usage
        if self._current_rev_id is None:
            raise AssertionError("start_new_revision() must be called"
                " before get_parents_and_revision_for_entry()")
        if ie.revision != self._current_rev_id:
            raise AssertionError("start_new_revision() registered a different"
                " revision (%s) to that in the inventory entry (%s)" %
                (self._current_rev_id, ie.revision))

        # Find the heads. This code is lifted from
        # repository.CommitBuilder.record_entry_contents().
        parent_candidate_entries = ie.parent_candidates(self._rev_parent_invs)
        head_set = self._commit_builder._heads(ie.file_id,
            parent_candidate_entries.keys())
        heads = []
        for inv in self._rev_parent_invs:
            if ie.file_id in inv:
                old_rev = inv[ie.file_id].revision
                if old_rev in head_set:
                    rev_id = inv[ie.file_id].revision
                    heads.append(rev_id)
                    head_set.remove(rev_id)

        # Find the revision to use. If the content has not changed
        # since the parent, record the parent's revision.
        if len(heads) == 0:
            return (), ie.revision
        parent_entry = parent_candidate_entries[heads[0]]
        changed = False
        if len(heads) > 1:
            changed = True
        elif (parent_entry.name != ie.name or parent_entry.kind != ie.kind or
            parent_entry.parent_id != ie.parent_id): 
            changed = True
        elif ie.kind == 'file':
            if (parent_entry.text_sha1 != ie.text_sha1 or
                parent_entry.executable != ie.executable):
                changed = True
        elif ie.kind == 'symlink':
            if parent_entry.symlink_target != ie.symlink_target:
                changed = True
        if changed:
            rev_id = ie.revision
        else:
            rev_id = parent_entry.revision
        return tuple(heads), rev_id

    def load(self, rev, inv, signature, text_provider, parents_provider,
        inventories_provider=None):
        """Load a revision.

        :param rev: the Revision
        :param inv: the inventory
        :param signature: signing information
        :param text_provider: a callable expecting a file_id parameter
            that returns the text for that file-id
        :param parents_provider: a callable expecting a file_id parameter
            that return the list of parent-ids for that file-id
        :param inventories_provider: a callable expecting a repository and
            a list of revision-ids, that returns:
              * the list of revision-ids present in the repository
              * the list of inventories for the revision-id's,
                including an empty inventory for the missing revisions
            If None, a default implementation is provided.
        """
        # NOTE: This is bzrlib.repository._install_revision refactored to
        # to provide more flexibility in how previous revisions are cached,
        # data is feed in, etc.

        # Get the non-ghost parents and their inventories
        if inventories_provider is None:
            inventories_provider = self._default_inventories_provider
        present_parents, parent_invs = inventories_provider(rev.parent_ids)

        # Load the inventory
        try:
            rev.inventory_sha1 = self._add_inventory(rev.revision_id,
                inv, present_parents, parent_invs)
        except errors.RevisionAlreadyPresent:
            pass

        # Load the texts, signature and revision
        entries = self._non_root_entries_iter(inv, rev.revision_id)
        self._load_texts(rev.revision_id, entries, text_provider,
            parents_provider)
        if signature is not None:
            self.repo.add_signature_text(rev.revision_id, signature)
        self._add_revision(rev, inv)

    def chk_load(self, rev, basis_inv, inv_delta, signature,
        text_provider, parents_provider, inventories_provider=None):
        """Load a revision for a CHKInventory.

        :param rev: the Revision
        :param basis_inv: the basis CHKInventory
        :param inv_delta: the inventory delta
        :param signature: signing information
        :param text_provider: a callable expecting a file_id parameter
            that returns the text for that file-id
        :param parents_provider: a callable expecting a file_id parameter
            that return the list of parent-ids for that file-id
        :param inventories_provider: a callable expecting a repository and
            a list of revision-ids, that returns:
              * the list of revision-ids present in the repository
              * the list of inventories for the revision-id's,
                including an empty inventory for the missing revisions
            If None, a default implementation is provided.
        """
        # Get the non-ghost parents and their inventories
        if inventories_provider is None:
            inventories_provider = self._default_inventories_provider
        present_parents, parent_invs = inventories_provider(rev.parent_ids)

        # Load the inventory
        try:
            rev_id = rev.revision_id
            rev.inventory_sha1, inv = self._add_inventory_by_delta(
                rev_id, basis_inv, inv_delta, present_parents, parent_invs)
        except errors.RevisionAlreadyPresent:
            pass

        # Load the texts, signature and revision
        file_rev_ids_needing_texts = [(id, ie.revision)
            for _, n, id, ie in inv_delta
            if n is not None and ie.revision == rev_id]
        self._load_texts_for_file_rev_ids(file_rev_ids_needing_texts,
            text_provider, parents_provider)
        if signature is not None:
            self.repo.add_signature_text(rev_id, signature)
        self._add_revision(rev, inv)
        return inv

    def _non_root_entries_iter(self, inv, revision_id):
        if hasattr(inv, 'iter_non_root_entries'):
            entries = inv.iter_non_root_entries()
        else:
            path_entries = inv.iter_entries()
            # Backwards compatibility hack: skip the root id.
            if not self.repo.supports_rich_root():
                path, root = path_entries.next()
                if root.revision != revision_id:
                    raise errors.IncompatibleRevision(repr(self.repo))
            entries = iter([ie for path, ie in path_entries])
        return entries

    def _load_texts(self, revision_id, entries, text_provider,
        parents_provider):
        """Load texts to a repository for inventory entries.
        
        This method is provided for subclasses to use or override.

        :param revision_id: the revision identifier
        :param entries: iterator over the inventory entries
        :param text_provider: a callable expecting a file_id parameter
            that returns the text for that file-id
        :param parents_provider: a callable expecting a file_id parameter
            that return the list of parent-ids for that file-id
        """
        raise NotImplementedError(self._load_texts)

    def _add_inventory(self, revision_id, inv, parents, parent_invs):
        """Add the inventory inv to the repository as revision_id.
        
        :param parents: The revision ids of the parents that revision_id
                        is known to have and are in the repository already.
        :param parent_invs: the parent inventories

        :returns: The validator(which is a sha1 digest, though what is sha'd is
            repository format specific) of the serialized inventory.
        """
        return self.repo.add_inventory(revision_id, inv, parents)

    def _add_inventory_by_delta(self, revision_id, basis_inv, inv_delta,
        parents, parent_invs):
        """Add the inventory to the repository as revision_id.
        
        :param basis_inv: the basis CHKInventory
        :param inv_delta: the inventory delta
        :param parents: The revision ids of the parents that revision_id
                        is known to have and are in the repository already.
        :param parent_invs: the parent inventories

        :returns: (validator, inv) where validator is the validator
          (which is a sha1 digest, though what is sha'd is repository format
          specific) of the serialized inventory;
          inv is the generated inventory
        """
        if len(parents):
            validator, new_inv = self.repo.add_inventory_by_delta(parents[0],
                inv_delta, revision_id, parents, basis_inv=basis_inv,
                propagate_caches=True)
        else:
            new_inv = basis_inv.create_by_apply_delta(inv_delta, revision_id)
            validator = self.repo.add_inventory(revision_id, new_inv, parents)
        return validator, new_inv

    def _add_revision(self, rev, inv):
        """Add a revision and its inventory to a repository.

        :param rev: the Revision
        :param inv: the inventory
        """
        self.repo.add_revision(rev.revision_id, rev, inv)

    def _default_inventories_provider(self, revision_ids):
        """An inventories provider that queries the repository."""
        present = []
        inventories = []
        for revision_id in revision_ids:
            if self.repo.has_revision(revision_id):
                present.append(revision_id)
                rev_tree = self.repo.revision_tree(revision_id)
            else:
                rev_tree = self.repo.revision_tree(None)
            inventories.append(rev_tree.inventory)
        return present, inventories


class RevisionStore1(AbstractRevisionStore):
    """A RevisionStore that uses the old bzrlib Repository API.
    
    The old API was present until bzr.dev rev 3510.
    """

    def _load_texts(self, revision_id, entries, text_provider, parents_provider):
        """See RevisionStore._load_texts()."""
        # Add the texts that are not already present
        tx = self.repo.get_transaction()
        for ie in entries:
            # This test is *really* slow: over 50% of import time
            #w = self.repo.weave_store.get_weave_or_empty(ie.file_id, tx)
            #if ie.revision in w:
            #    continue
            # Try another way, realising that this assumes that the
            # version is not already there. In the general case,
            # a shared repository might already have the revision but
            # we arguably don't need that check when importing from
            # a foreign system.
            if ie.revision != revision_id:
                continue
            file_id = ie.file_id
            text_parents = [(file_id, p) for p in parents_provider(file_id)]
            lines = text_provider(file_id)
            vfile = self.repo.weave_store.get_weave_or_empty(file_id,  tx)
            vfile.add_lines(revision_id, text_parents, lines)

    def get_file_lines(self, revision_id, file_id):
        tx = self.repo.get_transaction()
        w = self.repo.weave_store.get_weave(file_id, tx)
        return w.get_lines(revision_id)

    def _add_revision(self, rev, inv):
        # There's no need to do everything repo.add_revision does and
        # doing so (since bzr.dev 3392) can be pretty slow for long
        # delta chains on inventories. Just do the essentials here ...
        _mod_revision.check_not_reserved_id(rev.revision_id)
        self.repo._revision_store.add_revision(rev, self.repo.get_transaction())


class RevisionStore2(AbstractRevisionStore):
    """A RevisionStore that uses the new bzrlib Repository API."""

    def _load_texts(self, revision_id, entries, text_provider, parents_provider):
        """See RevisionStore._load_texts()."""
        text_keys = {}
        for ie in entries:
            text_keys[(ie.file_id, ie.revision)] = ie
        text_parent_map = self.repo.texts.get_parent_map(text_keys)
        missing_texts = set(text_keys) - set(text_parent_map)
        self._load_texts_for_file_rev_ids(missing_texts, text_provider,
            parents_provider)

    def _load_texts_for_file_rev_ids(self, file_rev_ids, text_provider,
        parents_provider):
        """Load texts to a repository for file-ids, revision-id tuples.
        
        :param file_rev_ids: iterator over the (file_id, revision_id) tuples
        :param text_provider: a callable expecting a file_id parameter
            that returns the text for that file-id
        :param parents_provider: a callable expecting a file_id parameter
            that return the list of parent-ids for that file-id
        """
        for file_id, revision_id in file_rev_ids:
            text_key = (file_id, revision_id)
            text_parents = [(file_id, p) for p in parents_provider(file_id)]
            lines = text_provider(file_id)
            #print "adding text for %s\n\tparents:%s" % (text_key,text_parents)
            self.repo.texts.add_lines(text_key, text_parents, lines)

    def get_file_lines(self, revision_id, file_id):
        record = self.repo.texts.get_record_stream([(file_id, revision_id)],
            'unordered', True).next()
        if record.storage_kind == 'absent':
            raise errors.RevisionNotPresent(record.key, self.repo)
        return osutils.split_lines(record.get_bytes_as('fulltext'))

    # This is breaking imports into brisbane-core currently
    #def _add_revision(self, rev, inv):
    #    # There's no need to do everything repo.add_revision does and
    #    # doing so (since bzr.dev 3392) can be pretty slow for long
    #    # delta chains on inventories. Just do the essentials here ...
    #    _mod_revision.check_not_reserved_id(rev.revision_id)
    #    self.repo._add_revision(rev)
 

class ImportRevisionStore1(RevisionStore1):
    """A RevisionStore (old Repository API) optimised for importing.

    This implementation caches serialised inventory texts and provides
    fine-grained control over when inventories are stored as fulltexts.
    """

    def __init__(self, repo, parent_texts_to_cache=1, fulltext_when=None,
        random_ids=True):
        """See AbstractRevisionStore.__init__.

        :param repository: the target repository
        :param parent_text_to_cache: the number of parent texts to cache
        :para fulltext_when: if non None, a function to call to decide
          whether to fulltext the inventory or not. The revision count
          is passed as a parameter and the result is treated as a boolean.
        """
        RevisionStore1.__init__(self, repo)
        self.inv_parent_texts = lru_cache.LRUCache(parent_texts_to_cache)
        self.fulltext_when = fulltext_when
        self.random_ids = random_ids
        self.revision_count = 0

    def _add_inventory(self, revision_id, inv, parents, parent_invs):
        """See RevisionStore._add_inventory."""
        # Code taken from bzrlib.repository.add_inventory
        assert self.repo.is_in_write_group()
        _mod_revision.check_not_reserved_id(revision_id)
        assert inv.revision_id is None or inv.revision_id == revision_id, \
            "Mismatch between inventory revision" \
            " id and insertion revid (%r, %r)" % (inv.revision_id, revision_id)
        assert inv.root is not None
        inv_lines = self.repo._serialise_inventory_to_lines(inv)
        inv_vf = self.repo.get_inventory_weave()
        sha1, num_bytes, parent_text = self._inventory_add_lines(inv_vf,
            revision_id, parents, inv_lines, self.inv_parent_texts)
        self.inv_parent_texts[revision_id] = parent_text
        return sha1

    def _inventory_add_lines(self, inv_vf, version_id, parents, lines,
            parent_texts):
        """See Repository._inventory_add_lines()."""
        # setup parameters used in original code but not this API
        self.revision_count += 1
        if self.fulltext_when is not None:
            delta = not self.fulltext_when(self.revision_count)
        else:
            delta = inv_vf.delta
        left_matching_blocks = None
        random_id = self.random_ids
        check_content = False

        # bzrlib.knit.add_lines() but error checking optimised
        inv_vf._check_add(version_id, lines, random_id, check_content)

        ####################################################################
        # bzrlib.knit._add() but skip checking if fulltext better than delta
        ####################################################################

        line_bytes = ''.join(lines)
        digest = osutils.sha_string(line_bytes)
        present_parents = []
        for parent in parents:
            if inv_vf.has_version(parent):
                present_parents.append(parent)
        if parent_texts is None:
            parent_texts = {}

        # can only compress against the left most present parent.
        if (delta and
            (len(present_parents) == 0 or
             present_parents[0] != parents[0])):
            delta = False

        text_length = len(line_bytes)
        options = []
        if lines:
            if lines[-1][-1] != '\n':
                # copy the contents of lines.
                lines = lines[:]
                options.append('no-eol')
                lines[-1] = lines[-1] + '\n'
                line_bytes += '\n'

        #if delta:
        #    # To speed the extract of texts the delta chain is limited
        #    # to a fixed number of deltas.  This should minimize both
        #    # I/O and the time spend applying deltas.
        #    delta = inv_vf._check_should_delta(present_parents)

        assert isinstance(version_id, str)
        content = inv_vf.factory.make(lines, version_id)
        if delta or (inv_vf.factory.annotated and len(present_parents) > 0):
            # Merge annotations from parent texts if needed.
            delta_hunks = inv_vf._merge_annotations(content, present_parents,
                parent_texts, delta, inv_vf.factory.annotated,
                left_matching_blocks)

        if delta:
            options.append('line-delta')
            store_lines = inv_vf.factory.lower_line_delta(delta_hunks)
            size, bytes = inv_vf._data._record_to_data(version_id, digest,
                store_lines)
        else:
            options.append('fulltext')
            # isinstance is slower and we have no hierarchy.
            if inv_vf.factory.__class__ == knit.KnitPlainFactory:
                # Use the already joined bytes saving iteration time in
                # _record_to_data.
                size, bytes = inv_vf._data._record_to_data(version_id, digest,
                    lines, [line_bytes])
            else:
                # get mixed annotation + content and feed it into the
                # serialiser.
                store_lines = inv_vf.factory.lower_fulltext(content)
                size, bytes = inv_vf._data._record_to_data(version_id, digest,
                    store_lines)

        access_memo = inv_vf._data.add_raw_records([size], bytes)[0]
        inv_vf._index.add_versions(
            ((version_id, options, access_memo, parents),),
            random_id=random_id)
        return digest, text_length, content
