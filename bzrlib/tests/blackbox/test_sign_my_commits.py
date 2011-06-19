# Copyright (C) 2005 Canonical Ltd
# -*- coding: utf-8 -*-
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

"""Black-box tests for bzr sign-my-commits."""

from bzrlib import (
    gpg,
    tests,
    )


class SignMyCommits(tests.TestCaseWithTransport):

    def monkey_patch_gpg(self):
        """Monkey patch the gpg signing strategy to be a loopback.

        This also registers the cleanup, so that we will revert to
        the original gpg strategy when done.
        """
        # monkey patch gpg signing mechanism
        self.overrideAttr(gpg, 'GPGStrategy', gpg.LoopbackGPGStrategy)

    def setup_tree(self, location='.'):
        wt = self.make_branch_and_tree(location)
        wt.commit("base A", allow_pointless=True, rev_id='A')
        wt.commit("base B", allow_pointless=True, rev_id='B')
        wt.commit("base C", allow_pointless=True, rev_id='C')
        wt.commit("base D", allow_pointless=True, rev_id='D',
                committer='Alternate <alt@foo.com>')
        wt.add_parent_tree_id("aghost")
        wt.commit("base E", allow_pointless=True, rev_id='E')
        return wt

    def assertUnsigned(self, repo, revision_id):
        """Assert that revision_id is not signed in repo."""
        self.assertFalse(repo.has_signature_for_revision_id(revision_id))

    def assertSigned(self, repo, revision_id):
        """Assert that revision_id is signed in repo."""
        self.assertTrue(repo.has_signature_for_revision_id(revision_id))

    def test_sign_my_commits(self):
        #Test re signing of data.
        wt = self.setup_tree()
        repo = wt.branch.repository

        self.monkey_patch_gpg()

        self.assertUnsigned(repo, 'A')
        self.assertUnsigned(repo, 'B')
        self.assertUnsigned(repo, 'C')
        self.assertUnsigned(repo, 'D')

        self.run_bzr('sign-my-commits')

        self.assertSigned(repo, 'A')
        self.assertSigned(repo, 'B')
        self.assertSigned(repo, 'C')
        self.assertUnsigned(repo, 'D')

    def test_sign_my_commits_location(self):
        wt = self.setup_tree('other')
        repo = wt.branch.repository

        self.monkey_patch_gpg()

        self.run_bzr('sign-my-commits other')

        self.assertSigned(repo, 'A')
        self.assertSigned(repo, 'B')
        self.assertSigned(repo, 'C')
        self.assertUnsigned(repo, 'D')

    def test_sign_diff_committer(self):
        wt = self.setup_tree()
        repo = wt.branch.repository

        self.monkey_patch_gpg()

        self.run_bzr(['sign-my-commits', '.', 'Alternate <alt@foo.com>'])

        self.assertUnsigned(repo, 'A')
        self.assertUnsigned(repo, 'B')
        self.assertUnsigned(repo, 'C')
        self.assertSigned(repo, 'D')

    def test_sign_dry_run(self):
        wt = self.setup_tree()
        repo = wt.branch.repository

        self.monkey_patch_gpg()

        out = self.run_bzr('sign-my-commits --dry-run')[0]

        outlines = out.splitlines()
        self.assertEquals(5, len(outlines))
        self.assertEquals('Signed 4 revisions', outlines[-1])
        self.assertUnsigned(repo, 'A')
        self.assertUnsigned(repo, 'B')
        self.assertUnsigned(repo, 'C')
        self.assertUnsigned(repo, 'D')
        self.assertUnsigned(repo, 'E')
