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
import time
import threading
import webbrowser
from collections import defaultdict
from pathlib import Path
from tkinter import (
    Tk, Frame, Label, Button, Entry, StringVar,
    Text, Scrollbar, filedialog, messagebox,
    END, DISABLED, NORMAL, RIGHT, LEFT, BOTH, Y, X, TOP, BOTTOM, W,
)
from tkinter.ttk import Progressbar

from PIL import Image

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_AVAILABLE = True
except ImportError:
    HEIC_AVAILABLE = False

EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.gif', '.bmp', '.tif', '.tiff'}
THRESH = 1.5
PLATFORM = platform.system()


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


class DuplicateFinderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Duplicate Photo Finder")
        self.root.geometry("700x550")
        self.root.minsize(600, 450)

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
              fg="green" if HEIC_AVAILABLE else "red").pack(anchor=W, pady=(2, 0))

        # --- Buttons ---
        btn_frame = Frame(self.root, padx=10, pady=5)
        btn_frame.pack(fill=X, side=TOP)

        self.scan_btn = Button(
            btn_frame, text="Find Duplicates", command=self._start_scan,
            bg="#4CAF50", fg="white", font=("Arial", 11, "bold"),
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
        Label(prog_frame, textvariable=self.status_var, anchor=W).pack(fill=X, pady=(2, 0))

        self.phase_var = StringVar(value="")
        self.phase_label = Label(
            prog_frame, textvariable=self.phase_var, anchor=W,
            font=("Arial", 9, "italic"), fg="#666666",
        )
        self.phase_label.pack(fill=X)

        # --- Log ---
        log_frame = Frame(self.root, padx=10, pady=(0, 10))
        log_frame.pack(fill=BOTH, expand=True, side=TOP)

        scrollbar = Scrollbar(log_frame)
        scrollbar.pack(side=RIGHT, fill=Y)

        self.log = Text(log_frame, height=12, state=DISABLED, wrap='word',
                        yscrollcommand=scrollbar.set, font=("Consolas", 9))
        self.log.pack(fill=BOTH, expand=True)
        scrollbar.config(command=self.log.yview)

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
        for i, path in enumerate(files):
            try:
                size = os.path.getsize(path)
                h = hashlib.sha256()
                with open(path, 'rb') as fh:
                    for chunk in iter(lambda: fh.read(65536), b''):
                        h.update(chunk)
                sha = h.hexdigest()
                try:
                    with Image.open(path) as img:
                        dh = dhash(img)
                        w, hgt = img.size
                except Exception:
                    dh, w, hgt = None, None, None
                data.append({'path': path, 'size': size, 'sha256': sha,
                             'dhash': dh, 'w': w, 'h': hgt})
            except Exception as e:
                self._log(f"ERROR {path}: {e}")

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

        # Save cache next to report
        cache_path = os.path.join(folder, "scan_cache.json")
        with open(cache_path, 'w') as f:
            json.dump(data, f)
        self._log(f"Cache saved: {cache_path}")

        elapsed_total = time.time() - scan_start
        if elapsed_total >= 60:
            self._log(f"Scan completed in {elapsed_total / 60:.1f} minutes")
        else:
            self._log(f"Scan completed in {elapsed_total:.0f} seconds")

        # ── Phase 3: Analyze hashes ──
        self._set_status("Step 3 of 4: Analyzing hashes for duplicates...")
        self._set_phase("Comparing fingerprints — usually quick.")
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
        self._set_status("Step 4 of 4: Verifying visual matches...")
        self._set_phase(f"Pixel-comparing {len(perceptual_mixed)} candidate groups.")
        verified = []
        for g in perceptual_mixed:
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

        lines = []
        lines.append("# Duplicate Photo Report")
        lines.append("")
        lines.append(f"Total images scanned: **{total}**")
        lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("## How to read this report")
        lines.append("")
        lines.append("This report has **three sections**. Not everything listed is safe to delete.")
        lines.append("Read this guide before taking any action.")
        lines.append("")
        lines.append(f"| Section | What it means | Groups | Safe to delete? |")
        lines.append(f"|---------|--------------|--------|----------------|")
        lines.append(f"| **1. Exact duplicates** | Byte-for-byte identical copies of the same file | {len(real_exact)} groups ({dup_count} extra copies) | **YES** — keep one, delete the rest |")
        lines.append(f"| **2. Same photo, different file** | Same image saved at different size/quality/format | {len(confirmed)} groups ({perc_count} extra copies) | **YES** — keep the highest quality version |")
        lines.append(f"| **3. Similar but NOT identical** | Different photos that look alike (burst shots, retakes) | {len(likely_different)} groups ({sim_count} photos) | **NO** — review manually, these are different photos |")
        lines.append("")
        lines.append(f"**Bottom line:** Sections 1 and 2 contain **{safe_delete} files** that can be safely removed.")
        lines.append(f"Section 3 is for your awareness only — those are different photos that happen to look similar.")
        lines.append("")
        lines.append("---")
        lines.append("")

        lines.append("## 1. SAFE TO DELETE — Exact byte-identical duplicates")
        lines.append("")
        lines.append(f"These are exact copies of the same file. {len(real_exact)} groups found.")
        lines.append("Keep one copy from each group and delete the rest.")
        lines.append("")
        for i, g in enumerate(real_exact, 1):
            lines.append(f"### Group E{i} — {fmt_size(g[0]['size'])}, {g[0]['w']}x{g[0]['h']}")
            for item in g:
                lines.append(f"- {item['path']}")
            lines.append("")

        if zero_byte:
            lines.append("## 1b. Empty / corrupted files (0 bytes)")
            lines.append("")
            lines.append("These are 0-byte placeholder/corrupted files, not real photos.")
            lines.append("")
            for g in zero_byte:
                for item in g:
                    lines.append(f"- {item['path']}")
            lines.append("")

        lines.append("## 2. SAFE TO DELETE — Same photo, different file")
        lines.append("")
        lines.append(f"Same image saved at different resolution, compression, or format. {len(confirmed)} groups found.")
        lines.append("Keep the highest quality version (usually the largest file) and delete the rest.")
        lines.append("")
        for i, (g, d) in enumerate(confirmed, 1):
            lines.append(f"### Group P{i} (pixel-diff {d:.2f}/255)")
            for item in g:
                lines.append(f"- {item['path']} — {fmt_size(item['size'])}, {item['w']}x{item['h']}")
            lines.append("")

        lines.append("## 3. DO NOT DELETE — Similar but NOT identical photos")
        lines.append("")
        lines.append(f"These are **different photos** that look similar — burst shots, retakes, or similar compositions. {len(likely_different)} groups found.")
        lines.append("Listed for awareness only. Review manually if you want to thin out burst sequences,")
        lines.append("but these are NOT duplicates.")
        lines.append("")
        for i, (g, d) in enumerate(likely_different, 1):
            lines.append(f"### Group S{i} (pixel-diff {d:.2f}/255)")
            for item in g:
                lines.append(f"- {item['path']} — {fmt_size(item['size'])}, {item['w']}x{item['h']}")
            lines.append("")

        report_path = os.path.join(folder, "DUPLICATES.md")
        with open(report_path, 'w') as f:
            f.write('\n'.join(lines))

        self.report_path = report_path
        self.root.after(0, lambda: self.open_btn.config(state=NORMAL))

        summary = (f"Complete! {len(real_exact)} exact duplicates, "
                   f"{len(confirmed)} perceptual matches, "
                   f"{len(likely_different)} similar-but-different")
        self._log(summary)
        self._log(f"Report saved: {report_path}")
        self._log("Click 'Open Report' to view results.")
        self._set_status(summary)
        self._set_phase("")
        self._set_progress(total, total)

    def _open_report(self):
        if self.report_path and os.path.exists(self.report_path):
            webbrowser.open(self.report_path)


def main():
    root = Tk()
    DuplicateFinderApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
