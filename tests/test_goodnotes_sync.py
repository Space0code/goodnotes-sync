import datetime as dt
import hashlib
import os
import sys
import tempfile
import time
import unittest
from zoneinfo import ZoneInfo

os.environ["GOODNOTES_SYNC_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import goodnotes_sync as G  # noqa: E402

COURSES = {
    "reinforcement_learning": {"prefix": "RL", "goodnotes_folders": ["Reinforcement learning"]},
    "symbolic_reasoning": {"prefix": "SR"},
    "autonomous_systems": {"prefix": "AS"},
    "spanish": {"prefix": "SPA", "exclude": True},
}


def read(path):
    with open(path, "rb") as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.materials = os.path.join(self.tmp.name, "materials")
        os.makedirs(self.materials)
        self.patch(MATERIALS=self.materials, COURSES=COURSES, STATE=os.path.join(self.tmp.name, "state.json"),
                   STAGING=os.path.join(self.tmp.name, "staging"), NOTIFY=False, COMPRESS=False)

    def tearDown(self):
        self.tmp.cleanup()

    def patch(self, **kw):
        for k, v in kw.items():
            old = getattr(G, k)
            setattr(G, k, v)
            self.addCleanup(setattr, G, k, old)

    def write(self, rel, data=b"%PDF-1.4 x", age=600):
        p = os.path.join(self.materials, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        t = time.time() - age
        os.utime(p, (t, t))
        return p


class Naming(unittest.TestCase):
    def setUp(self):
        G.COURSES, self._old = COURSES, G.COURSES

    def tearDown(self):
        G.COURSES = self._old

    def test_display_stem(self):
        self.assertEqual(G.display_stem("S04SR_auto.pdf"), "S04SR")
        self.assertEqual(G.display_stem("L3_auto (2).pdf"), "L3 (2)")
        self.assertEqual(G.display_stem("2.Reglas ortograficas_explicacion_auto.ppsx"),
                         "2.Reglas ortograficas_explicacion")
        self.assertEqual(G.display_stem("my_automata.pdf"), "my_automata")

    def test_goodnotes_name(self):
        self.assertEqual(G.goodnotes_name("reinforcement_learning", "Lecture05_auto.pdf"), "RL · Lecture05")
        self.assertTrue(G.goodnotes_name("symbolic_reasoning", "S04SR_auto.pdf", updated=True)
                        .startswith("SR · S04SR (updated "))
        self.assertEqual(G.goodnotes_name("symbolic_reasoning", "a/b:c_auto.pdf"), "SR · a_b_c")

    def test_default_prefix(self):
        self.assertEqual(G.course_prefix("introduction_to_machine_learning"), "ITML")

    def test_map_by_prefix(self):
        self.assertEqual(G.map_remote("RL · Lecture05.pdf"), ("reinforcement_learning", ["Lecture05"]))
        self.assertEqual(G.map_remote("Home/Inbox/SR - S04SR.pdf"), ("symbolic_reasoning", ["S04SR"]))
        self.assertEqual(G.map_remote("AS: Theory 1.pdf"), ("autonomous_systems", ["Theory 1"]))

    def test_emailed_import_double_extension(self):
        self.assertEqual(G.map_remote("SR · Lectura_practica_soluciones.pdf.pdf"),
                         ("symbolic_reasoning", ["Lectura_practica_soluciones"]))
        self.assertEqual(G.map_remote("EMAI/Symbolic reasoning/SR · Soluciones (2).pdf.pdf"),
                         ("symbolic_reasoning", ["Soluciones (2)"]))
        self.assertEqual(G.map_remote("RL · v1.2 notes.pdf"), ("reinforcement_learning", ["v1.2 notes"]))

    def test_folder_wins_and_keeps_subfolders(self):
        self.assertEqual(G.map_remote("EMAI/Reinforcement learning/Lectures/RL · L5.pdf"),
                         ("reinforcement_learning", ["Lectures", "L5"]))
        self.assertEqual(G.map_remote("EMAI/Symbolic reasoning/SR · S04SR.pdf"), ("symbolic_reasoning", ["S04SR"]))
        self.assertEqual(G.map_remote("EMAI/Reinforcement learning/SR · misfiled.pdf"),
                         ("reinforcement_learning", ["misfiled"]))
        self.assertIsNone(G.map_remote("EMAI/Notes.pdf"))

    def test_map_by_folder(self):
        self.assertEqual(G.map_remote("Master/Reinforcement Learning/Week 3/Notes.pdf"),
                         ("reinforcement_learning", ["Week 3", "Notes"]))
        self.assertEqual(G.map_remote("symbolic_reasoning/Proofs.pdf"), ("symbolic_reasoning", ["Proofs"]))
        self.assertEqual(G.map_remote("RL/Lecture 7.pdf"), ("reinforcement_learning", ["Lecture 7"]))
        self.assertEqual(G.map_remote("RL/RL · Lecture 8.pdf"), ("reinforcement_learning", ["Lecture 8"]))

    def test_unmapped_and_excluded(self):
        self.assertIsNone(G.map_remote("Personal/Diary.pdf"))
        self.assertIsNone(G.map_remote("RLX notes.pdf"))
        self.assertIsNone(G.map_remote("SPA · Tema 1.pdf"))  # excluded course
        self.assertIsNone(G.map_remote("rl · lowercase prefix.pdf"))  # prefixes are case-sensitive


class Dest(Base):
    def test_dest(self):
        d = G.handwritten_dest("reinforcement_learning", ["Week 3", "Notes"])
        self.assertEqual(d, os.path.join(self.materials, "reinforcement_learning/handwritten/Week 3/Notes.pdf"))

    def test_no_escape(self):
        with self.assertRaises(ValueError):
            G.handwritten_dest("reinforcement_learning", ["..", "..", "slides", "L1"])
        # clean_name neutralises '..' segments coming from Goodnotes paths
        course, parts = G.map_remote("RL/../../x/RL · ...pdf") or (None, None)
        self.assertEqual(course, "reinforcement_learning")
        self.assertTrue(G.handwritten_dest(course, parts).startswith(
            os.path.join(self.materials, "reinforcement_learning", "handwritten") + os.sep))


class Imports(Base):
    def test_candidates(self):
        self.write("reinforcement_learning/lectures/L1_auto.pdf")
        self.write("reinforcement_learning/lectures/L1.pdf")             # your own copy: skipped
        self.write("reinforcement_learning/slides/deck_auto.pptx")
        self.write("reinforcement_learning/exercises/sub/ex_auto.pdf")
        self.write("reinforcement_learning/code/nb_auto.ipynb")
        self.write("reinforcement_learning/readings/paper_auto.pdf")      # not an import folder
        self.write("reinforcement_learning/handwritten/x_auto.pdf")
        self.write("spanish/slides/T1_auto.pdf")                          # excluded course
        rels = sorted(r for _, r, _, _ in G.candidates())
        self.assertEqual(rels, ["reinforcement_learning/exercises/sub/ex_auto.pdf",
                                "reinforcement_learning/lectures/L1_auto.pdf",
                                "reinforcement_learning/slides/deck_auto.pptx"])

    def test_unreadable_folder_is_an_error_not_an_empty_baseline(self):
        self.write("symbolic_reasoning/lectures/S01_auto.pdf")
        locked = os.path.join(self.materials, "symbolic_reasoning/lectures")
        os.chmod(locked, 0)
        state = {}
        try:
            with self.assertRaises(PermissionError):
                G.pending_imports(state)
        finally:
            os.chmod(locked, 0o755)
        self.assertNotIn("baseline_done", state)

    def test_baseline_then_new(self):
        self.write("symbolic_reasoning/lectures/S01_auto.pdf")
        state = {}
        self.assertEqual(G.pending_imports(state), [])
        self.assertEqual(state["imports"]["symbolic_reasoning/lectures/S01_auto.pdf"]["status"], "baseline")
        self.write("symbolic_reasoning/lectures/S02_auto.pdf")
        pend = G.pending_imports(state)
        self.assertEqual([p[1] for p in pend], ["symbolic_reasoning/lectures/S02_auto.pdf"])
        self.assertFalse(pend[0][4])

    def test_baseline_recorded_before_email_is_set(self):
        sent = []
        self.patch(GOODNOTES_EMAIL="", deliver=lambda path, name, rel: sent.append(name) or True)
        state = {}
        self.write("symbolic_reasoning/lectures/S01_auto.pdf")
        G.import_new(state)
        self.write("symbolic_reasoning/lectures/S02_auto.pdf")
        G.import_new(state)
        self.assertEqual(sent, [])
        self.patch(GOODNOTES_EMAIL="x@goodnotes.email")
        G.import_new(state)
        self.assertEqual(sent, ["SR · S02"])

    def test_import_since_cutoff_on_first_run(self):
        self.write("symbolic_reasoning/lectures/old_auto.pdf", age=3 * 3600)
        self.write("symbolic_reasoning/lectures/new_auto.pdf", age=600)
        since = dt.datetime.fromtimestamp(time.time() - 3600).isoformat(timespec="minutes")
        self.patch(IMPORT_SINCE=since)
        state = {}
        pend = G.pending_imports(state)
        self.assertEqual([p[1] for p in pend], ["symbolic_reasoning/lectures/new_auto.pdf"])
        self.assertEqual(state["imports"]["symbolic_reasoning/lectures/old_auto.pdf"]["status"], "baseline")

    def test_fresh_file_waits(self):
        state = {"baseline_done": 1}
        self.write("symbolic_reasoning/lectures/S03_auto.pdf", age=5)
        self.assertEqual(G.pending_imports(state), [])

    def test_import_flow_and_updates(self):
        sent = []
        self.patch(GOODNOTES_EMAIL="x@goodnotes.email",
                   deliver=lambda path, name, rel: sent.append(name) or True)
        state = {"baseline_done": 1}
        p = self.write("reinforcement_learning/lectures/L5_auto.pdf")
        G.import_new(state)
        self.assertEqual(sent, ["RL · L5"])
        G.import_new(state)
        self.assertEqual(sent, ["RL · L5"])                               # nothing new
        self.write("reinforcement_learning/lectures/L5_auto.pdf", data=b"%PDF-1.4 v2", age=300)
        G.import_new(state)
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[1].startswith("RL · L5 (updated "))
        os.utime(p, (time.time() - 200, time.time() - 200))               # touched, same content
        G.import_new(state)
        self.assertEqual(len(sent), 2)

    def test_updated_baseline_file(self):
        state = {}
        self.write("symbolic_reasoning/lectures/S01_auto.pdf")
        G.pending_imports(state)
        self.write("symbolic_reasoning/lectures/S01_auto.pdf", data=b"%PDF new version", age=300)
        pend = G.pending_imports(state)
        self.assertEqual(len(pend), 1)
        self.assertTrue(pend[0][4])

    def test_failure_retries_with_backoff(self):
        calls = []

        def boom(path, name, rel):
            calls.append(name)
            raise OSError("smtp down")
        self.patch(GOODNOTES_EMAIL="x@goodnotes.email", deliver=boom, alert=lambda *a: None)
        state = {"baseline_done": 1}
        self.write("autonomous_systems/slides/T1_auto.pdf")
        G.import_new(state)
        G.import_new(state)                                               # within backoff: not retried
        self.assertEqual(len(calls), 1)
        rec = state["imports"]["autonomous_systems/slides/T1_auto.pdf"]
        self.assertEqual(rec["status"], "failed")
        rec["retry_at"] = 0
        G.import_new(state)
        self.assertEqual(len(calls), 2)

    def test_backfill_course(self):
        self.patch(GOODNOTES_EMAIL="x@goodnotes.email")
        state = {}
        self.write("symbolic_reasoning/lectures/S01_auto.pdf")
        self.write("autonomous_systems/slides/T1_auto.pdf")
        G.pending_imports(state)
        pend = G.pending_imports(state, only_course="symbolic_reasoning")
        self.assertEqual([p[1] for p in pend], ["symbolic_reasoning/lectures/S01_auto.pdf"])


class Exports(Base):
    def entry(self, path, data, mod="2026-10-22T10:00:00.000Z"):
        return {"Path": path, "Size": len(data), "ModTime": mod, "Hashes": {"md5": hashlib.md5(data).hexdigest()}}

    def run_export(self, state, remote):
        def fetch(path, tmp):
            with open(tmp, "wb") as f:
                f.write(remote[path])
        self.patch(remote_listing=lambda: [self.entry(p, d) for p, d in remote.items()], fetch=fetch)
        return G.export_changed(state)

    def test_export_only_changed_and_overwrites(self):
        state = {}
        remote = {"RL · L5.pdf": b"v1", "Personal/Diary.pdf": b"secret"}
        self.assertEqual(self.run_export(state, remote), ["reinforcement_learning/handwritten/L5.pdf"])
        dest = os.path.join(self.materials, "reinforcement_learning/handwritten/L5.pdf")
        self.assertEqual(read(dest), b"v1")
        self.assertEqual(self.run_export(state, remote), [])               # unchanged: nothing downloaded
        remote["RL · L5.pdf"] = b"v2 with more ink"
        self.assertEqual(self.run_export(state, remote), ["reinforcement_learning/handwritten/L5.pdf"])
        self.assertEqual(read(dest), b"v2 with more ink")    # same file, replaced
        self.assertEqual(os.listdir(os.path.dirname(dest)), ["L5.pdf"])

    def test_never_overwrites_foreign_or_edited_file(self):
        mine = self.write("reinforcement_learning/handwritten/L6.pdf", data=b"my own file")
        state = {}
        out = self.run_export(state, {"RL · L6.pdf": b"goodnotes"})
        self.assertEqual(out, ["reinforcement_learning/handwritten/L6 (2).pdf"])
        self.assertEqual(read(mine), b"my own file")
        self.assertEqual(self.run_export(state, {"RL · L6.pdf": b"goodnotes"}), [])   # sibling is stable
        out = self.run_export(state, {"RL · L6.pdf": b"goodnotes v2"})
        self.assertEqual(out, ["reinforcement_learning/handwritten/L6 (2).pdf"])

    def test_compressed_export_is_recorded_as_written(self):
        def squeeze(src, workdir, profile=None, name="c.pdf"):
            dst = os.path.join(workdir, name)
            with open(dst, "wb") as f:
                f.write(read(src)[:3])
            return dst
        self.patch(COMPRESS=True, compress_pdf=squeeze)
        state = {}
        remote = {"RL · L9.pdf": b"big notebook"}
        self.assertEqual(self.run_export(state, remote), ["reinforcement_learning/handwritten/L9.pdf"])
        dest = os.path.join(self.materials, "reinforcement_learning/handwritten/L9.pdf")
        self.assertEqual(read(dest), b"big")
        rec = state["exports"]["RL · L9.pdf"]
        self.assertEqual(rec["md5"], hashlib.md5(b"big").hexdigest())
        self.assertEqual(rec["fingerprint"], hashlib.md5(b"big notebook").hexdigest())
        self.assertEqual(self.run_export(state, remote), [])               # unchanged remote: no re-download
        remote["RL · L9.pdf"] = b"bigger notebook"
        self.assertEqual(self.run_export(state, remote), ["reinforcement_learning/handwritten/L9.pdf"])

    def handwritten_files(self, course="reinforcement_learning"):
        root = os.path.join(self.materials, course, "handwritten")
        return sorted(os.path.relpath(os.path.join(dp, f), root) for dp, _, fns in os.walk(root) for f in fns)

    def test_moved_into_course_folder_same_place(self):
        state = {}
        self.run_export(state, {"RL · L7.pdf": b"v1"})
        self.assertEqual(self.run_export(state, {"EMAI/Reinforcement learning/RL · L7.pdf": b"v1"}),
                         ["reinforcement_learning/handwritten/L7.pdf"])
        self.assertEqual(self.handwritten_files(), ["L7.pdf"])
        self.assertEqual(list(state["exports"]), ["EMAI/Reinforcement learning/RL · L7.pdf"])
        self.assertEqual(self.run_export(state, {"EMAI/Reinforcement learning/RL · L7.pdf": b"v1"}), [])

    def test_moved_into_subfolder_unchanged_is_moved_not_copied(self):
        state = {}
        self.run_export(state, {"RL · L8.pdf": b"v1"})
        out = self.run_export(state, {"EMAI/Reinforcement learning/Lectures/RL · L8.pdf": b"v1"})
        self.assertEqual(out, ["reinforcement_learning/handwritten/Lectures/L8.pdf"])
        self.assertEqual(self.handwritten_files(), ["Lectures/L8.pdf"])

    def test_moved_and_edited_replaces_old_copy(self):
        state = {}
        self.run_export(state, {"RL · L9.pdf": b"v1"})
        out = self.run_export(state, {"EMAI/Reinforcement learning/Lectures/RL · L9.pdf": b"v2 more ink"})
        self.assertEqual(out, ["reinforcement_learning/handwritten/Lectures/L9.pdf"])
        self.assertEqual(self.handwritten_files(), ["Lectures/L9.pdf"])
        self.assertEqual(read(os.path.join(self.materials, "reinforcement_learning/handwritten/Lectures/L9.pdf")),
                         b"v2 more ink")

    def test_moved_but_old_copy_edited_is_kept(self):
        state = {}
        self.run_export(state, {"RL · L10.pdf": b"v1"})
        self.write("reinforcement_learning/handwritten/L10.pdf", data=b"I annotated this on the Mac")
        self.run_export(state, {"EMAI/Reinforcement learning/Lectures/RL · L10.pdf": b"v1"})
        self.assertEqual(self.handwritten_files(), ["L10.pdf", "Lectures/L10.pdf"])

    def test_never_touches_originals(self):
        orig = self.write("reinforcement_learning/lectures/L8_auto.pdf", data=b"aula original")
        state = {}
        self.run_export(state, {"RL · L8_auto.pdf": b"ink", "RL/lectures/L8_auto.pdf": b"ink2"})
        self.assertEqual(read(orig), b"aula original")
        for dp, _, fns in os.walk(self.materials):
            for fn in fns:
                p = os.path.join(dp, fn)
                if p != orig:
                    self.assertIn(os.sep + "handwritten" + os.sep, p)


class LocalDriveBackup(Base):
    """Reading Goodnotes Auto Backup from Google Drive for desktop's folder, without patched listing/fetch."""

    def setUp(self):
        super().setUp()
        self.cloud = os.path.join(self.tmp.name, "CloudStorage")
        os.makedirs(self.cloud)
        self.patch(CLOUD_STORAGE=self.cloud, BACKUP_FOLDER="")

    def backup(self, rel, data, mtime=1_800_000_000):
        p = os.path.join(self.cloud, "GoogleDrive-me@gmail.com", "Moja mapa", "Goodnotes", rel)  # localised My Drive
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        os.utime(p, (mtime, mtime))
        return p

    def test_not_set_up(self):
        with self.assertRaises(G.BackupUnavailable):
            G.backup_folder()

    def test_signed_in_but_nothing_backed_up(self):
        os.makedirs(os.path.join(self.cloud, "GoogleDrive-me@gmail.com", "My Drive"))
        self.assertIsNone(G.backup_folder())
        self.assertEqual(G.remote_listing(), [])

    def test_listing_and_export(self):
        self.backup("EMAI/Reinforcement learning/Lectures/RL · L5.pdf", b"ink v1")
        self.backup("EMAI/Reinforcement learning/Lectures/RL · L5.goodnotes", b"not a pdf")
        self.backup("Personal/Diary.pdf", b"private")
        listing = G.remote_listing()
        self.assertEqual(sorted(e["Path"] for e in listing),
                         ["EMAI/Reinforcement learning/Lectures/RL · L5.pdf", "Personal/Diary.pdf"])
        state = {}
        self.assertEqual(G.export_changed(state), ["reinforcement_learning/handwritten/Lectures/L5.pdf"])
        dest = os.path.join(self.materials, "reinforcement_learning/handwritten/Lectures/L5.pdf")
        self.assertEqual(read(dest), b"ink v1")
        self.assertEqual(G.export_changed(state), [])                      # unchanged size and date
        self.backup("EMAI/Reinforcement learning/Lectures/RL · L5.pdf", b"ink v2, more", mtime=1_800_000_900)
        self.assertEqual(G.export_changed(state), ["reinforcement_learning/handwritten/Lectures/L5.pdf"])
        self.assertEqual(read(dest), b"ink v2, more")

    def test_move_in_goodnotes_moves_the_file(self):
        old = self.backup("RL · L6.pdf", b"ink")
        state = {}
        G.export_changed(state)
        os.makedirs(os.path.join(os.path.dirname(old), "EMAI", "Reinforcement learning"))
        os.rename(old, os.path.join(os.path.dirname(old), "EMAI", "Reinforcement learning", "RL · L6.pdf"))
        self.assertEqual(G.export_changed(state), ["reinforcement_learning/handwritten/L6.pdf"])
        self.assertEqual(list(state["exports"]), ["EMAI/Reinforcement learning/RL · L6.pdf"])


@unittest.skipUnless(G.shutil.which("gs", path="/opt/homebrew/bin:/usr/local/bin"), "ghostscript not installed")
class Compression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make_pdf(self, pages=3):
        """An uncompressed PDF with many vector strokes per page, like a handwritten Goodnotes export."""
        path = os.path.join(self.tmp.name, "ink.pdf")
        page = ("0.4 setlinewidth 50 9 550 { /x exch def 50 11 800 { /y exch def x y moveto x 7 add y 3 add "
                "lineto } for } for stroke showpage ")
        ps = page * pages
        G.subprocess.run([G.tool("gs"), "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite", "-dCompressPages=false",
                          f"-sOutputFile={path}", "-c", ps], check=True, capture_output=True)
        return path

    def test_keeps_pages_and_shrinks(self):
        src = self.make_pdf()
        out = G.compress_pdf(src, self.tmp.name)
        self.assertNotEqual(out, src)
        self.assertLess(os.path.getsize(out), 0.9 * os.path.getsize(src))
        self.assertEqual(G.page_count(out), 3)

    def test_broken_pdf_falls_back_to_original(self):
        bad = os.path.join(self.tmp.name, "bad.pdf")
        with open(bad, "wb") as f:
            f.write(b"%PDF-1.4 not really")
        self.assertEqual(G.compress_pdf(bad, self.tmp.name), bad)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "compressed.pdf")))


class Schedule(unittest.TestCase):
    ICS = ("BEGIN:VCALENDAR\nX-WR-TIMEZONE:Europe/Madrid\nBEGIN:VEVENT\nDTSTART:20261022T090000\n"
           "DTEND:20261022T110000\nSUMMARY:RL\nEND:VEVENT\nBEGIN:VEVENT\nDTSTART;VALUE=DATE:20261012\n"
           "DTEND;VALUE=DATE:20261013\nEND:VEVENT\nBEGIN:VEVENT\nDTSTART;TZID=Europe/Madrid:20261022T150000\n"
           "DTEND;TZID=Europe/Madrid:20261022T163000\nEND:VEVENT\nEND:VCALENDAR\n")

    def test_event_ends(self):
        mad = ZoneInfo("Europe/Madrid")
        self.assertEqual(G.event_times(self.ICS, "DTEND"),
                         [dt.datetime(2026, 10, 22, 11, 0, tzinfo=mad), dt.datetime(2026, 10, 22, 16, 30, tzinfo=mad)])

    def test_triggers_and_due(self):
        mad = ZoneInfo("Europe/Madrid")
        now = dt.datetime(2026, 10, 22, 11, 20, tzinfo=mad)
        trig = G.export_triggers(now, self.ICS)
        kinds = [(t.astimezone(mad).strftime("%d %H:%M"), k) for t, k in trig]
        self.assertIn(("22 11:15", "after lecture"), kinds)
        self.assertIn(("22 12:00", "after lecture"), kinds)
        self.assertIn(("22 16:45", "after lecture"), kinds)
        self.assertEqual(G.due(now, None, trig), "first run")
        self.assertTrue(G.due(now, now - dt.timedelta(minutes=10), trig).startswith("after lecture"))
        self.assertIsNone(G.due(now, now - dt.timedelta(minutes=2), trig))


if __name__ == "__main__":
    unittest.main()
