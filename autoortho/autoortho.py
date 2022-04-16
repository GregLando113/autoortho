#!/usr/bin/env python

from __future__ import with_statement

import os
import re
import sys
import time
import math
import errno
import random
import psutil
import pathlib
import threading
import itertools

import logging
logging.basicConfig()
log = logging.getLogger('log')
log.setLevel(logging.INFO)


from fuse import FUSE, FuseOSError, Operations, fuse_get_context

import getortho

from xp_udp import DecodePacket, RequestDataRefs
import socket


def deg2num(lat_deg, lon_deg, zoom):
  lat_rad = math.radians(lat_deg)
  n = 2.0 ** zoom
  xtile = int((lon_deg + 180.0) / 360.0 * n)
  ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
  return (xtile, ytile)


def tilemeters(lat_deg, zoom):
    y = 64120000 * math.cos(math.radians(lat_deg)) / (pow(2, zoom))
    x = 64120000 / (pow(2, zoom))
    return (x, y)


class TileCacher(object):
    min_zoom = 13
    max_zoom = 18

    tiles = {}
    tile_lock = threading.Lock()

    def clean(self):
        memlimit = pow(2,30) * 4
        log.info(f"Started tile clean thread.  Mem limit {memlimit}")
        while True:
            process = psutil.Process(os.getpid())
            cur_mem = process.memory_info().rss
            log.info(f"NUM TILES CACHED: {len(self.tiles)}.  TOTAL MEM: {cur_mem//1048576} MB")
            while len(self.tiles) >= 200 and cur_mem > memlimit:
                log.info("Hit cache limit.  Remove oldest 100")
                with self.tile_lock:
                    for i in list(self.tiles.keys())[:100]:
                        t = self.tiles.pop(i)
                        #t.close()
                        del(t)
                cur_mem = process.memory_info().rss
            time.sleep(30)

    def __init__(self, cache_dir='.cache'):
        self.cache_dir = cache_dir
        self.clean_t = threading.Thread(target=self.clean, daemon=True)
        self.clean_t.start()
        #self.map = getortho.Map(cache_dir=self.cache_dir)

        #self.go = getortho.GetOrtho(chunk_threads=32, tile_threads=6)
        #if not os.path.exists(self.cache_dir):
        #    log.info("Creating cache dir.")
        #    os.makedirs(self.cache_dir)

    def _get_tile(self, row, col, map_type, zoom):
        idx = f"{row}_{col}_{map_type}_{zoom}"
        with self.tile_lock:
            tile = self.tiles.get(idx)
        if not tile:
            tile = getortho.Tile(col, row, map_type, zoom, cache_dir =
                    self.cache_dir)
            with self.tile_lock:
                self.tiles[idx] = tile
        return tile


class AutoOrtho(Operations):

    open_paths = []
    path_condition = threading.Condition()
    path_dict = {}
    tile_dict = {}


    def __init__(self, root, cache_dir='.cache'):
        log.info(f"ROOT: {root}")
        self.dds_re = re.compile(".*/(\d+)[-_](\d+)[-_]((?!ZL)\D*)(\d+).dds")
        self.dsf_re = re.compile(".*/\+\d+[-+]\d+.dsf")
        self.root = root
        self.cache_dir = cache_dir
    
        #self._start_reset()
        #self.go = getortho.GetOrtho()
        self.tc = TileCacher(cache_dir)
        #self.dsf_parser = DSF(self.tc) 
        #self.ff = FlightFollower()
        #self.background_tc = TileCacher()

    # Helpers
    # =======

    def _full_path(self, partial):
        if partial.startswith("/"):
            partial = partial[1:]
        path = os.path.join(self.root, partial)
        return path


    # Filesystem methods
    # ==================

    def access(self, path, mode):
        #log.debug(f"ACCESS: {path}")
        #m = re.match(".*/(\d+)[-_](\d+)[-_](\D*)(\d+).dds", path)
        #if m:
        #    log.info(f"ACCESS: Found DDS file {path}: %s " % str(m.groups()))
        full_path = self._full_path(path)
        if not os.access(full_path, mode):
            raise FuseOSError(errno.EACCES)

    def chmod(self, path, mode):
        full_path = self._full_path(path)
        return os.chmod(full_path, mode)

    def chown(self, path, uid, gid):
        full_path = self._full_path(path)
        return os.chown(full_path, uid, gid)


    def getattr(self, path, fh=None):
        log.debug(f"GETATTR {path}")

        full_path = None
        m = self.dds_re.match(path)
        if m:
            #log.info(f"{path}: MATCH!")
            row, col, maptype, zoom = m.groups()
            log.debug(f"GETATTR: Fetch for {path}: %s" % str(m.groups()))
            attrs = {
                'st_atime': 1649857250.382081, 
                'st_ctime': 1649857251.726115, 
                'st_gid': 1000, 
                'st_mode': 33204,
                'st_mtime': 1649857251.726115, 
                'st_nlink': 1, 
                'st_size': 22369744, 
                'st_uid': 1000, 
                'st_blksize': 16384
            }

        else:
            full_path = self._full_path(path)

        #log.info(f"GETATTR: FH: {fh}")
            st = os.lstat(full_path)
     
            attrs = dict((key, getattr(st, key)) for key in ('st_atime', 'st_ctime',
                        'st_gid', 'st_mode', 'st_mtime', 'st_nlink', 'st_size', 'st_uid'))


        return attrs

    def readdir(self, path, fh):
        log.debug(f"READDIR: {path}")
        full_path = self._full_path(path)

        dirents = ['.', '..']
        if os.path.isdir(full_path):
            dirents.extend(os.listdir(full_path))
        for r in dirents:
            yield r

    def readlink(self, path):
        pathname = os.readlink(self._full_path(path))
        if pathname.startswith("/"):
            # Path name is absolute, sanitize it.
            return os.path.relpath(pathname, self.root)
        else:
            return pathname

    def mknod(self, path, mode, dev):
        return os.mknod(self._full_path(path), mode, dev)

    def rmdir(self, path):
        full_path = self._full_path(path)
        return os.rmdir(full_path)

    def mkdir(self, path, mode):
        return os.mkdir(self._full_path(path), mode)

    def statfs(self, path):
        #log.debug(f"STATFS: {path}")
        full_path = self._full_path(path)
        stv = os.statvfs(full_path)
        return dict((key, getattr(stv, key)) for key in ('f_bavail', 'f_bfree',
            'f_blocks', 'f_bsize', 'f_favail', 'f_ffree', 'f_files', 'f_flag',
            'f_frsize', 'f_namemax'))

    def unlink(self, path):
        return os.unlink(self._full_path(path))

    def symlink(self, name, target):
        return os.symlink(target, self._full_path(name))

    def rename(self, old, new):
        return os.rename(self._full_path(old), self._full_path(new))

    def link(self, target, name):
        return os.link(self._full_path(name), self._full_path(target))

    def utimens(self, path, times=None):
        return os.utime(self._full_path(path), times)

    # File methods
    # ============


    def open(self, path, flags):
        h = 0

        log.debug(f"OPEN: {path}, {flags}")
        full_path = self._full_path(path)
        log.debug(f"OPEN: FULL PATH: {full_path}")

        m = self.dds_re.match(path)
        if m:
            with self.path_condition:
                while path in self.open_paths:
                    log.info(f"{path} already open.  wait.")
                    self.path_condition.wait()

                log.info(f"Opening for {path}....")
                self.open_paths.append(path)

        else:
            h = os.open(full_path, flags)

        return h

    def _create(self, path, mode, fi=None):
        uid, gid, pid = fuse_get_context()
        full_path = self._full_path(path)
        fd = os.open(full_path, os.O_WRONLY | os.O_CREAT, mode)
        os.chown(full_path,uid,gid) #chown to context uid & gid
        return fd

    def read(self, path, length, offset, fh):
        log.debug(f"READ: {path}")
        data = None
        m = self.dds_re.match(path)
        if m:
            row, col, maptype, zoom = m.groups()
            row = int(row)
            col = int(col)
            zoom = int(zoom)
            log.debug(f"READ: DDS file {path}, offset {offset}, length {length} (%s) " % str(m.groups()))
            t = self.tc._get_tile(row, col, maptype, zoom) 

            if offset == 0:
                log.debug("HEADER")
                #header = os.read(fh, 128)
                #data = header + b'\0'*(length-128)
                
                # Inefficient.  Will re compress each time!
                t.get_bytes(length)
            else:
                log.debug("DATA")
                curpos = t.dds.tell()
                if offset > curpos:
                    log.debug(f"Increasing position from {curpos} to {offset}") 
                    # Find mipmap
                    
                    mipmap_idx = 4
                    for idx in range(1,5):
                        mipmap = t.dds.mipmap_list[idx]
                        if offset <= mipmap.startpos < (offset + length):
                            log.debug(f"Mipmap {idx} is in range. {offset} >= {mipmap.startpos} < {(offset + length)}")
                            mipmap_idx = idx
                            break

                    if not mipmap.retrieved:
                        log.info(f"Will retrieve MIPMAP {mipmap_idx}") 
                        t.get_mipmap(mipmap_idx)
                else:
                    log.debug("Retrieving sequential bytes")
                    mipmap = t.dds.mipmap_list[0]
                    if not mipmap.retrieved and offset < t.dds.mipmap_list[1].startpos:
                        log.debug(f"{offset}, {t.dds.mipmap_list[1]}")
                        log.debug("Still within mipmap 0...")
                        mipmap_idx = 0
                        log.info(f"Will retrieve MIPMAP {mipmap_idx}") 
                        t.get_mipmap(mipmap_idx)
                
            
            t.dds.seek(offset)
            data = t.dds.read(length)

        if not data:
            os.lseek(fh, offset, os.SEEK_SET)
            data = os.read(fh, length)
        #return os.read(fh, length)
        return data

    def _write(self, path, buf, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.write(fh, buf)

    def truncate(self, path, length, fh=None):
        log.info(f"TRUNCATE")
        full_path = self._full_path(path)
        with open(full_path, 'r+') as f:
            f.truncate(length)

    def _flush(self, path, fh):
        log.info(f"FLUSH")
        m = self.dds_re.match(path)
        if m:
            log.info(f"RELEASE: {path}")
            return 0
        else:
            return os.fsync(fh)

    def release(self, path, fh):
        m = self.dds_re.match(path)
        if m:
            log.info(f"RELEASE: {path}")
            with self.path_condition:
                if path in self.open_paths:
                    log.debug(f"RELEASE: {path}")
                    self.open_paths.remove(path)
                    self.path_condition.notify_all()
            return 0
        else:
            return os.close(fh)

    def fsync(self, path, fdatasync, fh):
        log.info(f"FSYNC")
        return self.flush(path, fh)


def main(mountpoint, root):
    FUSE(AutoOrtho(root), mountpoint, nothreads=False, foreground=True, allow_other=True)


if __name__ == '__main__':
    main(sys.argv[2], sys.argv[1])
