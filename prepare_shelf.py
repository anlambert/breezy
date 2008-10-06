# Copyright (C) 2008 Aaron Bentley <aaron@aaronbentley.com>
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program; if not, write to the Free Software
#    Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA


from cStringIO import StringIO

from bzrlib import merge3, pack, transform

from bzrlib.plugins.shelf2 import serialize_transform


class ShelfCreator(object):

    def __init__(self, work_tree, target_tree):
        self.work_tree = work_tree
        self.work_transform = transform.TreeTransform(work_tree)
        self.target_tree = target_tree
        self.shelf_transform = transform.TransformPreview(self.target_tree)
        self.renames = {}
        self.iter_changes = work_tree.iter_changes(self.target_tree)

    def __iter__(self):
        for (file_id, paths, changed, versioned, parents, names, kind,
             executable) in self.iter_changes:
            if kind[0] is None or versioned[0] == False:
                yield ('add file', file_id, kind[1])
            else:
                if names[0] != names[1] or parents[0] != parents[1]:
                    self.renames[file_id] = (names, parents)
                    yield ('rename', file_id) + paths
                if changed:
                    yield ('modify text', file_id)

    def shelve_rename(self, file_id):
        names, parents = self.renames[file_id]
        w_trans_id = self.work_transform.trans_id_file_id(file_id)
        work_parent = self.work_transform.trans_id_file_id(parents[0])
        self.work_transform.adjust_path(names[0], work_parent, w_trans_id)

        s_trans_id = self.shelf_transform.trans_id_file_id(file_id)
        shelf_parent = self.shelf_transform.trans_id_file_id(parents[1])
        self.shelf_transform.adjust_path(names[1], shelf_parent, s_trans_id)

    def shelve_text(self, file_id, new_text):
        s = StringIO()
        s.writelines(new_text)
        s.seek(0)
        new_lines = s.readlines()
        w_trans_id = self.work_transform.trans_id_file_id(file_id)
        self.work_transform.delete_contents(w_trans_id)
        self.work_transform.create_file(new_lines, w_trans_id)

        s_trans_id = self.shelf_transform.trans_id_file_id(file_id)
        self.shelf_transform.delete_contents(s_trans_id)
        inverse_lines = self._inverse_lines(new_lines, file_id)
        self.shelf_transform.create_file(inverse_lines, s_trans_id)

    def shelve_creation(self, file_id, kind):
        w_trans_id = self.work_transform.trans_id_file_id(file_id)
        self.work_transform.delete_contents(w_trans_id)
        self.work_transform.unversion_file(w_trans_id)

        s_trans_id = self.shelf_transform.trans_id_file_id(file_id)
        if kind == 'file':
            lines = self.read_tree_lines(file_id)
            self.shelf_transform.create_file(lines, s_trans_id)
        if kind == 'directory':
            self.shelf_transform.create_directory(s_trans_id)
        if kind == 'symlink':
            target = self.work_tree.get_symlink_target(file_id)
            self.shelf_transform.create_symlink(target, s_trans_id)
        self.shelf_transform.version_file(file_id, s_trans_id)

    def read_tree_lines(self, file_id):
        tree_file = self.work_tree.get_file(file_id)
        try:
            return tree_file.readlines()
        finally:
            tree_file.close()

    def _inverse_lines(self, new_lines, file_id):
        """Produce a version with only those changes removed from new_lines."""
        target_lines = self.target_tree.get_file_lines(file_id)
        work_lines = self.read_tree_lines(file_id)
        return merge3.Merge3(new_lines, target_lines, work_lines).merge_lines()

    def finalize(self):
        self.work_transform.finalize()
        self.shelf_transform.finalize()

    def transform(self):
        self.work_transform.apply()

    def make_shelf_filename(self):
        transport = self.work_tree.bzrdir.root_transport.clone('.shelf2')
        transport.ensure_base()
        return transport.local_abspath('01')

    def write_shelf(self):
        filename = self.make_shelf_filename()
        shelf_file = open(filename, 'wb')
        try:
            serializer = pack.ContainerSerialiser()
            shelf_file.write(serializer.begin())
            for bytes in serialize_transform.serialize(
                self.shelf_transform, serializer):
                shelf_file.write(bytes)
            shelf_file.write(serializer.end())
        finally:
            shelf_file.close()
        return filename


class Unshelver(object):

    def __init__(self, tree, base_tree, transform):
        self.tree = tree
        self.transform = transform

    @classmethod
    def from_tree_and_shelf(klass, tree, shelf_filename):
        parser = pack.ContainerPushParser()
        shelf_file = open(shelf_filename, 'rb')
        try:
            parser.accept_bytes(shelf_file.read())
        finally:
            shelf_file.close()
        tt = transform.TreeTransform(tree)
        records = iter(parser.read_pending_records())
        serialize_transform.deserialize(tt, records)
        return klass(tree, tree, tt)

    def unshelve(self):
        self.transform.apply()
