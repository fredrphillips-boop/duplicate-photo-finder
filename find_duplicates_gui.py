#!/usr/bin/env python3
"""
Duplicate Photo Finder — GUI version.
Standalone tkinter app that can be bundled with PyInstaller into a single .exe.
"""
import os
import sys
import platform
import subprocess
import hashlib
import json
import re
import time
import threading
import webbrowser
from collections import defaultdict
from pathlib import Path
from tkinter import (
    Tk, Frame, Label, Button, Entry, StringVar,
    Text, Scrollbar, filedialog, messagebox,
    END, DISABLED, NORMAL, RIGHT, LEFT, BOTH, Y, X, TOP, BOTTOM, W, E,
)
from tkinter.ttk import Progressbar

from PIL import Image

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_AVAILABLE = True
except ImportError:
    HEIC_AVAILABLE = False

VERSION = "1.0.10"


def output_dir():
    if getattr(sys, 'frozen', False):
        d = os.path.dirname(sys.executable)
    else:
        d = os.path.dirname(os.path.abspath(__file__))
    try:
        test_file = os.path.join(d, '.write_test')
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
        return d
    except OSError:
        docs = os.path.join(Path.home(), 'Documents', 'DuplicatePhotoFinder')
        os.makedirs(docs, exist_ok=True)
        return docs


def safe_name(folder_path):
    name = os.path.basename(os.path.normpath(folder_path))
    safe = ''.join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in name)
    return safe.strip() or 'scan'
EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.gif', '.bmp', '.tif', '.tiff'}
THRESH = 1.5
PLATFORM = platform.system()
Image.MAX_IMAGE_PIXELS = 200_000_000


class SleepInhibitor:
    """Prevent OS sleep/suspend during long scans. Works on Windows, macOS, Linux."""

    def __init__(self):
        self._caffeinate_proc = None
        self._inhibit_proc = None

    def acquire(self):
        if PLATFORM == 'Windows':
            try:
                import ctypes
                ES_CONTINUOUS = 0x80000000
                ES_SYSTEM_REQUIRED = 0x00000001
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                )
            except Exception:
                pass
        elif PLATFORM == 'Darwin':
            try:
                self._caffeinate_proc = subprocess.Popen(
                    ['caffeinate', '-i', '-s'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
        else:
            try:
                self._inhibit_proc = subprocess.Popen(
                    ['systemd-inhibit', '--what=idle:sleep', '--who=DuplicatePhotoFinder',
                     '--why=Scanning photos', '--mode=block', 'sleep', 'infinity'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

    def release(self):
        if PLATFORM == 'Windows':
            try:
                import ctypes
                ES_CONTINUOUS = 0x80000000
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:
                pass
        elif PLATFORM == 'Darwin':
            if self._caffeinate_proc:
                self._caffeinate_proc.terminate()
                self._caffeinate_proc = None
        else:
            if self._inhibit_proc:
                self._inhibit_proc.terminate()
                self._inhibit_proc = None


def sleep_warning_message():
    if PLATFORM == 'Windows':
        return (
            "This scan may take a long time (over an hour for large photo libraries).\n\n"
            "The app will try to prevent your computer from sleeping automatically.\n\n"
            "To be safe, you can also change your power settings:\n"
            "  Settings → System → Power & sleep → set Sleep to \"Never\"\n"
            "  (on laptops: set both \"On battery\" and \"Plugged in\" to Never)\n\n"
            "Keep your laptop plugged in if possible.\n\n"
            "Start the scan?"
        )
    elif PLATFORM == 'Darwin':
        return (
            "This scan may take a long time (over an hour for large photo libraries).\n\n"
            "The app will try to prevent your Mac from sleeping automatically.\n\n"
            "To be safe, you can also change your settings:\n"
            "  System Settings → Displays → Advanced → "
            "turn off \"Automatically sleep display\"\n"
            "  Or: System Settings → Battery → "
            "set \"Turn display off after\" to Never\n\n"
            "Keep your laptop plugged in if possible.\n\n"
            "Start the scan?"
        )
    else:
        return (
            "This scan may take a long time (over an hour for large photo libraries).\n\n"
            "The app will try to prevent your computer from sleeping automatically.\n\n"
            "To be safe, you can also check your power settings:\n"
            "  Settings → Power → Automatic Suspend → set to Off\n\n"
            "Keep your laptop plugged in if possible.\n\n"
            "Start the scan?"
        )


def dhash(img, size=8):
    img = img.convert('L').resize((size + 1, size), Image.LANCZOS)
    pixels = list(img.getdata())
    bits = []
    for row in range(size):
        for col in range(size):
            left = pixels[row * (size + 1) + col]
            right = pixels[row * (size + 1) + col + 1]
            bits.append('1' if left > right else '0')
    return int(''.join(bits), 2)


def thumb_pixels(path, size=32):
    with Image.open(path) as img:
        return list(img.convert('L').resize((size, size), Image.LANCZOS).getdata())


def fmt_size(n):
    if n is None:
        return '?'
    for unit in ['B', 'KB', 'MB', 'GB']:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


ALBUM_KEYWORDS = ['album', 'favorites', 'favourites', 'best of', 'picks',
                   'selected', 'shared', 'collage', 'highlights', 'edited',
                   'copies', 'backup']
SEQUENCE_KEYWORDS = ['dcim', 'camera', 'camera roll', 'photo library',
                     'import', 'original', 'originals']


def recommend_keep_remove(group):
    """Score files in a duplicate group. Returns [(item, 'keep'|'remove', reason)]."""
    if len(group) < 2:
        return [(group[0], 'keep', 'Only copy')]

    scored = []
    for item in group:
        scored.append({'item': item, 'score': 0,
                       'keep_reasons': [], 'remove_reasons': []})

    max_size = max(s['item']['size'] for s in scored)
    min_size = min(s['item']['size'] for s in scored)
    if max_size != min_size:
        for s in scored:
            if s['item']['size'] == max_size:
                s['score'] += 3
                s['keep_reasons'].append('Largest file')
            else:
                s['remove_reasons'].append('Smaller file')

    has_res = all(s['item'].get('w') and s['item'].get('h') for s in scored)
    if has_res:
        pixels = [s['item']['w'] * s['item']['h'] for s in scored]
        max_px, min_px = max(pixels), min(pixels)
        if max_px != min_px:
            for s, px in zip(scored, pixels):
                if px == max_px:
                    s['score'] += 2
                    s['keep_reasons'].append('Highest resolution')
                else:
                    s['remove_reasons'].append('Lower resolution')

    for s in scored:
        path = s['item']['path']
        filename = os.path.splitext(os.path.basename(path))[0].lower()
        if re.search(r'[-_ ](copy|copia)(\s*\(\d+\))?$', filename) or \
           re.search(r'\(\d+\)$', filename):
            s['score'] -= 3
            s['remove_reasons'].append('Filename indicates copy')

        parent = os.path.basename(os.path.dirname(path)).lower()
        grandparent = os.path.basename(
            os.path.dirname(os.path.dirname(path))).lower()
        folder_ctx = parent + ' ' + grandparent

        if any(kw in folder_ctx for kw in ALBUM_KEYWORDS):
            s['score'] -= 2
            s['remove_reasons'].append('Album folder (likely copy)')
        if any(kw in folder_ctx for kw in SEQUENCE_KEYWORDS):
            s['score'] += 1
            s['keep_reasons'].append('Original sequence')
        elif re.search(r'20\d{2}[-_./]\d{2}', folder_ctx):
            s['score'] += 1
            s['keep_reasons'].append('Date-organized folder')

    mtimes = [s['item'].get('mtime', float('inf')) for s in scored]
    if len(set(mtimes)) > 1 and not all(m == float('inf') for m in mtimes):
        earliest = min(mtimes)
        for s, mt in zip(scored, mtimes):
            if mt == earliest:
                s['score'] += 1
                s['keep_reasons'].append('Earliest file date')
            elif mt != float('inf'):
                s['remove_reasons'].append('Later copy')

    max_score = max(s['score'] for s in scored)
    keep_assigned = False
    results = []
    for s in sorted(scored, key=lambda x: (-x['score'], x['item']['path'])):
        if s['score'] == max_score and not keep_assigned:
            reason = ', '.join(s['keep_reasons']) if s['keep_reasons'] else 'Best candidate'
            results.append((s['item'], 'keep', reason))
            keep_assigned = True
        else:
            reason = ', '.join(s['remove_reasons']) if s['remove_reasons'] else 'Redundant copy'
            results.append((s['item'], 'remove', reason))
    return results


class DuplicateFinderApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"Duplicate Photo Finder v{VERSION}")
        self.root.geometry("700x600")
        self.root.minsize(600, 500)

        self.folder_var = StringVar()
        self.running = False
        self.sleep_inhibitor = SleepInhibitor()
        self.report_path = None

        self._build_ui()

    def _build_ui(self):
        # --- Folder selection ---
        folder_frame = Frame(self.root, padx=10, pady=10)
        folder_frame.pack(fill=X, side=TOP)

        Label(folder_frame, text="Photos folder:").pack(side=LEFT)
        Entry(folder_frame, textvariable=self.folder_var).pack(
            side=LEFT, fill=X, expand=True, padx=(5, 5)
        )
        Button(folder_frame, text="Browse...", command=self._browse).pack(side=RIGHT)

        # --- Options ---
        opts_frame = Frame(self.root, padx=10)
        opts_frame.pack(fill=X, side=TOP, anchor=W)

        heic_label = "HEIC support: available" if HEIC_AVAILABLE else "HEIC support: not available (pillow-heif not installed)"
        Label(opts_frame, text=heic_label,
              fg="green" if HEIC_AVAILABLE else "red").pack(anchor=W, pady=2)

        # --- Buttons ---
        btn_frame = Frame(self.root, padx=10, pady=5)
        btn_frame.pack(fill=X, side=TOP)

        self.scan_btn = Button(
            btn_frame, text="Find Duplicates", command=self._start_scan,
            bg="#2196F3", fg="white", font=("Arial", 11, "bold"),
            padx=20, pady=5,
        )
        self.scan_btn.pack(side=LEFT)

        self.open_btn = Button(
            btn_frame, text="Open Report", command=self._open_report,
            state=DISABLED, padx=10, pady=5,
        )
        self.open_btn.pack(side=LEFT, padx=(10, 0))

        # --- Progress ---
        prog_frame = Frame(self.root, padx=10, pady=5)
        prog_frame.pack(fill=X, side=TOP)

        self.progress = Progressbar(prog_frame, mode='determinate')
        self.progress.pack(fill=X)

        self.status_var = StringVar(value="Ready — select a folder and click Find Duplicates")
        Label(prog_frame, textvariable=self.status_var, anchor=W).pack(fill=X, pady=2)

        self.phase_var = StringVar(value="")
        self.phase_label = Label(
            prog_frame, textvariable=self.phase_var, anchor=W,
            font=("Arial", 9, "italic"), fg="#666666",
        )
        self.phase_label.pack(fill=X)

        # --- Log ---
        log_frame = Frame(self.root, padx=10, pady=10)
        log_frame.pack(fill=BOTH, expand=True, side=TOP)

        scrollbar = Scrollbar(log_frame)
        scrollbar.pack(side=RIGHT, fill=Y)

        self.log = Text(log_frame, height=12, state=DISABLED, wrap='word',
                        yscrollcommand=scrollbar.set, font=("Consolas", 9))
        self.log.pack(fill=BOTH, expand=True)
        scrollbar.config(command=self.log.yview)

        # --- Footer ---
        footer_frame = Frame(self.root, padx=10, pady=5)
        footer_frame.pack(fill=X, side=BOTTOM)

        Label(
            footer_frame,
            text=f"© {time.strftime('%Y')} Fred R Phillips. All rights reserved.  |  v{VERSION}",
            font=("Arial", 9), fg="#555555",
        ).pack(expand=True)

    def _browse(self):
        folder = filedialog.askdirectory(title="Select photos folder")
        if folder:
            self.folder_var.set(folder)

    def _log(self, msg):
        self.root.after(0, self._log_thread_safe, msg)

    def _log_thread_safe(self, msg):
        self.log.config(state=NORMAL)
        self.log.insert(END, msg + "\n")
        self.log.see(END)
        self.log.config(state=DISABLED)

    def _set_progress(self, value, maximum=100):
        self.root.after(0, self._set_progress_safe, value, maximum)

    def _set_progress_safe(self, value, maximum):
        self.progress['maximum'] = maximum
        self.progress['value'] = value

    def _set_status(self, msg):
        self.root.after(0, self.status_var.set, msg)

    def _set_phase(self, msg):
        self.root.after(0, self.phase_var.set, msg)

    def _set_indeterminate(self, on):
        self.root.after(0, self._set_indeterminate_safe, on)

    def _set_indeterminate_safe(self, on):
        if on:
            self.progress.config(mode='indeterminate')
            self.progress.start(15)
        else:
            self.progress.stop()
            self.progress.config(mode='determinate')

    def _start_scan(self):
        folder = self.folder_var.get().strip()
        if not folder:
            messagebox.showwarning("No folder", "Please select a photos folder first.")
            return
        if not os.path.isdir(folder):
            messagebox.showerror("Invalid folder", f"Folder not found:\n{folder}")
            return
        if self.running:
            return

        if not messagebox.askyesno("Before you start", sleep_warning_message()):
            return

        self.running = True
        self.sleep_inhibitor.acquire()
        self.scan_btn.config(state=DISABLED)
        self.open_btn.config(state=DISABLED)
        self.log.config(state=NORMAL)
        self.log.delete('1.0', END)
        self.log.config(state=DISABLED)

        thread = threading.Thread(target=self._run_scan, args=(folder,), daemon=True)
        thread.start()

    def _run_scan(self, folder):
        try:
            self._do_scan(folder)
        except Exception as e:
            self._log(f"ERROR: {e}")
            self._set_status(f"Failed: {e}")
            self._set_phase("")
            self._set_indeterminate(False)
        finally:
            self.sleep_inhibitor.release()
            self.running = False
            self.root.after(0, lambda: self.scan_btn.config(state=NORMAL))

    def _do_scan(self, folder):
        # ── Phase 1: Collect image paths ──
        self._set_indeterminate(True)
        self._set_status("Step 1 of 4: Discovering image files — this may take a minute...")
        self._set_phase("Walking all folders to find photos. Please be patient.")
        self._log("Searching for image files (this can take a while on large libraries)...")
        files = []
        dirs_walked = 0
        for dirpath, dirnames, filenames in os.walk(folder):
            dirs_walked += 1
            for f in filenames:
                if os.path.splitext(f)[1].lower() in EXTS:
                    files.append(os.path.join(dirpath, f))
            if dirs_walked % 50 == 0:
                self._set_status(
                    f"Step 1 of 4: Discovering files — {len(files)} images found "
                    f"in {dirs_walked} folders so far..."
                )

        total = len(files)
        self._set_indeterminate(False)
        self._log(f"Found {total} images in {dirs_walked} folders")
        if total == 0:
            self._set_status("No images found in selected folder")
            self._set_phase("")
            return

        # Estimate time
        est_minutes = max(1, total // 200)
        if est_minutes >= 60:
            est_str = f"roughly {est_minutes // 60}h {est_minutes % 60}m"
        else:
            est_str = f"roughly {est_minutes} minutes"
        self._log(f"Estimated time: {est_str} (depends on disk speed and image sizes)")
        self._set_phase(
            f"Each image is read, hashed, and fingerprinted. "
            f"Estimated time: {est_str}."
        )

        # ── Phase 2: Hash and fingerprint every image ──
        self._set_status(f"Step 2 of 4: Scanning images — 0/{total}")
        scan_start = time.time()
        data = []
        errors = 0
        for i, path in enumerate(files):
            try:
                size = os.path.getsize(path)
                mtime = os.path.getmtime(path)
                h = hashlib.sha256()
                with open(path, 'rb') as fh:
                    for chunk in iter(lambda: fh.read(65536), b''):
                        h.update(chunk)
                sha = h.hexdigest()
                try:
                    with Image.open(path) as img:
                        img_w, img_h = img.size
                        if img_w * img_h > 200_000_000:
                            dh, w, hgt = None, img_w, img_h
                        else:
                            dh = dhash(img)
                            w, hgt = img_w, img_h
                except Exception:
                    dh, w, hgt = None, None, None
                data.append({'path': path, 'size': size, 'mtime': mtime,
                             'sha256': sha, 'dhash': dh, 'w': w, 'h': hgt})
            except Exception as e:
                errors += 1
                if errors <= 20:
                    self._log(f"ERROR {path}: {e}")
                elif errors == 21:
                    self._log("(suppressing further errors — see log at end)")

            done = i + 1
            if done % 50 == 0 or done == total:
                pct = done / total * 100
                self._set_progress(done, total)
                elapsed = time.time() - scan_start
                rate = done / elapsed if elapsed > 0 else 0
                remaining = (total - done) / rate if rate > 0 else 0
                if remaining >= 3600:
                    eta_str = f"{remaining / 3600:.1f}h remaining"
                elif remaining >= 60:
                    eta_str = f"{remaining / 60:.0f}m remaining"
                else:
                    eta_str = f"{remaining:.0f}s remaining"
                self._set_status(
                    f"Step 2 of 4: Scanning images — {done}/{total} ({pct:.0f}%) — {eta_str}"
                )

        # Save cache next to app
        out_dir = output_dir()
        folder_label = safe_name(folder)
        cache_path = os.path.join(out_dir, f"scan_cache_{folder_label}.json")
        with open(cache_path, 'w') as f:
            json.dump(data, f)
        self._log(f"Cache saved: {cache_path}")

        elapsed_total = time.time() - scan_start
        if elapsed_total >= 60:
            self._log(f"Scan completed in {elapsed_total / 60:.1f} minutes")
        else:
            self._log(f"Scan completed in {elapsed_total:.0f} seconds")
        if errors > 0:
            self._log(f"{errors} files had errors and were skipped")

        # ── Phase 3: Analyze hashes ──
        self._set_status("Step 3 of 4: Analyzing hashes for duplicates — working...")
        self._set_phase("Comparing fingerprints. Still working, please wait...")
        self._log("Analyzing hashes...")

        sha_groups = defaultdict(list)
        for d in data:
            sha_groups[d['sha256']].append(d)
        exact = [v for v in sha_groups.values() if len(v) > 1]

        dhash_groups = defaultdict(list)
        for d in data:
            if d['dhash'] is not None:
                dhash_groups[d['dhash']].append(d)
        perceptual_mixed = []
        for v in dhash_groups.values():
            if len(v) > 1 and len(set(x['sha256'] for x in v)) > 1:
                perceptual_mixed.append(v)

        # ── Phase 4: Verify perceptual matches ──
        n_groups = len(perceptual_mixed)
        self._set_status(f"Step 4 of 4: Verifying visual matches — 0/{n_groups} groups...")
        self._set_phase("Pixel-comparing candidate groups. Still working...")
        self._log(f"Verifying {n_groups} candidate groups...")
        verified = []
        for gi, g in enumerate(perceptual_mixed):
            arrs = []
            for item in g:
                try:
                    arrs.append(thumb_pixels(item['path']))
                except Exception:
                    arrs.append(None)
            base = next((x for x in arrs if x is not None), None)
            diffs = []
            for x in arrs:
                if x is None or base is None:
                    diffs.append(None)
                else:
                    diffs.append(sum(abs(p1 - p2) for p1, p2 in zip(x, base)) / len(base))
            verified.append({'group': g, 'diffs': diffs})
            if (gi + 1) % 5 == 0 or (gi + 1) == n_groups:
                self._set_status(f"Step 4 of 4: Verifying visual matches — {gi + 1}/{n_groups} groups...")
        self._set_phase("Building report...")

        # Build report
        zero_byte = [g for g in exact if g[0]['size'] == 0]
        real_exact = [g for g in exact if g[0]['size'] > 0]

        confirmed, likely_different = [], []
        for r in verified:
            maxdiff = max(d for d in r['diffs'] if d is not None)
            (confirmed if maxdiff <= THRESH else likely_different).append((r['group'], maxdiff))

        dup_count = sum(len(g) - 1 for g in real_exact)
        perc_count = sum(len(g) - 1 for g, _ in confirmed)
        safe_delete = dup_count + perc_count
        sim_count = sum(len(g) - 1 for g, _ in likely_different)

        # Build HTML report
        def h(text):
            return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        html = []
        html.append('<!DOCTYPE html>')
        html.append('<html><head><meta charset="utf-8">')
        html.append(f'<title>Duplicate Photo Report — v{VERSION}</title>')
        html.append('<style>')
        html.append('body { font-family: Segoe UI, Arial, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; color: #222; line-height: 1.5; }')
        html.append('h1 { color: #1565C0; }')
        html.append('h2 { color: #1976D2; border-bottom: 2px solid #1976D2; padding-bottom: 4px; }')
        html.append('h3 { color: #1E88E5; }')
        html.append('.warning { color: #D32F2F; font-weight: bold; }')
        html.append('.safe { color: #2E7D32; font-weight: bold; }')
        html.append('.muted { color: #666; font-size: 0.9em; }')
        html.append('.summary-box { background: #E3F2FD; border-left: 4px solid #1976D2; padding: 12px 16px; margin: 16px 0; }')
        html.append('.warning-box { background: #FFEBEE; border-left: 4px solid #D32F2F; padding: 12px 16px; margin: 16px 0; }')
        html.append('ul { padding-left: 24px; }')
        html.append('li { margin: 2px 0; }')
        html.append('hr { border: none; border-top: 1px solid #ccc; margin: 24px 0; }')
        html.append('.keep { background: #E8F5E9; border-left: 3px solid #2E7D32; padding: 6px 10px; margin: 3px 0; }')
        html.append('.remove { background: #FFEBEE; border-left: 3px solid #D32F2F; padding: 6px 10px; margin: 3px 0; }')
        html.append('.keep-tag { background: #2E7D32; color: white; padding: 2px 8px; border-radius: 3px; font-size: 0.85em; font-weight: bold; margin-right: 6px; }')
        html.append('.remove-tag { background: #D32F2F; color: white; padding: 2px 8px; border-radius: 3px; font-size: 0.85em; font-weight: bold; margin-right: 6px; }')
        html.append('.reason { color: #666; font-size: 0.85em; font-style: italic; }')
        html.append('.legend { background: #F5F5F5; border: 1px solid #ddd; padding: 12px 16px; margin: 16px 0; border-radius: 4px; }')
        html.append('.footer { color: #888; font-size: 0.85em; margin-top: 40px; border-top: 1px solid #ccc; padding-top: 8px; }')
        html.append('</style></head><body>')

        html.append('<h1>Duplicate Photo Report</h1>')
        html.append(f'<p class="muted">Generated by Duplicate Photo Finder v{VERSION}<br>')
        html.append(f'&copy; {time.strftime("%Y")} Fred R Phillips. All rights reserved.</p>')
        html.append(f'<p>Total images scanned: <strong>{total}</strong></p>')

        html.append('<hr>')
        html.append('<h2>How to read this report</h2>')
        html.append('<p>This report has three sections. <span class="warning">NOT everything listed is safe to delete.</span><br>')
        html.append('Please read this guide before taking any action.</p>')

        html.append('<div class="summary-box">')
        html.append(f'<p><strong>Section 1 — Exact duplicates</strong> ({len(real_exact)} groups, {dup_count} extra copies)<br>')
        html.append('Byte-for-byte identical copies of the same file.<br>')
        html.append('<span class="safe">SAFE TO DELETE THE REDUNDANT FILE (SELECT CAREFULLY)</span> — keep one, delete the rest.</p>')
        html.append(f'<p><strong>Section 2 — Same photo, different file</strong> ({len(confirmed)} groups, {perc_count} extra copies)<br>')
        html.append('Same image saved at different size, quality, or format.<br>')
        html.append('<span class="safe">SAFE TO DELETE THE REDUNDANT FILE (SELECT CAREFULLY)</span> — keep the highest quality version.</p>')
        html.append('</div>')

        html.append('<div class="warning-box">')
        html.append(f'<p><strong>Section 3 — Similar but NOT identical</strong> ({len(likely_different)} groups, {sim_count} photos)<br>')
        html.append('Different photos that look alike — burst shots, retakes, similar compositions.<br>')
        html.append('<span class="warning">DO NOT DELETE — these are different photos.</span> Listed for your awareness only.</p>')
        html.append('</div>')

        html.append('<div class="legend">')
        html.append('<h3>Color Key</h3>')
        html.append('<p><span class="keep-tag">KEEP</span> Recommended to keep — best quality, original location, or earliest file date.</p>')
        html.append('<p><span class="remove-tag">REMOVE</span> Recommended to remove — redundant copy, lower quality, or album duplicate.</p>')
        html.append('<p class="muted">Recommendations are based on file size, resolution, folder location, and file date. Always verify before deleting.</p>')
        html.append('</div>')

        html.append(f'<p><strong>Bottom line:</strong> Sections 1 and 2 found <strong>{safe_delete} files</strong> that can be safely removed.<br>')
        html.append('Section 3 photos are NOT duplicates — review them manually if you wish.</p>')

        html.append('<hr>')
        html.append(f'<h2>1. <span class="safe">SAFE TO DELETE THE REDUNDANT FILE (SELECT CAREFULLY)</span> — Exact byte-identical duplicates</h2>')
        html.append(f'<p>These are exact copies of the same file. {len(real_exact)} groups found.<br>')
        html.append('Keep one copy from each group and delete the rest.</p>')
        for i, g in enumerate(real_exact, 1):
            html.append(f'<h3>Group E{i} — {fmt_size(g[0]["size"])}, {g[0]["w"]}x{g[0]["h"]}</h3>')
            recs = recommend_keep_remove(g)
            for item, action, reason in recs:
                tag = 'keep' if action == 'keep' else 'remove'
                label = 'KEEP' if action == 'keep' else 'REMOVE'
                html.append(f'<div class="{tag}"><span class="{tag}-tag">{label}</span> '
                            f'{h(item["path"])} '
                            f'<span class="reason">— {h(reason)}</span></div>')

        if zero_byte:
            html.append('<h2>1b. Empty / corrupted files (0 bytes)</h2>')
            html.append('<p>These are 0-byte placeholder/corrupted files, not real photos.</p>')
            html.append('<ul>')
            for g in zero_byte:
                for item in g:
                    html.append(f'<li>{h(item["path"])}</li>')
            html.append('</ul>')

        html.append(f'<h2>2. <span class="safe">SAFE TO DELETE THE REDUNDANT FILE (SELECT CAREFULLY)</span> — Same photo, different file</h2>')
        html.append(f'<p>Same image saved at different resolution, compression, or format. {len(confirmed)} groups found.<br>')
        html.append('Keep the highest quality version (usually the largest file) and delete the rest.</p>')
        for i, (g, d) in enumerate(confirmed, 1):
            html.append(f'<h3>Group P{i} <span class="muted">(pixel-diff {d:.2f}/255)</span></h3>')
            recs = recommend_keep_remove(g)
            for item, action, reason in recs:
                tag = 'keep' if action == 'keep' else 'remove'
                label = 'KEEP' if action == 'keep' else 'REMOVE'
                html.append(f'<div class="{tag}"><span class="{tag}-tag">{label}</span> '
                            f'{h(item["path"])} — {fmt_size(item["size"])}, {item["w"]}x{item["h"]} '
                            f'<span class="reason">— {h(reason)}</span></div>')

        html.append(f'<h2>3. <span class="warning">DO NOT DELETE</span> — Similar but NOT identical photos</h2>')
        html.append(f'<p>These are different photos that look similar — burst shots, retakes, or similar compositions. {len(likely_different)} groups found.<br>')
        html.append('<span class="warning">Listed for awareness only. Review manually if you want to thin out burst sequences, but these are NOT duplicates.</span></p>')
        for i, (g, d) in enumerate(likely_different, 1):
            html.append(f'<h3>Group S{i} <span class="muted">(pixel-diff {d:.2f}/255)</span></h3>')
            html.append('<ul>')
            for item in g:
                html.append(f'<li>{h(item["path"])} — {fmt_size(item["size"])}, {item["w"]}x{item["h"]}</li>')
            html.append('</ul>')

        html.append(f'<div class="footer" style="text-align: center;">')
        html.append(f'&copy; {time.strftime("%Y")} Fred R Phillips. All rights reserved. | v{VERSION}</div>')
        html.append('</body></html>')

        report_path = os.path.join(out_dir, f"DUPLICATES_{folder_label}.html")
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(html))

        self.report_path = report_path
        self.root.after(0, lambda: self.open_btn.config(state=NORMAL))

        summary = (f"Complete! {len(real_exact)} exact duplicates, "
                   f"{len(confirmed)} perceptual matches, "
                   f"{len(likely_different)} similar-but-different")
        self._log(summary)
        self._log(f"Report saved: {report_path}")
        self._log("Click 'Open Report' to view results in your browser.")
        self._set_status(summary)
        self._set_phase("")
        self._set_progress(total, total)

    def _open_report(self):
        if self.report_path and os.path.exists(self.report_path):
            url = Path(self.report_path).resolve().as_uri()
            webbrowser.open(url)


def main():
    root = Tk()
    DuplicateFinderApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
