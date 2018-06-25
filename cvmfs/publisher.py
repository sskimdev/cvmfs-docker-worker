# NOTE:
# This file is forked from singularity 2.2's "cli.py".  I have left the
# copyright notice below untouched; this file remains under the same license.
# - Brian Bockelman

'''
bootstrap.py: python helper for Singularity command line tool
Copyright (c) 2016, Vanessa Sochat. All rights reserved.
"Singularity" Copyright (c) 2016, The Regents of the University of California,
through Lawrence Berkeley National Laboratory (subject to receipt of any
required approvals from the U.S. Dept. of Energy).  All rights reserved.

This software is licensed under a customized 3-clause BSD license.  Please
consult LICENSE file distributed with the sources of this project regarding
your rights to use or distribute this software.

NOTICE.  This Software was developed under funding from the U.S. Department of
Energy and the U.S. Government consequently retains certain rights. As such,
the U.S. Government has been granted for itself and others acting on its
behalf a paid-up, nonexclusive, irrevocable, worldwide license in the Software
to reproduce, distribute copies to the public, prepare derivative works, and
perform publicly and display publicly, and to permit other to do so.
'''

import sys
import docker
import os
import pathlib
import stat
import errno
import glob
import hashlib
import tempfile
import tarfile
import json

class ImageInfo:
    def __init__(self, registry, namespace, project, digest, tag="latest"):
        self.registry = registry
        self.namespace = namespace
        self.project = project
        self.digest = digest
        self.tag = tag

    def name(self):
        return '/'.join(filter(None,[self.registry,self.namespace,self.project])) + ":" + self.tag

_in_txn = False

def abort_txn(filesystem):
    sys.stderr.write("Aborting transaction on %s!\n" % filesystem)
    return os.system("cvmfs_server abort -f %s" % filesystem)

def start_txn(filesystem):
    global _in_txn
    if _in_txn:
        return 0
    if os.path.exists("/var/spool/cvmfs/%s/in_transaction.lock" % filesystem):
        result = abort_txn(filesystem)
        if result:
            sys.stderr.write("Failed to abort lingering transaction (exit status %d).")
            return 1
    result = os.system("cvmfs_server transaction %s" % filesystem)
    if result:
        sys.stderr.write("Transaction start failed (exit status %d); will not attempt update." % result)
        return 1
    _in_txn = True

def publish_txn(filesystem):
    global _in_txn
    if _in_txn:
        _in_txn = False
        return os.system("cvmfs_server publish %s" % filesystem)
    return 0

def create_symlink(image_dir, tag_dir):
    # check to ensure that we are in a transaction

    parent_dir = os.path.split(tag_dir)[0]

    if not os.path.exists(parent_dir):
        try:
            os.makedirs(parent_dir)
        except OSError as oe:
            if oe.errno != errno.EEXIST:
                raise

    if not os.path.exists(tag_dir):
        os.symlink(image_dir, tag_dir)
    elif os.path.islink(tag_dir):
        if os.readlink(tag_dir) != image_dir:
            os.unlink(tag_dir)
            os.symlink(image_dir, tag_dir)
    else:
        return 1

    return 0

def write_docker_image(image_dir, filesystem, image):
    # we should check to make sure that we're in a txn

    # will use a mix of Python 3.4 pathlib and old-style os module for now
    image_path = pathlib.Path(image_dir)

    status = os.system("singularity build --sandbox %s docker://%s" % (image_dir,image) )
    if os.WEXITSTATUS(status) != 0:
        return False

    # Walk the path, fixing file permissions
    for (dirpath, dirnames, filenames) in os.walk(image_dir):
        for fname in filenames:
            full_fname = os.path.join(dirpath, fname)
            st = os.lstat(full_fname)
            old_mode = stat.S_IMODE(st.st_mode)
            if (old_mode & 0o0444) == 0o0000:
                new_mode = old_mode | 0o0400
                print("Fixing mode of", full_fname, "to", oct(new_mode))
                os.chmod(full_fname, new_mode)
        for dname in dirnames:
            full_dname = os.path.join(dirpath, dname)
            st = os.lstat(full_dname)
            if not stat.S_ISDIR(st.st_mode):
                continue
            old_mode = stat.S_IMODE(st.st_mode)
            if old_mode & 0o0111 == 0:
                new_mode = old_mode | 0o0100
                print(("Fixing mode of", full_dname, "to", oct(new_mode)))
                os.chmod(full_dname, new_mode)
            if old_mode & 0o0222 == 0:
                new_mode = old_mode | 0o0200
                print(("Fixing mode of", full_dname, "to", oct(new_mode)))
                os.chmod(full_dname, new_mode)

    # turns out to be a lot easier to add bind points and
    # de-publish directories if we have write perms on root path
    os.chmod(image_dir, 0o0755)

    # if the image contains a linux operating system, then add bind points
    if list(image_path.glob('etc/*-release')):
        bindpoints = [ 'srv', 'cvmfs', 'dev', 'proc', 'sys' ]
        for bindpoint in bindpoints:
            path_to_create = image_path / bindpoint
            path_to_create.mkdir(parents=False,exist_ok=True)

    # create .cvmfscatalog file so publishing indexes each container separately
    cvmfscatalog = image_path / '.cvmfscatalog'
    cvmfscatalog.touch()

    return True

def publish_docker_image(image_info, filesystem, rootdir='',
                  username=None, token=None):

    if image_info.digest is not None:
        digest = image_info.digest
    else:
        client = docker.from_env(timeout=3600)
        image = client.images.pull(image_info.name())
        digest = image.attrs['RepoDigests'][0].split('@')[1]
        client.images.remove(image_info.name())

    hash_alg, hash = digest.split(':')

    # start transaction to trigger fs mount
    retval = start_txn(filesystem)
    if retval:
        return retval

    # a single image can have multiple tags. User-facing directories with tags
    # should be symlinks to a single copy of the image
    digest_path = os.path.join(".digests", hash_alg, hash[0:2], hash)
    image_dir = os.path.join("/cvmfs", filesystem, rootdir, image_info.namespace,
        image_info.project, digest_path)
    if not os.path.exists(image_dir):
        os.makedirs(image_dir)
        if write_docker_image(image_dir, filesystem, image_info.name()):
            tag_dir = os.path.join("/cvmfs", filesystem, rootdir,
                image_info.namespace, image_info.project, image_info.tag)
            create_symlink(digest_path, tag_dir)
            publish_txn(filesystem)
        else:
            abort_txn(filesystem)
