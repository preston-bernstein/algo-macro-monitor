#!/usr/bin/env python3
"""Read-only snapshot of the live paper-trading DB into the paper-readers share.

Run as the algo-factory user (owns the source DB). Uses SQLite's own backup
API so the copy is always transaction-consistent, even while paper-track
writes concurrently -- never touches the source with anything but a read.
"""
import grp
import os
import sqlite3

SRC = "/home/algo-factory/app/data/paper.db"
DST = "/srv/paper-share/paper.db"
DST_TMP = DST + ".tmp"
GROUP = "paper-readers"

src_conn = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
dst_conn = sqlite3.connect(DST_TMP)
with dst_conn:
    src_conn.backup(dst_conn)
src_conn.close()

# The live DB is WAL-mode; backup() copies that flag into the snapshot verbatim, which then
# needs to create/open -wal/-shm sidecar files even just to read -- and the paper-readers group
# only has read access to the share dir, not write. Converting to rollback-journal mode here
# (while we still hold the writable connection, pre-chmod) makes the snapshot a genuinely
# self-contained, lock-free file for any read-only reader downstream.
dst_conn.execute("PRAGMA journal_mode=DELETE")
dst_conn.close()

os.chmod(DST_TMP, 0o640)
os.chown(DST_TMP, -1, grp.getgrnam(GROUP).gr_gid)
os.replace(DST_TMP, DST)
