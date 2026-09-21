#!/usr/bin/env python3
"""Tests for photo-culler v0. Stdlib unittest + Pillow, nothing else.

    python test_cull.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import cull


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(str(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(root: Path):
    """Every file under root with its size and mtime - for proving nothing changed."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in filenames:
            full = os.path.join(dirpath, name)
            st = os.stat(full)
            out[os.path.relpath(full, str(root))] = (st.st_size, st.st_mtime, sha256(full))
    return out


def make_jpeg(path: Path, size=(800, 600), color=(120, 40, 40)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(str(path), "JPEG", quality=92)
    return path


def make_fake_raw(path: Path, payload: bytes = b"RAWDATA" * 512):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cull-test-"))
        self.photos = self.tmp / "shoot"
        self.data = self.tmp / "appdata"
        self.photos.mkdir()
        self.data.mkdir()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def open_manifest(self):
        lib = cull.library_dir(self.photos, self.data)
        lib.mkdir(parents=True, exist_ok=True)
        m = cull.Manifest(lib / "manifest.sqlite3")
        self.addCleanup(m.close)
        return m


class TestScanAndPairing(Base):
    def test_pairs_raw_with_matching_stem_and_reports_unpaired(self):
        make_jpeg(self.photos / "IMG_0001.JPG")
        make_fake_raw(self.photos / "IMG_0001.CR2")
        make_jpeg(self.photos / "sub" / "IMG_0002.jpeg")
        make_fake_raw(self.photos / "sub" / "IMG_0099.nef")  # no sibling JPEG
        make_fake_raw(self.photos / "IMG_0002.arw")  # different directory - not a pair

        m = self.open_manifest()
        summary = cull.scan(m, self.photos, verbose=False)

        self.assertEqual(summary["photos"], 2)
        self.assertEqual(summary["paired"], 1)
        self.assertEqual(summary["unpaired_raws"], 2)

        rows = {os.path.basename(r["path"]): r for r in m.query("SELECT * FROM photos")}
        self.assertTrue(rows["IMG_0001.JPG"]["raw_path"].endswith("IMG_0001.CR2"))
        self.assertIsNone(rows["IMG_0002.jpeg"]["raw_path"])

        unpaired = {os.path.basename(r["path"])
                    for r in m.query("SELECT path FROM unpaired_raws")}
        self.assertEqual(unpaired, {"IMG_0099.nef", "IMG_0002.arw"})

    def test_unpaired_raws_are_never_given_a_review_slot(self):
        make_jpeg(self.photos / "a.jpg")
        make_fake_raw(self.photos / "orphan.dng")
        m = self.open_manifest()
        cull.scan(m, self.photos, verbose=False)
        seqs = m.query("SELECT path FROM photos WHERE seq IS NOT NULL")
        self.assertEqual(len(seqs), 1)
        self.assertTrue(seqs[0]["path"].endswith("a.jpg"))

    def test_review_order_is_chronological_then_filename(self):
        for name, mtime in (("z_first.jpg", 1000), ("a_second.jpg", 2000),
                            ("m_third.jpg", 3000)):
            p = make_jpeg(self.photos / name)
            os.utime(str(p), (mtime, mtime))
        m = self.open_manifest()
        cull.scan(m, self.photos, verbose=False)
        order = [os.path.basename(r["path"]) for r in
                 m.query("SELECT path FROM photos WHERE seq IS NOT NULL ORDER BY seq")]
        self.assertEqual(order, ["z_first.jpg", "a_second.jpg", "m_third.jpg"])

    def test_dimensions_are_recorded_for_print_size(self):
        make_jpeg(self.photos / "big.jpg", size=(6000, 4000))
        m = self.open_manifest()
        cull.scan(m, self.photos, verbose=False)
        row = m.one("SELECT width, height FROM photos")
        self.assertEqual((row["width"], row["height"]), (6000, 4000))
        # 6000 / 300 dpi = 20.0in on the long edge
        self.assertAlmostEqual(row["width"] / 300.0, 20.0, places=2)


class TestRescan(Base):
    def test_rescan_keeps_decisions_and_slots_new_files_in(self):
        for i, mtime in ((1, 1000), (2, 2000)):
            p = make_jpeg(self.photos / ("img%d.jpg" % i))
            os.utime(str(p), (mtime, mtime))

        m = self.open_manifest()
        cull.scan(m, self.photos, verbose=False)
        first = m.one("SELECT id, path FROM photos WHERE seq=0")
        m.set_decision(first["id"], "keep")
        m.set_state("position", 1)

        # A file arrives that belongs chronologically between the two.
        p = make_jpeg(self.photos / "img3.jpg")
        os.utime(str(p), (1500, 1500))

        cull.scan(m, self.photos, verbose=False)

        order = [os.path.basename(r["path"]) for r in
                 m.query("SELECT path FROM photos WHERE seq IS NOT NULL ORDER BY seq")]
        self.assertEqual(order, ["img1.jpg", "img3.jpg", "img2.jpg"])

        kept = m.one("SELECT decision FROM photos WHERE id=?", (first["id"],))
        self.assertEqual(kept["decision"], "keep")
        self.assertIsNotNone(
            m.one("SELECT decided_at FROM photos WHERE id=?", (first["id"],))["decided_at"])
        self.assertEqual(m.get_state("position"), "1")

    def test_reopening_manifest_resumes_state(self):
        make_jpeg(self.photos / "a.jpg")
        m = self.open_manifest()
        cull.scan(m, self.photos, verbose=False)
        row = m.one("SELECT id FROM photos")
        m.set_decision(row["id"], "maybe")
        m.set_state("position", 7)
        m.close()

        again = self.open_manifest()
        cull.scan(again, self.photos, verbose=False)
        self.assertEqual(again.one("SELECT decision FROM photos")["decision"], "maybe")
        self.assertEqual(again.get_state("position"), "7")


class TestDecisions(Base):
    def test_undo_restores_previous_decision_ten_times(self):
        make_jpeg(self.photos / "a.jpg")
        m = self.open_manifest()
        cull.scan(m, self.photos, verbose=False)
        photo_id = m.one("SELECT id FROM photos")["id"]

        sequence = ["keep", "reject", "maybe", "keep", "reject",
                    "maybe", "keep", "reject", "maybe", "keep"]
        history = []
        for decision in sequence:
            previous = m.one("SELECT decision FROM photos WHERE id=?", (photo_id,))["decision"]
            history.append(previous)
            m.set_decision(photo_id, decision)

        for previous in reversed(history):
            m.set_decision(photo_id, previous)
            self.assertEqual(
                m.one("SELECT decision FROM photos WHERE id=?", (photo_id,))["decision"],
                previous)
        self.assertEqual(
            m.one("SELECT decision FROM photos WHERE id=?", (photo_id,))["decision"],
            "undecided")

    def test_invalid_decision_is_rejected(self):
        make_jpeg(self.photos / "a.jpg")
        m = self.open_manifest()
        cull.scan(m, self.photos, verbose=False)
        photo_id = m.one("SELECT id FROM photos")["id"]
        self.assertFalse(m.set_decision(photo_id, "deleted"))
        self.assertEqual(m.one("SELECT decision FROM photos")["decision"], "undecided")


class TestProxyCache(Base):
    def test_proxy_is_capped_at_1600_and_written_outside_the_photo_folder(self):
        make_jpeg(self.photos / "big.jpg", size=(4000, 3000))
        m = self.open_manifest()
        cull.scan(m, self.photos, verbose=False)
        row = m.one("SELECT * FROM photos")

        before = snapshot(self.photos)
        cache = cull.ProxyCache(self.data / "proxies")
        proxy = cache.get(row["path"], row["mtime"], row["bytes"])

        self.assertTrue(proxy.exists())
        with Image.open(str(proxy)) as im:
            self.assertEqual(max(im.size), 1600)
        self.assertNotIn(str(self.photos), str(proxy))
        self.assertEqual(snapshot(self.photos), before)

    def test_proxy_key_changes_when_the_file_changes(self):
        cache = cull.ProxyCache(self.data / "proxies")
        a = cache.key("/x/y.jpg", 100.0, 500)
        self.assertEqual(a, cache.key("/x/y.jpg", 100.0, 500))
        self.assertNotEqual(a, cache.key("/x/y.jpg", 101.0, 500))
        self.assertNotEqual(a, cache.key("/x/y.jpg", 100.0, 501))
        self.assertNotEqual(a, cache.key("/x/z.jpg", 100.0, 500))


class TestExport(Base):
    def setUp(self):
        super().setUp()
        make_jpeg(self.photos / "keep1.jpg", size=(1200, 800), color=(10, 90, 10))
        make_fake_raw(self.photos / "keep1.cr2")
        make_jpeg(self.photos / "keep2.jpg", size=(900, 1200), color=(10, 10, 90))
        make_jpeg(self.photos / "maybe1.jpg", size=(640, 480), color=(90, 90, 10))
        make_fake_raw(self.photos / "maybe1.nef")
        make_jpeg(self.photos / "reject1.jpg", size=(640, 480), color=(90, 10, 10))
        make_fake_raw(self.photos / "orphan.arw")

        self.m = self.open_manifest()
        cull.scan(self.m, self.photos, verbose=False)
        for name, decision in (("keep1.jpg", "keep"), ("keep2.jpg", "keep"),
                               ("maybe1.jpg", "maybe"), ("reject1.jpg", "reject")):
            row = self.m.one("SELECT id FROM photos WHERE path LIKE ?", ("%" + name,))
            self.m.set_decision(row["id"], decision)
        self.dest = self.tmp / "album-selects"

    def test_exported_files_are_byte_identical(self):
        report = cull.export(self.m, self.photos, self.dest)
        self.assertEqual(report["counts"]["files_copied"], 3)
        self.assertEqual(report["counts"]["raws_copied"], 2)

        checked = 0
        for record in report["decisions"]:
            if not record["exported_to"]:
                continue
            self.assertEqual(sha256(record["path"]), sha256(record["exported_to"]))
            self.assertEqual(os.path.getsize(record["path"]),
                             os.path.getsize(record["exported_to"]))
            # copy2 preserves timestamps
            self.assertAlmostEqual(os.path.getmtime(record["path"]),
                                   os.path.getmtime(record["exported_to"]), places=4)
            checked += 1
            if record["raw_exported_to"]:
                self.assertEqual(sha256(record["raw_path"]),
                                 sha256(record["raw_exported_to"]))
                checked += 1
        self.assertEqual(checked, 5)

    def test_source_folder_is_untouched_by_a_full_session(self):
        before = snapshot(self.photos)

        cache = cull.ProxyCache(self.data / "proxies")
        for row in self.m.query("SELECT * FROM photos"):
            cache.get(row["path"], row["mtime"], row["bytes"])
        cull.export(self.m, self.photos, self.dest)

        after = snapshot(self.photos)
        self.assertEqual(sorted(before), sorted(after), "file set changed")
        self.assertEqual(before, after, "sizes, mtimes or contents changed")

    def test_rejects_are_not_exported_and_nothing_is_deleted(self):
        cull.export(self.m, self.photos, self.dest)
        exported = {p.name for p in (self.dest / "keep").glob("*.jpg")}
        exported |= {p.name for p in (self.dest / "maybe").glob("*.jpg")}
        self.assertEqual(exported, {"keep1.jpg", "keep2.jpg", "maybe1.jpg"})
        self.assertFalse((self.dest / "reject").exists())
        self.assertTrue((self.photos / "reject1.jpg").exists())

    def test_raws_land_beside_their_bucket(self):
        cull.export(self.m, self.photos, self.dest)
        self.assertTrue((self.dest / "keep" / "raw" / "keep1.cr2").exists())
        self.assertTrue((self.dest / "maybe" / "raw" / "maybe1.nef").exists())

    def test_collision_suffixes_rather_than_overwrites(self):
        (self.dest / "keep").mkdir(parents=True)
        decoy = self.dest / "keep" / "keep1.jpg"
        decoy.write_bytes(b"do not overwrite me")

        cull.export(self.m, self.photos, self.dest)

        self.assertEqual(decoy.read_bytes(), b"do not overwrite me")
        self.assertTrue((self.dest / "keep" / "keep1_1.jpg").exists())
        self.assertEqual(sha256(self.photos / "keep1.jpg"),
                         sha256(self.dest / "keep" / "keep1_1.jpg"))

    def test_report_lists_every_decision_and_unpaired_raws(self):
        cull.export(self.m, self.photos, self.dest)
        report = json.loads((self.dest / "export-report.json").read_text())

        self.assertEqual(report["counts"], {
            "keep": 2, "maybe": 1, "reject": 1, "undecided": 0,
            "files_copied": 3, "raws_copied": 2, "failures": 0, "unpaired_raws": 1,
        })
        self.assertEqual(len(report["decisions"]), 4)
        self.assertEqual([os.path.basename(r["path"]) for r in report["unpaired_raws"]],
                         ["orphan.arw"])
        self.assertEqual(report["failures"], [])

    def test_export_into_the_photo_folder_is_refused(self):
        with self.assertRaises(ValueError):
            cull.export(self.m, self.photos, self.photos / "selects")
        self.assertFalse((self.photos / "selects").exists())


class TestLocations(Base):
    def test_library_is_outside_the_photo_folder_and_stable_per_folder(self):
        lib = cull.library_dir(self.photos, self.data)
        self.assertNotIn(str(self.photos), str(lib))
        self.assertEqual(lib, cull.library_dir(self.photos, self.data))
        self.assertNotEqual(lib, cull.library_dir(self.photos.parent / "other", self.data))

    def test_app_data_dir_is_not_the_project_folder(self):
        self.assertNotEqual(cull.app_data_dir(), cull.PROJECT_DIR)


if __name__ == "__main__":
    unittest.main(verbosity=2)
