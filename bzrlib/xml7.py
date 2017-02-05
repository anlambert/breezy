# Copyright (C) 2006-2010 Canonical Ltd
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

from __future__ import absolute_import

from bzrlib import (
    inventory,
    xml6,
    )


class Serializer_v7(xml6.Serializer_v6):
    """A Serializer that supports tree references"""

    # this format is used by BzrBranch6

    supported_kinds = {'file', 'directory', 'symlink', 'tree-reference'}
    format_num = '7'


serializer_v7 = Serializer_v7()
