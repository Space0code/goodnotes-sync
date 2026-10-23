# goodnotes-sync

Unattended two-way bridge between your course folder in iCloud Drive and **Goodnotes** on the iPad:

- **In:** every new slide deck or exercise sheet that lands in `<materials>/<course>/` (for example from [aula-sync](https://github.com/Space0code/aula-sync)) is imported into Goodnotes automatically, named `RL · Lecture05`. PowerPoint files are converted to PDF first.
- **Out:** your annotated notebooks come back as PDFs in `<materials>/<course>/handwritten/<name>.pdf`, after every lecture and every evening at 20:00. Only notebooks that changed are downloaded, and each one replaces its previous version in place, so large handwritten PDFs never pile up as copies.

```
Aula Global ──aula-sync──▶ materials/RL/lectures/L5_auto.pdf          (originals, never touched)
                                   │ new file (checked every 5 min)
                                   ▼
                     Gmail ──▶ Email to Goodnotes ──▶ Goodnotes: "RL · L5"   ✍️ you write on the iPad
                                                            │ Auto Backup (PDF)
                                                            ▼
materials/RL/handwritten/L5.pdf ◀──(changed only)── Google Drive for desktop: My Drive/Goodnotes
             after each lecture (+15, +60 min) and daily at 20:00
```

## Why it works this way
Goodnotes has no export API: its Shortcuts actions can create notebooks, add pages, open or search, but not export, and its iCloud sync stores notebooks in CloudKit, where no other app can read them. Two built-in features are scriptable from the outside, and this tool uses both:

| Direction | Goodnotes feature | What this tool does |
|---|---|---|
| into Goodnotes | **Email to Goodnotes**: PDFs mailed to your personal `…@goodnotes.email` address are imported | sends new files from your Gmail via SMTP (app password in Keychain) |
| out of Goodnotes | **Auto Backup** (iPad only): uploads a PDF of every notebook to Google Drive whenever it changes, mirroring your folders | reads the backup through Google Drive for desktop's local folder, and copies only notebooks whose size or modification time changed |

Google Drive is read through Google's own desktop app, not the Drive API. Its sign-in doesn't expire, and there's no OAuth client to create or renew; rclone's shared client ID is being retired in 2026, and a self-made one in "Testing" mode needs signing in again every week.

## Which notebook goes where
A notebook belongs to a course when:
1. it sits anywhere inside a Goodnotes folder named like the course: the materials folder name, the prefix, or a `goodnotes_folders` entry, such as the full course name (case and `_`/`-`/space differences are ignored). Subfolders are kept and the course prefix is dropped from the file name, so `EMAI/Reinforcement learning/Lectures/RL · L5` goes to `reinforcement_learning/handwritten/Lectures/L5.pdf`. Courses where you only write handwritten notebooks (no slides) work this way; or
2. it is outside any course folder (an import still in the library root) and its name starts with the course prefix and a separator (`RL · Lecture05`, `RL - Lecture 6`, `RL: notes`). It then goes to `reinforcement_learning/handwritten/Lecture05.pdf`.

When you move a notebook in Goodnotes, from the root into `EMAI/<course>/…` or between subfolders, its PDF moves too. If it hasn't changed, the existing file is just moved, with no download. If it has, the new version is written and the old copy is removed, but only if it is still exactly what the tool wrote. So moving notebooks never leaves duplicates.

Notebooks that match neither rule (personal notebooks, for example) are never downloaded. `status` lists them.

## Safety
- It writes **only** inside `<materials>/<course>/handwritten/`; any other target raises an error. Aula originals are never written to.
- An exported PDF is replaced only if the file on disk is still exactly what was last written there. If you put your own `L5.pdf` in `handwritten/` or edit one on the Mac, the export goes to `L5 (2).pdf` instead.
- Downloads go to a temporary file first, are verified against Google Drive's MD5, and are then moved into place atomically.
- Deleting or renaming a notebook in Goodnotes never deletes anything on the Mac.
- On the first run, files already in the materials folder are only recorded and **not** imported, so your library gets no duplicates. Use `backfill <course>` if you want them. If any folder can't be read, the run stops instead of recording an incomplete baseline.
- If aula-sync replaces a slide deck with a new version, the new version is imported as `RL · L5 (updated 22 Oct)`, so your annotated copy is left alone.
- **Compression without losing detail.** Every imported slide deck and every exported notebook is recompressed with Ghostscript's `/prepress` profile, with colours left unchanged and existing JPEGs passed through untouched. Handwriting, text and diagrams are vectors and stay vectors, so this step is lossless for them. Embedded images are downsampled only if they exceed 450 dpi, and then to 300 dpi. The result is kept only if it is at least 10% smaller and has the same page count; otherwise the original is used. On real files: handwritten Goodnotes exports −32% (8.7 → 5.9 MB) with pixel-identical ink at 300 dpi; image-heavy slides −40–60% (19.8 → 7.5 MB). Only a file that is still over 18 MB (Gmail's limit after encoding) gets a stronger 150 dpi pass, as a last resort before you're asked to import it by hand. Set `"compress": false` to turn recompression off.

## Requirements
- macOS with the Xcode Command Line Tools (`/usr/bin/python3` 3.9+, standard library only, and `cc` to build the launcher).
- `brew install ghostscript`.
- [Google Drive for desktop](https://www.google.com/drive/download/) (`brew install --cask google-drive`), signed in with the Google account Goodnotes backs up to. The default "Stream files" mode is fine.
- Microsoft PowerPoint, only for `.ppt`, `.pptx`, `.pps` and `.ppsx` files. It is opened in the background for the conversion and closed again.
- A Goodnotes plan that includes Email to Goodnotes, and Goodnotes on an iPad (Auto Backup isn't available on the Mac).
- A Gmail account with an app password in Keychain (the same item aula-sync uses, `aula-sync-gmail`). Optional: an ntfy topic (`aula-sync-ntfy`) for push notifications; without one, notifications go to your Gmail.

## Setup
1. **iPad, Goodnotes → Settings → Email to Goodnotes:** turn it on and copy your address.
2. **iPad, Goodnotes → Settings → Cloud & Backup → Auto Backup:** turn it on, choose **Google Drive**, set the file format to **PDF only** and keep the folder name **Goodnotes**. Keep the app open until the first full upload finishes. Keep iCloud sync on as well.
   - Why PDF only: this tool needs PDFs, and the Goodnotes format can't be read outside Goodnotes. Your editable library is already in iCloud sync. Backing up both formats would roughly double Drive space and upload time for no gain here.
3. **Mac: install Google Drive for desktop and sign in once** (the installer asks for your Mac password):
   ```bash
   brew install --cask google-drive && open -a "Google Drive"
   ```
4. **Mac: install:**
   ```bash
   git clone https://github.com/Space0code/goodnotes-sync.git && cd goodnotes-sync
   ./install.sh   # first run creates ~/Library/Application Support/goodnotes-sync/settings.json
   ```
   Edit the settings (below) and run `./install.sh` again. It builds a small launcher binary and loads a launchd agent that runs it every 5 minutes.
5. **Mac: let the agent into iCloud Drive.** macOS keeps background jobs out of `~/Library/Mobile Documents` until you allow them. Right after installing, a dialog appears: *"goodnotes-sync" would like to access files in iCloud Drive*. Click **Allow**. A second dialog asks the same for **Google Drive**; allow that too. The permission belongs to the small launcher binary only, not to Python as a whole. If you missed or declined the dialog, turn it on under System Settings → Privacy & Security → Files & Folders → goodnotes-sync → iCloud Drive. Alternatively, add the launcher to Full Disk Access:
   ```bash
   open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
   open -R ~/Library/Application\ Support/goodnotes-sync/app/goodnotes-sync   # drag it into the list
   ```
   Without access, each tick stops before touching any files and pushes a reminder (at most once a day).
6. Check everything with `status`, and send one test file:
   ```bash
   A=~/Library/Application\ Support/goodnotes-sync/app/goodnotes_sync.py
   /usr/bin/python3 "$A" status
   /usr/bin/python3 "$A" test-import "<materials>/<course>/slides/L1_auto.pdf"
   ```
7. **Optional, recommended (iPad Shortcuts → Automation → Time of Day, 19:45, Run Immediately → Open App → Goodnotes).** iPadOS gives apps little background time, so the Auto Backup queue empties most reliably while Goodnotes is open. This makes sure the day's notes are uploaded before the 20:00 export. During lectures Goodnotes is open anyway.

The first time a PowerPoint file needs converting, macOS may ask whether `goodnotes-sync` may control Microsoft PowerPoint; click **OK**. If the conversion is refused, you get a push and can allow it later under Privacy & Security → Automation.

### Settings (`~/Library/Application Support/goodnotes-sync/settings.json`)
| Key | Meaning | Default |
|---|---|---|
| `materials` | the course folder root (`~` allowed) | — |
| `goodnotes_email` | your Email to Goodnotes address; empty turns imports off | `""` |
| `gmail` | Gmail address that sends the imports (and receives notifications if ntfy isn't set up) | — |
| `courses` | `{"<folder>": {"prefix": "RL", "goodnotes_folders": ["Reinforcement learning"], "exclude": false}}` | — |
| `import_folders` | subfolders of a course whose files are imported | `["slides", "lectures", "exercises"]` |
| `only_auto_files` | import only files aula-sync wrote (`*_auto.*`), not your own copies | `true` |
| `backup_folder` | local path of the Auto Backup folder; empty = auto-detect `~/Library/CloudStorage/GoogleDrive-*/My Drive/<backup_folder_name>` | `""` |
| `backup_folder_name` | the folder name set in Goodnotes' Auto Backup | `"Goodnotes"` |
| `export_after_lecture_minutes` | export this many minutes after each lecture ends | `[15, 60]` |
| `export_times` | daily export times (local) | `["20:00"]` |
| `calendar_url` | iCalendar feed of your lectures; defaults to aula-sync's `calendar_url` | aula-sync's |
| `notify` | push a summary after each import/export (errors are always pushed) | `true` |
| `import_since` | local time like `"2026-10-22T15:00"`: on the first run, files changed after it are imported rather than only recorded (covers the gap between installing and allowing access) | first run's time |
| `compress` | detail-preserving recompression of imported and exported PDFs | `true` |
| `name_separator`, `handwritten_folder` | `" · "`, `"handwritten"` | |
| `gmail_keychain_service`, `ntfy_keychain_service` | Keychain items to use | `aula-sync-gmail`, `aula-sync-ntfy` |

## Commands
```
goodnotes_sync.py status                       courses, pending imports, notebook → file mapping, next exports
goodnotes_sync.py import [--dry-run]           import new files now
goodnotes_sync.py export [--dry-run]           pull changed notebooks now
goodnotes_sync.py backfill COURSE [--dry-run]  also import a course's files from before the first run
goodnotes_sync.py test-import FILE             email one file to Goodnotes
goodnotes_sync.py test-ntfy                    send a test push
goodnotes_sync.py tick                         what launchd runs every 5 min
```
Logs: `~/Library/Logs/goodnotes-sync/goodnotes-sync.log`. State: `~/Library/Application Support/goodnotes-sync/state.json`.

## Limitations
- Imported files land in the root of your Goodnotes library: Email to Goodnotes can't target a folder, and Goodnotes offers no background way to move documents. The course prefix shows where each one belongs; move them into `EMAI/<course>/…` yourself, and the exported PDFs follow.
- Goodnotes fetches emailed imports when the app is open, usually within a minute or two.
- How quickly a change on the iPad shows up depends on when Goodnotes uploads it. That happens immediately while the app is open, and less predictably in the background (see setup step 6).
- Deletions and renames in Goodnotes are not mirrored (moves are). A renamed notebook is exported under its new name, and the old PDF stays.

## Development
```bash
/usr/bin/python3 -m unittest discover -s tests -v
```

## Uninstall
`./uninstall.sh` stops the agent and leaves your PDFs in place. Add `--purge` to also delete settings, state and logs.

## License
MIT
