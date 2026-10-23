#!/usr/bin/env python3
"""Two-way bridge between a course-materials folder (e.g. filled by aula-sync) and Goodnotes.

  in:  new slides/exercises in <materials>/<course>/ are emailed to your "Email to Goodnotes" address, so they
       show up in Goodnotes as "RL · Lecture05" (PowerPoint files are converted to PDF first).
  out: Goodnotes Auto Backup (PDF format, Google Drive) is read through Google Drive for desktop's local folder
       (no API credentials, nothing expires); every notebook that belongs to a
       course is written to <materials>/<course>/handwritten/<name>.pdf and replaced in place when it changes.

Usage:
  goodnotes_sync.py tick                        launchd calls this every 5 min: import new files, export when due
  goodnotes_sync.py import [--dry-run]          import new files now
  goodnotes_sync.py export [--dry-run]          pull changed notebooks now
  goodnotes_sync.py status                      courses, pending imports, notebook mapping, next exports
  goodnotes_sync.py backfill COURSE [--dry-run] also import a course's files that predate the first run
  goodnotes_sync.py test-import FILE            email one file to Goodnotes (checks the whole import path)
  goodnotes_sync.py test-ntfy                   send a test notification
"""
import argparse
import contextlib
import ctypes
import datetime as dt
import email.utils
import fcntl
import fnmatch
import glob
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import smtplib
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
import zipfile
from email.message import EmailMessage
from zoneinfo import ZoneInfo

HOME = os.path.expanduser("~")
APP_DIR = os.environ.get("GOODNOTES_SYNC_HOME", os.path.join(HOME, "Library/Application Support/goodnotes-sync"))
LOG_DIR = os.path.join(HOME, "Library/Logs/goodnotes-sync")
STAGING = os.path.join(HOME, "Library/Caches/goodnotes-sync")
STATE = os.path.join(APP_DIR, "state.json")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
CAL_CACHE = os.path.join(APP_DIR, "calendar.ics")
LOCK = os.path.join(APP_DIR, "lock")
AULA_SETTINGS = os.path.join(HOME, "Library/Application Support/aula-sync/settings.json")
# PowerPoint is sandboxed: it can only read and write inside its own container
PPT_TMP = os.path.join(HOME, "Library/Containers/com.microsoft.Powerpoint/Data/tmp/goodnotes-sync")
TOOL_PATH = ["/opt/homebrew/bin", "/usr/local/bin"]


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


SETTINGS = load_json(SETTINGS_FILE, {})
MATERIALS = os.path.expanduser(SETTINGS.get("materials", ""))
GOODNOTES_EMAIL = SETTINGS.get("goodnotes_email", "")
GMAIL = SETTINGS.get("gmail", "")
GMAIL_KEYCHAIN = SETTINGS.get("gmail_keychain_service", "aula-sync-gmail")
NTFY_KEYCHAIN = SETTINGS.get("ntfy_keychain_service", "aula-sync-ntfy")
# courses: {"<folder in materials>": {"prefix": "RL", "goodnotes_folders": ["Reinforcement learning"]}}
COURSES = SETTINGS.get("courses", {})
IMPORT_FOLDERS = SETTINGS.get("import_folders", ["slides", "lectures", "exercises"])
IMPORT_EXT = {"pdf", "ppt", "pptx", "pps", "ppsx"}
ONLY_AUTO = SETTINGS.get("only_auto_files", True)  # only files aula-sync wrote (*_auto.*), not your own copies
SEP = SETTINGS.get("name_separator", " · ")
HANDWRITTEN = SETTINGS.get("handwritten_folder", "handwritten")
# Goodnotes Auto Backup goes to Google Drive; Google Drive for desktop shows it in CLOUD_STORAGE, so no API
# credentials are needed. backup_folder overrides the auto-detected <account>/My Drive/<backup_folder_name>.
CLOUD_STORAGE = os.path.join(HOME, "Library/CloudStorage")
BACKUP_FOLDER = SETTINGS.get("backup_folder", "")
BACKUP_NAME = SETTINGS.get("backup_folder_name", "Goodnotes")
# folders (glob patterns, any level) inside the backup folder holding one-time copies, e.g. a manual export of
# notebooks Auto Backup never uploads because they no longer change; the live Auto Backup copy always wins
ARCHIVE_FOLDERS = SETTINGS.get("archive_folders", ["*manual-backup*", "*manual backup*"])
CALENDAR_URL = SETTINGS.get("calendar_url") or load_json(AULA_SETTINGS, {}).get("calendar_url", "")
AFTER_LECTURE = SETTINGS.get("export_after_lecture_minutes", [15, 60])
EXPORT_TIMES = SETTINGS.get("export_times", ["20:00"])
NOTIFY = SETTINGS.get("notify", True)  # push a summary after each import/export (errors are always pushed)
# the first run imports files changed after this local time (e.g. "2026-10-22T15:00") instead of only recording them
IMPORT_SINCE = SETTINGS.get("import_since", "")
COMPRESS = SETTINGS.get("compress", True)  # detail-preserving recompression of imported and exported PDFs
MIN_SAVING = 0.10                          # keep the recompressed PDF only if it is at least 10 % smaller
# Ghostscript /prepress keeps vector ink and text as vectors (lossless), passes existing JPEGs through untouched,
# keeps colours as they are, and only downsamples images above 450 dpi, to 300 dpi
GS_KEEP_DETAIL = ["-dPDFSETTINGS=/prepress", "-dCompatibilityLevel=1.7", "-sColorConversionStrategy=LeaveColorUnchanged",
                  "-dPassThroughJPEGImages=true", "-dDetectDuplicateImages=true"]
# last resort, only for a file that is still too large to email: 150 dpi images
GS_EMAIL = ["-dPDFSETTINGS=/ebook", "-sColorConversionStrategy=RGB"]

CAL_REFRESH = 6 * 3600
SETTLE = 120                   # seconds a file must be untouched before it is imported
MAX_MAIL_BYTES = 18 * 1024**2  # Gmail caps a message at 25 MB *after* base64 (+33 %)
ALERT_EVERY = 24 * 3600
RETRY_MAX = 6 * 3600
DONE = ("sent", "baseline", "too_large")
SF_DATALESS = 0x40000000       # iCloud file evicted from local disk

log = logging.getLogger("goodnotes-sync")


# ---------------------------------------------------------------- utilities
def setup_logging(verbose):
    os.makedirs(LOG_DIR, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(os.path.join(LOG_DIR, "goodnotes-sync.log"), maxBytes=1_000_000,
                                             backupCount=5)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    if verbose:
        log.addHandler(logging.StreamHandler(sys.stdout))
    log.setLevel(logging.INFO)


def clean_name(s):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s or "")
    return re.sub(r"\s+", " ", s)[:150].strip(" .") or "untitled"


def norm(s):
    return re.sub(r"[\s_\-]+", " ", s).strip().casefold()


def ext_of(filename):
    return os.path.splitext(filename)[1][1:].lower()


def file_hash(path, algo="sha256"):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_dataless(st):
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)


def tool(name):
    path = shutil.which(name, path=os.pathsep.join(TOOL_PATH + [os.environ.get("PATH", "")]))
    if not path:
        raise RuntimeError(f"{name} not found (brew install {name})")
    return path


def keychain(service, account=None):
    cmd = ["/usr/bin/security", "find-generic-password", "-s", service] + (["-a", account] if account else [])
    r = subprocess.run(cmd + ["-w"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Keychain item '{service}' not found")
    return r.stdout.rstrip("\n")


def allow_downloads():
    """Let this process read files that Google Drive (or iCloud) only streams.

    Background jobs may not download "dataless" placeholders by default; reading one then fails with
    EDEADLK ("Resource deadlock avoided"). Same as setiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
    IOPOL_SCOPE_PROCESS, IOPOL_MATERIALIZE_DATALESS_FILES_ON) in C.
    """
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        if libc.setiopolicy_np(3, 0, 2) != 0:
            log.warning("setiopolicy_np failed: errno %d", ctypes.get_errno())
    except (OSError, AttributeError) as e:
        log.warning("cannot allow downloads of streamed files: %s", e)


def online(host="www.googleapis.com"):
    try:
        socket.create_connection((host, 443), timeout=10).close()
        return True
    except OSError:
        return False


def staging():
    os.makedirs(STAGING, exist_ok=True)
    return STAGING


# ---------------------------------------------------------------- courses and names
def course_folders():
    return [c for c, cfg in COURSES.items() if not cfg.get("exclude")]


def course_prefix(course):
    return (COURSES.get(course) or {}).get("prefix") or "".join(w[0] for w in re.split(r"[_\W]+", course) if w).upper()


def course_aliases(course):
    """Goodnotes folder names (normalised) that stand for this course."""
    cfg = COURSES.get(course) or {}
    return {norm(n) for n in [course, course_prefix(course), *cfg.get("goodnotes_folders", [])]}


def display_stem(filename):
    """'S04SR_auto.pdf' -> 'S04SR'; 'L3_auto (2).pdf' -> 'L3 (2)'."""
    stem = os.path.splitext(filename)[0]
    return re.sub(r"_auto(?=$| \(\d+\)$)", "", stem).strip() or stem


def goodnotes_name(course, filename, updated=False):
    name = f"{course_prefix(course)}{SEP}{display_stem(filename)}"
    return clean_name(name + (f" (updated {dt.date.today():%d %b})" if updated else ""))


def strip_prefix(name):
    """('reinforcement_learning', 'Lecture05') for 'RL · Lecture05' (also 'RL - …', 'RL: …'), else (None, name).

    '_' or a dash without spaces is part of a name ('RL_emai_26_1', 'AS-Theory'), not a separator.
    """
    for course in course_folders():
        m = re.match(rf"^\s*{re.escape(course_prefix(course))}(?:\s*[·•:|]\s*|\s+[-–—]\s+)(.+)$", name)
        if m:
            return course, m.group(1).strip()
    return None, name


def map_remote(path):
    """(course, [subfolders..., name]) for a backed-up notebook PDF, or None if it belongs to no course.

    A Goodnotes folder named like a course (its materials folder, prefix or a goodnotes_folders entry, e.g.
    EMAI/Reinforcement learning) maps everything below it, keeping subfolders. A notebook outside such a
    folder (e.g. just imported into the library root) maps by its course prefix ('RL · Lecture05').
    """
    parts = [p for p in path.split("/") if p]
    # Goodnotes names emailed imports after the attachment incl. its extension: 'SPA · X.pdf' -> 'SPA · X.pdf.pdf'
    stem = re.sub(r"(\.pdf)+$", "", parts[-1], flags=re.I)
    prefixed, name = strip_prefix(stem)
    for i, part in enumerate(parts[:-1]):
        for c in course_folders():
            if norm(part) in course_aliases(c):
                return c, [clean_name(p) for p in parts[i + 1:-1]] + [clean_name(name)]
    if prefixed:
        return prefixed, [clean_name(name)]
    return None


def handwritten_dest(course, parts):
    root = os.path.normpath(os.path.join(MATERIALS, course, HANDWRITTEN))
    dest = os.path.normpath(os.path.join(root, *parts[:-1], parts[-1] + ".pdf"))
    if not dest.startswith(root + os.sep):  # never write outside <course>/handwritten/
        raise ValueError(f"refusing to write outside {root}: {dest}")
    return dest


# ---------------------------------------------------------------- import (materials -> Goodnotes)
def _walk_error(e):
    if not isinstance(e, FileNotFoundError):  # a missing import folder is normal; no access is not
        raise e


def candidates(only_course=None):
    """(course, relpath, path, stat) of importable files: <course>/<import folder>/**/<name>_auto.<pdf|pptx…>."""
    for course in course_folders():
        if only_course and course != only_course:
            continue
        for sub in IMPORT_FOLDERS:
            for dp, dns, fns in os.walk(os.path.join(MATERIALS, course, sub), onerror=_walk_error):
                dns[:] = sorted(d for d in dns if not d.startswith(".") and d != HANDWRITTEN)
                for fn in sorted(fns):
                    if fn.startswith(".") or ext_of(fn) not in IMPORT_EXT:
                        continue
                    if ONLY_AUTO and "_auto" not in os.path.splitext(fn)[0]:
                        continue
                    p = os.path.join(dp, fn)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    yield course, os.path.relpath(p, MATERIALS), p, st


def pending_imports(state, only_course=None, now=None):
    """Files to send now, as (course, rel, path, sha256, updated). The first run only records a baseline."""
    now = now or time.time()
    imports = state.setdefault("imports", {})
    first = not state.get("baseline_done")
    since = dt.datetime.fromisoformat(IMPORT_SINCE).timestamp() if IMPORT_SINCE else now
    out = []
    for course, rel, path, st in candidates(only_course):
        rec = imports.get(rel)
        redo = bool(only_course and rec and rec.get("status") == "baseline")
        if rec and (rec["size"], rec["mtime"]) == (st.st_size, st.st_mtime):
            if rec["status"] in DONE and not redo:
                continue
            if rec["status"] == "failed" and now < rec.get("retry_at", 0):
                continue
            digest = rec["sha256"]
        else:
            if is_dataless(st) or now - st.st_mtime < SETTLE:
                continue  # not on disk yet / still being written
            digest = file_hash(path)
            if rec and rec.get("sha256") == digest and rec["status"] in DONE and not redo:
                rec.update(size=st.st_size, mtime=st.st_mtime)  # touched, same content
                continue
        if first and not only_course and st.st_mtime <= since:
            imports[rel] = {"status": "baseline", "sha256": digest, "size": st.st_size, "mtime": st.st_mtime}
            continue
        prev = rec and (rec.get("sent_sha256") or (rec["sha256"] if rec.get("status") == "baseline" else None))
        out.append((course, rel, path, digest, bool(prev and prev != digest)))
    if first and not only_course:
        state["baseline_done"] = now
    return out


def ppt_to_pdf(src, workdir):
    """Export a PowerPoint file to PDF with Microsoft PowerPoint (and quit it again if we started it)."""
    os.makedirs(PPT_TMP, exist_ok=True)
    tag = hashlib.sha1(src.encode()).hexdigest()[:12]
    inp, out = os.path.join(PPT_TMP, f"{tag}.{ext_of(src)}"), os.path.join(PPT_TMP, f"{tag}.pdf")
    shutil.copyfile(src, inp)
    running = subprocess.run(["/usr/bin/pgrep", "-x", "Microsoft PowerPoint"], capture_output=True).returncode == 0
    script = """on run argv
  tell application "Microsoft PowerPoint"
    open (POSIX file (item 1 of argv))
    set p to presentation (item 3 of argv)
    save p in (POSIX file (item 2 of argv)) as save as PDF
    close p saving no
    if (item 4 of argv) is "quit" then quit
  end tell
end run"""
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", script, inp, out, os.path.basename(inp),
                            "stay" if running else "quit"], capture_output=True, text=True, timeout=300)
        for _ in range(30):  # PowerPoint may still be flushing the file
            if os.path.exists(out) and os.path.getsize(out) > 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"PowerPoint did not export {src}: {r.stderr.strip() or 'no PDF written'}")
        dst = os.path.join(workdir, "converted.pdf")
        shutil.move(out, dst)
        return dst
    finally:
        for p in (inp, out):
            with contextlib.suppress(FileNotFoundError):
                os.remove(p)


def page_count(pdf):
    r = subprocess.run([tool("gs"), "-q", "-dNODISPLAY", "-dNOSAFER", f"-sFile={pdf}", "-c",
                        "File (r) file runpdfbegin pdfpagecount = quit"], capture_output=True, text=True, timeout=300)
    return int(r.stdout.strip().splitlines()[-1])


def compress_pdf(src, workdir, profile=GS_KEEP_DETAIL, name="compressed.pdf"):
    """Recompressed copy of `src` in workdir, or `src` itself when that saves < MIN_SAVING or anything fails."""
    dst = os.path.join(workdir, name)
    try:
        subprocess.run([tool("gs"), "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite", *profile,
                        f"-sOutputFile={dst}", src], check=True, capture_output=True, timeout=900)
        if page_count(dst) != page_count(src):
            raise RuntimeError("page count changed")
    except Exception as e:
        log.warning("not compressing %s: %s", os.path.basename(src), e)
        with contextlib.suppress(FileNotFoundError):
            os.remove(dst)
        return src
    before, after = os.path.getsize(src), os.path.getsize(dst)
    if after > (1 - MIN_SAVING) * before:
        os.remove(dst)
        return src
    log.info("compressed %s: %.1f -> %.1f MB", os.path.basename(src), before / 1e6, after / 1e6)
    return dst


def as_mailable_pdf(path, workdir):
    """PDF version of `path` small enough for Gmail, or None if even the compressed PDF is too large."""
    pdf = path if ext_of(path) == "pdf" else ppt_to_pdf(path, workdir)
    if COMPRESS:
        pdf = compress_pdf(pdf, workdir)
    if os.path.getsize(pdf) > MAX_MAIL_BYTES:
        pdf = compress_pdf(pdf, workdir, GS_EMAIL, "small.pdf")
    return pdf if os.path.getsize(pdf) <= MAX_MAIL_BYTES else None


def send_mail(to, subject, body, attachment=None, filename=None):
    pw = keychain(GMAIL_KEYCHAIN, GMAIL).replace(" ", "")  # Google shows app passwords as 4 groups of 4
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"goodnotes-sync <{GMAIL}>", to
    msg["Date"], msg["Message-ID"] = email.utils.formatdate(localtime=True), email.utils.make_msgid()
    msg.set_content(body)
    if attachment:
        with open(attachment, "rb") as f:
            msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=filename)
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=120) as s:
        s.starttls()
        s.login(GMAIL, pw)
        s.send_message(msg)


def deliver(path, name, rel):
    """Email `path` (as PDF) to Goodnotes as notebook `name`. Returns False if it is too large to email."""
    with tempfile.TemporaryDirectory(dir=staging()) as work:
        pdf = as_mailable_pdf(path, work)
        if not pdf:
            return False
        send_mail(GOODNOTES_EMAIL, name, f"{rel}\n\nImported by goodnotes-sync.", pdf, name + ".pdf")
    log.info("imported %s as '%s'", rel, name)
    return True


def import_new(state, dry=False, only_course=None):
    """Email new or updated course files to Goodnotes. Returns the notebook names sent."""
    if not GOODNOTES_EMAIL and not dry:
        if not state.get("baseline_done"):
            pending_imports(state)  # record what is there now; files arriving before setup are imported later
        log.info("import skipped: goodnotes_email not set")
        return []
    sent, too_large = [], []
    for course, rel, path, digest, updated in pending_imports(state, only_course):
        name = goodnotes_name(course, os.path.basename(path), updated)
        if dry:
            print(f"  would import {rel}  ->  '{name}'")
            continue
        st = os.stat(path)
        rec = state["imports"].setdefault(rel, {})
        rec.update(sha256=digest, size=st.st_size, mtime=st.st_mtime, name=name, time=time.time())
        try:
            if deliver(path, name, rel):
                rec.update(status="sent", sent_sha256=digest, tries=0)
                sent.append(name)
            else:
                rec["status"] = "too_large"
                too_large.append(rel)
        except Exception as e:
            rec["tries"] = rec.get("tries", 0) + 1
            rec.update(status="failed", error=str(e), retry_at=time.time() + min(300 * 2 ** rec["tries"], RETRY_MAX))
            log.error("import of %s failed (try %d): %s", rel, rec["tries"], e)
            if rec["tries"] >= 3:
                alert(state, "import", f"Could not import {rel} into Goodnotes after {rec['tries']} tries:\n{e}")
        save_json(STATE, state)
        time.sleep(2)
    if too_large:
        alert(state, "too_large", "Too large to email to Goodnotes even after compression; import manually:\n"
              + "\n".join(too_large))
    if sent and NOTIFY:
        notify(f"📥 {len(sent)} new in Goodnotes", "\n".join(sent))
    return sent


# ---------------------------------------------------------------- export (Goodnotes Auto Backup -> materials)
class BackupUnavailable(Exception):
    pass


def backup_folder():
    """The Auto Backup folder as Google Drive for desktop shows it, or None if nothing is backed up yet.

    Raises BackupUnavailable when Google Drive for desktop isn't installed and signed in.
    """
    if BACKUP_FOLDER:
        path = os.path.expanduser(BACKUP_FOLDER)
        return path if os.path.isdir(path) else None
    accounts = sorted(glob.glob(os.path.join(CLOUD_STORAGE, "GoogleDrive-*")))
    if not accounts:
        raise BackupUnavailable("Google Drive for desktop is not set up: install it (brew install --cask "
                                "google-drive), open it and sign in with the Google account Goodnotes backs up to.")
    for account in accounts:  # <account>/<"My Drive", localised>/Goodnotes
        for path in sorted(glob.glob(os.path.join(account, "*", BACKUP_NAME))):
            if os.path.isdir(path):
                return path
    return None


def remote_listing():
    """PDFs in the Auto Backup folder: [{"Path", "Size", "ModTime"}], Path relative and '/'-separated."""
    root = backup_folder()
    if not root:
        log.info("no '%s' folder in Google Drive yet: nothing backed up", BACKUP_NAME)
        return []
    out = []
    for dp, dns, fns in os.walk(root, onerror=_walk_error):
        dns[:] = sorted(d for d in dns if not d.startswith("."))
        for fn in sorted(fns):
            if fn.startswith(".") or not fn.lower().endswith(".pdf"):
                continue
            p = os.path.join(dp, fn)
            st = os.stat(p)  # placeholders of not-yet-downloaded files report their real size and date
            out.append({"Path": os.path.relpath(p, root).replace(os.sep, "/"), "Size": st.st_size,
                        "ModTime": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).isoformat()})
    return out


def fetch(remote_path, tmp):
    """Copy a backed-up PDF; Google Drive for desktop downloads it first if it is only streamed."""
    shutil.copyfile(os.path.join(backup_folder(), *remote_path.split("/")), tmp)


def fingerprint(entry):
    return (entry.get("Hashes") or {}).get("md5") or f"{entry['Size']}:{entry['ModTime']}"


def same_slot(rec_dest, dest):
    """True if `rec_dest` (relpath) is `dest` or the 'name (n).pdf' sibling we fell back to for it."""
    stem = os.path.splitext(os.path.relpath(dest, MATERIALS))[0]
    return rec_dest == stem + ".pdf" or bool(re.fullmatch(re.escape(stem) + r" \(\d+\)\.pdf", rec_dest))


def archived(path):
    """True if a backed-up PDF sits in an archive folder (a one-time copy, see ARCHIVE_FOLDERS)."""
    return any(fnmatch.fnmatch(part.casefold(), pat.casefold()) for part in path.split("/")[:-1]
               for pat in ARCHIVE_FOLDERS)


def preference(entry):
    """Sort key: a live Auto Backup copy beats an archived one; then the newer one wins."""
    return not archived(entry["Path"]), entry["ModTime"]


def adopt_seed(e, fp, dest, exports):
    """If archived entry `e` is the very PDF `seed` placed at `dest` (same size as its source), take it over as
    that file's record without downloading it again. Returns True when adopted."""
    rel = os.path.relpath(dest, MATERIALS)
    for key, rec in list(exports.items()):
        if rec.get("fingerprint") == "seed" and rec.get("dest") == rel and rec.get("source_size") == e["Size"] \
                and unchanged_since_written(dest, rec):
            exports[e["Path"]] = {**rec, "fingerprint": fp, "time": time.time()}
            del exports[key]
            return True
    return False


def plan_export(listing, state):
    """[(entry, fingerprint, dest)] of notebooks whose PDF changed since the last export, plus unmapped paths."""
    exports = state.setdefault("exports", {})
    best, unmapped = {}, []
    for e in listing:
        m = map_remote(e["Path"])
        if not m:
            unmapped.append(e["Path"])
            continue
        dest = handwritten_dest(*m)
        if dest in best and preference(best[dest]) >= preference(e):
            if not (archived(e["Path"]) or archived(best[dest]["Path"])):
                log.warning("%s and %s both map to %s; keeping the newer", best[dest]["Path"], e["Path"], dest)
            continue
        best[dest] = e
    todo = []
    for dest, e in sorted(best.items()):
        fp, rec = fingerprint(e), exports.get(e["Path"])
        if rec and rec["fingerprint"] == fp and same_slot(rec["dest"], dest) \
                and os.path.lexists(os.path.join(MATERIALS, rec["dest"])):
            continue
        if not rec and archived(e["Path"]) and adopt_seed(e, fp, dest, exports):
            continue
        todo.append((e, fp, writable(dest, exports)))
    return todo, unmapped


def unchanged_since_written(path, rec):
    """True if `path` still holds exactly what we wrote for export record `rec` (evicted iCloud files: same size)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False
    if is_dataless(st):
        return st.st_size == rec.get("size")
    return file_hash(path, "md5") == rec.get("md5")


def writable(dest, exports):
    """`dest` if it is free or still holds exactly what we last wrote there; otherwise a sibling 'name (2).pdf'.

    So a file you put in handwritten/ yourself (or edited on the Mac) is never overwritten.
    """
    def ours(path):  # several records can name one file (an archived copy and its later live upload)
        rel = os.path.relpath(path, MATERIALS)
        return any(r.get("dest") == rel and unchanged_since_written(path, r) for r in exports.values())

    stem = os.path.splitext(dest)[0]
    for cand in [dest] + [f"{stem} ({i}).pdf" for i in range(2, 1000)]:
        if not os.path.lexists(cand) or ours(cand):
            return cand
    raise RuntimeError(f"no free name for {dest}")


def moved_from(dest, gone):
    """(old remote path, record) of a notebook that disappeared from the backup and now maps to `dest`:
    the same course and file name, i.e. you moved it in Goodnotes (e.g. from the library root into a folder)."""
    rel = os.path.relpath(dest, MATERIALS)
    course, base = rel.split(os.sep)[0], os.path.basename(rel)
    for path, rec in gone.items():
        if rec["dest"].split("/")[0] == course and os.path.basename(rec["dest"]) == base:
            return path, rec
    return None, None


def export_changed(state, dry=False):
    """Pull changed notebook PDFs into <course>/handwritten/. Returns the relpaths written."""
    listing = remote_listing()
    todo, unmapped = plan_export(listing, state)
    if unmapped:
        log.info("%d backed-up notebooks belong to no course (see `status`)", len(unmapped))
    exports = state["exports"]
    listed = {e["Path"] for e in listing}
    gone = {p: r for p, r in exports.items() if p not in listed}
    written = []
    for e, fp, dest in todo:
        rel = os.path.relpath(dest, MATERIALS)
        old_path, old = (None, None) if e["Path"] in exports else moved_from(dest, gone)
        old_file = old and os.path.join(MATERIALS, old["dest"])
        if dry:
            print(f"  would update {rel}  <-  {e['Path']}" + (f"  (moved from {old['dest']})" if old else ""))
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if old and old["fingerprint"] == fp and unchanged_since_written(old_file, old):
            if old_file != dest:
                os.replace(old_file, dest)  # moved in Goodnotes, content unchanged: move our file, no download
            exports[e["Path"]] = {**old, "dest": rel, "time": time.time()}
            del exports[old_path], gone[old_path]
            save_json(STATE, state)
            written.append(rel)
            log.info("moved %s -> %s", old["dest"], rel)
            continue
        tmp = os.path.join(staging(), f"export-{os.getpid()}.pdf")
        fetch(e["Path"], tmp)
        if (e.get("Hashes") or {}).get("md5") not in (None, file_hash(tmp, "md5")):
            os.remove(tmp)
            raise RuntimeError(f"checksum mismatch downloading {e['Path']}")
        if COMPRESS:
            small = compress_pdf(tmp, staging(), name=f"export-{os.getpid()}.small.pdf")
            if small != tmp:
                os.replace(small, tmp)
        md5 = file_hash(tmp, "md5")  # of what we write, so `writable` can tell whether it was edited since
        with contextlib.suppress(ValueError, OSError):
            mtime = dt.datetime.fromisoformat(re.sub(r"(\.\d{6})\d*", r"\1", e["ModTime"]).replace("Z", "+00:00"))
            os.utime(tmp, (time.time(), mtime.timestamp()))
        os.replace(tmp, dest)  # same file name every time: the previous version is replaced, not kept
        exports[e["Path"]] = {"fingerprint": fp, "md5": md5, "size": os.path.getsize(dest), "dest": rel,
                              "time": time.time()}
        if old:  # moved and edited: drop the copy at the old place if it is still exactly ours
            if old_file != dest and unchanged_since_written(old_file, old):
                os.remove(old_file)
                log.info("removed %s (notebook moved to %s)", old["dest"], rel)
            del exports[old_path], gone[old_path]
        save_json(STATE, state)
        written.append(rel)
        log.info("exported %s -> %s", e["Path"], rel)
    if written and NOTIFY:
        notify(f"✍️ {len(written)} handwritten note{'s' * (len(written) > 1)} updated", "\n".join(written))
    return written


# ---------------------------------------------------------------- seed (one-off: existing notebooks)
def notebook_key(name):
    """'Extra1-emotions.pdf.pdf', 'Extra1-emotions.pdf' and 'Extra1-emotions.pdf.goodnotes' -> 'extra1-emotions'."""
    name = re.sub(r"\.goodnotes$", "", name, flags=re.I)
    return re.sub(r"(\.pdf)+$", "", name, flags=re.I).strip().casefold()


def backup_layout(zip_path):
    """notebook_key -> [library path without extension] from a Goodnotes backup zip (Settings › Export backup)."""
    out = {}
    with zipfile.ZipFile(zip_path) as z:
        for n in z.namelist():
            if n.lower().endswith(".goodnotes") and not n.startswith("."):
                out.setdefault(notebook_key(os.path.basename(n)), []).append(n[:-len(".goodnotes")])
    return out


def library_path(rel, layout):
    """Library path (no extension) of an exported PDF at `rel`, using the backup layout when given; None if
    ambiguous (same name in several folders and the export's own folders don't tell which)."""
    folders = [p for p in rel.split("/")[:-1] if p]
    found = (layout or {}).get(notebook_key(os.path.basename(rel)), [])
    if folders:  # keep candidates whose folders end with the export's folders
        found = [c for c in found if [norm(p) for p in c.split("/")[:-1][-len(folders):]] ==
                 [norm(p) for p in folders]] or found
    if len(found) == 1:
        return found[0]
    if not found and not layout:
        return re.sub(r"(\.pdf)+$", "", rel, flags=re.I)
    return None


def seed(source, state, layout_zip=None, dry=False):
    """Place a Goodnotes PDF export (folder or zip) of existing notebooks into <course>/handwritten/ and register
    each file under the path Auto Backup will use, so the first real backup of a notebook replaces it in place."""
    exports = state.setdefault("exports", {})
    layout = backup_layout(layout_zip) if layout_zip else None
    placed, skipped = [], []
    with tempfile.TemporaryDirectory(dir=staging()) as work:
        root = source
        if zipfile.is_zipfile(source):
            root = os.path.join(work, "export")
            with zipfile.ZipFile(source) as z:
                for n in z.namelist():  # PDFs only, and never outside the temporary folder
                    if n.lower().endswith(".pdf") and not n.startswith(("/", "__MACOSX")) and ".." not in n.split("/"):
                        z.extract(n, root)
        for dp, dns, fns in os.walk(root):
            dns[:] = sorted(d for d in dns if not d.startswith("."))
            for fn in sorted(f for f in fns if f.lower().endswith(".pdf") and not f.startswith(".")):
                src = os.path.join(dp, fn)
                rel = os.path.relpath(src, root).replace(os.sep, "/")
                lib = library_path(rel, layout)
                m = lib and map_remote(lib + ".pdf")
                if not m:
                    skipped.append(f"{rel} ({'no course' if lib else 'not found in layout / ambiguous'})")
                    continue
                key = lib + ".pdf"  # what Auto Backup will call it
                if exports.get(key, {}).get("fingerprint", "seed") != "seed":
                    skipped.append(f"{rel} (already exported from Auto Backup)")
                    continue
                dest = writable(handwritten_dest(*m), exports)
                drel = os.path.relpath(dest, MATERIALS)
                if dry:
                    print(f"  would place {rel}  ->  {drel}")
                    placed.append(drel)
                    continue
                pdf = compress_pdf(src, work, name="seed.pdf") if COMPRESS else src
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(pdf, dest + ".part")
                os.replace(dest + ".part", dest)
                exports[key] = {"fingerprint": "seed", "md5": file_hash(dest, "md5"), "size": os.path.getsize(dest),
                                "dest": drel, "time": time.time(), "source_size": os.path.getsize(src)}
                save_json(STATE, state)
                placed.append(drel)
    for s in skipped:
        print(f"  skipped {s}")
    print(f"{'would place' if dry else 'placed'} {len(placed)} notebooks; skipped {len(skipped)}")
    return placed, skipped


# ---------------------------------------------------------------- schedule
def event_times(ics, prop):
    """Aware datetimes of `prop` (DTSTART/DTEND) of the timed events in an iCalendar feed."""
    default_tz = re.search(r"^X-WR-TIMEZONE:(.+)$", ics, re.M)
    default_tz = default_tz.group(1).strip() if default_tz else "Europe/Madrid"
    out = []
    for m in re.finditer(rf"^{prop}((?:;[^:\r\n]*)?):(\d{{8}}T\d{{6}})(Z?)\s*$", ics, re.M):
        params, stamp, utc = m.groups()
        tz = re.search(r";TZID=([^;]+)", params)
        tz = dt.timezone.utc if utc else ZoneInfo(tz.group(1) if tz else default_tz)
        out.append(dt.datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=tz))
    return sorted(out)


def calendar_text():
    """The lecture calendar, refreshed every CAL_REFRESH; falls back to the cached copy when offline."""
    if not CALENDAR_URL:
        return ""
    if not (os.path.exists(CAL_CACHE) and time.time() - os.path.getmtime(CAL_CACHE) < CAL_REFRESH):
        try:
            with urllib.request.urlopen(CALENDAR_URL.replace("webcal://", "https://", 1), timeout=30) as r:
                text = r.read().decode("utf-8", "replace")
            if "BEGIN:VCALENDAR" in text:
                with open(CAL_CACHE + ".tmp", "w") as f:
                    f.write(text)
                os.replace(CAL_CACHE + ".tmp", CAL_CACHE)
        except Exception as e:
            log.warning("calendar not refreshed (using cache): %s", e)
    try:
        with open(CAL_CACHE) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def export_triggers(now, ics):
    """(time, kind) of each export slot from yesterday to tomorrow: daily times and lecture ends + offsets."""
    today = now.astimezone().date()
    days = [today + dt.timedelta(days=d) for d in (-1, 0, 1)]
    daily = [(dt.datetime.combine(d, dt.time.fromisoformat(t)).astimezone(), "daily") for d in days
             for t in EXPORT_TIMES]
    lo, hi = now - dt.timedelta(days=2), now + dt.timedelta(days=2)
    lectures = [(t + dt.timedelta(minutes=o), "after lecture") for t in event_times(ics, "DTEND") if lo < t < hi
                for o in AFTER_LECTURE]
    return sorted(daily + lectures)


def due(now, last, triggers):
    """Why an export is due at `now` (aware datetimes; last may be None), or None."""
    if last is None:
        return "first run"
    for t, kind in triggers:
        if last < t <= now:
            return f"{kind} {t.astimezone():%a %H:%M}"
    return None


# ---------------------------------------------------------------- notifications
def notify(title, body, priority="default", tags="pencil"):
    """Push via ntfy (topic in Keychain); falls back to an email to yourself."""
    try:
        topic = keychain(NTFY_KEYCHAIN, "topic")
    except RuntimeError:
        topic = None
    try:
        if topic:
            req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body[:3800].encode(), method="POST",
                                         headers={"Title": title.encode().decode("latin-1", "replace"),
                                                  "Priority": priority, "Tags": tags})
            urllib.request.urlopen(req, timeout=30).close()
        elif GMAIL:
            send_mail(GMAIL, title, body)
        log.info("notified: %s", title)
    except Exception as e:
        log.error("could not notify '%s': %s", title, e)


def alert(state, kind, text):
    """Error push, at most once per 24 h per kind."""
    log.error("%s: %s", kind, text)
    alerts = state.setdefault("alerts", {})
    if time.time() - alerts.get(kind, 0) < ALERT_EVERY:
        return
    alerts[kind] = time.time()
    notify("⚠️ goodnotes-sync problem", text[-3000:], priority="high", tags="warning")


# ---------------------------------------------------------------- commands
@contextlib.contextmanager
def locked():
    os.makedirs(APP_DIR, exist_ok=True)
    with open(LOCK, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("another run is in progress")
            yield False
            return
        yield True


def check_config():
    if not MATERIALS or not os.path.isdir(MATERIALS):
        raise SystemExit(f"materials folder not found: {MATERIALS!r} (set it in {SETTINGS_FILE})")
    if not course_folders():
        raise SystemExit(f"no courses configured in {SETTINGS_FILE}")


def run_export(state):
    try:
        export_changed(state)
    except BackupUnavailable as e:
        alert(state, "drive", str(e))
    except PermissionError as e:
        alert(state, "drive", f"macOS blocks the background agent from Google Drive ({e.filename}).\nAllow it: "
                              f"System Settings → Privacy & Security → Files & Folders → goodnotes-sync → Google "
                              f"Drive (or add {os.path.join(APP_DIR, 'app', 'goodnotes-sync')} to Full Disk Access).")
    except Exception as e:
        alert(state, "export", f"Export from the Goodnotes backup failed:\n{e}\n\n{traceback.format_exc()[-1500:]}")


def tick():
    with locked() as ok:
        if not ok:
            return
        check_config()
        state = load_json(STATE, {})
        try:
            os.listdir(MATERIALS)
        except PermissionError:
            alert(state, "access", f"macOS blocks the background agent from {MATERIALS}.\nAllow it: System "
                                   f"Settings → Privacy & Security → Files & Folders → goodnotes-sync → iCloud "
                                   f"Drive (or add {os.path.join(APP_DIR, 'app', 'goodnotes-sync')} to Full "
                                   f"Disk Access).")
            save_json(STATE, state)
            return
        except OSError as e:  # e.g. interrupted while iCloud is busy: try again at the next tick
            log.warning("cannot read %s now: %s", MATERIALS, e)
            return
        if not online():
            return
        try:
            import_new(state)
        except Exception as e:
            alert(state, "import", f"Import into Goodnotes failed:\n{e}\n\n{traceback.format_exc()[-1500:]}")
        now = dt.datetime.now(dt.timezone.utc)
        last = state.get("last_export")
        reason = due(now, last and dt.datetime.fromtimestamp(last, dt.timezone.utc),
                     export_triggers(now, calendar_text()))
        if reason:
            log.info("export due: %s", reason)
            state["last_export"] = now.timestamp()  # an attempt counts: a failing export retries at the next slot
            run_export(state)
        save_json(STATE, state)


def status():
    check_config()
    state = load_json(STATE, {})
    print(f"materials: {MATERIALS}\ngoodnotes_email: {GOODNOTES_EMAIL or '(not set: imports are off)'}")
    print("\ncourses (prefix in Goodnotes, Goodnotes folder names that map to it):")
    for c in course_folders():
        print(f"  {course_prefix(c):<5} {c:<34} {', '.join(sorted(course_aliases(c)))}")
    pend = pending_imports(json.loads(json.dumps(state)))
    note = "" if state.get("baseline_done") else " (first run only records what is already there)"
    print(f"\npending imports: {len(pend)}{note}")
    for _, rel, *_ in pend:
        print(f"  {rel}")
    try:
        root = backup_folder()
        print(f"\nGoodnotes backup ({root or 'no ' + BACKUP_NAME + ' folder in Google Drive yet'}):")
    except BackupUnavailable as e:
        root = None
        print(f"\nGoodnotes backup: {e}")
    if root:
        listing = remote_listing()
        todo, unmapped = plan_export(listing, json.loads(json.dumps(state)))
        changed = {e["Path"]: d for e, _, d in todo}
        for e in sorted(listing, key=lambda e: e["Path"]):
            m = map_remote(e["Path"])
            if m:
                dest = os.path.relpath(changed.get(e["Path"]) or handwritten_dest(*m), MATERIALS)
                print(f"  {'↻' if e['Path'] in changed else '✓'} {e['Path']}  ->  {dest}")
        for p in unmapped:
            print(f"  – {p}  (no course: add a prefix or a goodnotes_folders entry)")
    now = dt.datetime.now(dt.timezone.utc)
    last = state.get("last_export")
    print(f"\nlast export: {dt.datetime.fromtimestamp(last):%a %d %b %H:%M}" if last else "\nlast export: never")
    nxt = [x for x in export_triggers(now, calendar_text()) if x[0] > now][:6]
    print("next exports: " + ", ".join(f"{t.astimezone():%a %H:%M} ({k})" for t, k in nxt))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["tick", "import", "export", "status", "backfill", "seed", "test-import",
                                    "test-ntfy"])
    ap.add_argument("arg", nargs="?", help="backfill: course folder; seed: PDF export (folder or zip); "
                                           "test-import: file")
    ap.add_argument("--layout", help="seed: Goodnotes backup zip, to find each notebook's folders")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    setup_logging(a.verbose or a.cmd != "tick")
    allow_downloads()
    if a.cmd == "tick":
        return tick()
    if a.cmd == "test-ntfy":
        return notify("goodnotes-sync: test", f"Test notification at {dt.datetime.now():%Y-%m-%d %H:%M}.")
    if a.cmd == "status":
        return status()
    check_config()
    with locked() as ok:
        if not ok:
            raise SystemExit("another run is in progress")
        state = load_json(STATE, {})
        work = json.loads(json.dumps(state)) if a.dry_run else state
        if a.cmd == "import":
            import_new(work, dry=a.dry_run)
        elif a.cmd == "backfill":
            if a.arg not in course_folders():
                raise SystemExit(f"unknown course {a.arg!r}; one of: {', '.join(course_folders())}")
            import_new(work, dry=a.dry_run, only_course=a.arg)
        elif a.cmd == "export":
            export_changed(work, dry=True) if a.dry_run else run_export(work)
        elif a.cmd == "seed":
            if not a.arg or not os.path.exists(a.arg):
                raise SystemExit("seed needs a Goodnotes PDF export (folder or zip)")
            seed(os.path.abspath(a.arg), work, a.layout and os.path.abspath(a.layout), dry=a.dry_run)
        elif a.cmd == "test-import":
            path = os.path.abspath(a.arg or "")
            rel = os.path.relpath(path, MATERIALS)
            course = rel.split(os.sep)[0]
            name = goodnotes_name(course, os.path.basename(path)) if course in COURSES else \
                clean_name("TEST" + SEP + display_stem(os.path.basename(path)))
            print("sent as" if deliver(path, name, rel) else "too large to email:", name)
        if not a.dry_run:
            save_json(STATE, state)


if __name__ == "__main__":
    main()
