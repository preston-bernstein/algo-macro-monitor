#!/usr/bin/env python3
"""Read-only snapshot of a live paper-trading DB into a group-readable share.

One worked example of the "periodic transaction-consistent snapshot" read path this repo's
README describes -- adapt paths/user/group to your own deployment. Run as the user that owns the
source DB. Uses SQLite's own backup API so the copy is always transaction-consistent, even while
the source is written concurrently -- this script never touches the source with anything but a
read.

Configure via env vars (defaults shown are generic, not tied to any real deployment):
  MACRO_MONITOR_SOURCE_DB       source paper.db path      (default: /var/lib/paper-trading/paper.db)
  MACRO_MONITOR_SNAPSHOT_PATH   destination snapshot path (default: /var/lib/macro-monitor/paper.db)
  MACRO_MONITOR_SNAPSHOT_GROUP  group granted read access (default: paper-readers)
"""
import grp
import os
import sqlite3

SRC = os.environ.get("MACRO_MONITOR_SOURCE_DB", "/var/lib/paper-trading/paper.db")
DST = os.environ.get("MACRO_MONITOR_SNAPSHOT_PATH", "/var/lib/macro-monitor/paper.db")
DST_TMP = DST + ".tmp"
GROUP = os.environ.get("MACRO_MONITOR_SNAPSHOT_GROUP", "paper-readers")

os.makedirs(os.path.dirname(DST_TMP), exist_ok=True)

src_conn = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
dst_conn = sqlite3.connect(DST_TMP)
with dst_conn:
    src_conn.backup(dst_conn)
src_conn.close()

# The live DB may be WAL-mode; backup() copies that flag into the snapshot verbatim, which then
# needs to create/open -wal/-shm sidecar files even just to read -- and the read-only group only
# has read access to the share dir, not write. Converting to rollback-journal mode here (while we
# still hold the writable connection, pre-chmod) makes the snapshot a genuinely self-contained,
# lock-free file for any read-only reader downstream.
dst_conn.execute("PRAGMA journal_mode=DELETE")
dst_conn.close()

os.chmod(DST_TMP, 0o640)
os.chown(DST_TMP, -1, grp.getgrnam(GROUP).gr_gid)
os.replace(DST_TMP, DST)
