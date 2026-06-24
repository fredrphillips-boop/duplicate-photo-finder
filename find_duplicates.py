#!/usr/bin/env python3
"""
Find duplicate / near-duplicate photos in a folder tree.

Usage:
    python3 find_duplicates.py /path/to/photos [--exclude DIRNAME ...] [--out DUPLICATES.md]
    python3 find_duplicates.py --from-cache scan_cache.json [--out DUPLICATES.md] [--include-similar]

Detects:
  1. Exact byte-identical files (sha256)
  2. Same photo saved as a different file (different size/resolution/format)
     via perceptual hash (dHash) + pixel-diff verification
  3. Visually similar but likely different shots (burst sequences) —
     only included with --include-similar
"""
import os, sys, hashlib, json, argparse
from collections import defaultdict
from PIL import Image

EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.gif', '.bmp', '.tif', '.tiff'}
THRESH = 1.5  # pixel-diff threshold (0-255) for "confirmed identical"


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


def scan(root, exclude):
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude]
        for f in filenames:
            if os.path.splitext(f)[1].lower() in EXTS:
                files.append(os.path.join(dirpath, f))

    print(f"Found {len(files)} images", file=sys.stderr)
    results = []
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
            results.append({'path': path, 'size': size, 'sha256': sha, 'dhash': dh, 'w': w, 'h': hgt})
        except Exception as e:
            print(f"ERROR {path}: {e}", file=sys.stderr)
        if (i + 1) % 100 == 0 or (i + 1) == len(files):
            pct = (i + 1) / len(files) * 100
            print(f"  ...{i + 1}/{len(files)} ({pct:.1f}%)", file=sys.stderr)
    return results


def analyze(data):
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
    return exact, perceptual_mixed


def verify(perceptual_mixed):
    results = []
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
        results.append({'group': g, 'diffs': diffs})
    return results


def write_report(out_path, total, exact, verified, include_similar=False):
    zero_byte = [g for g in exact if g[0]['size'] == 0]
    real_exact = [g for g in exact if g[0]['size'] > 0]

    confirmed, likely_different = [], []
    for r in verified:
        maxdiff = max(d for d in r['diffs'] if d is not None)
        (confirmed if maxdiff <= THRESH else likely_different).append((r['group'], maxdiff))

    lines = []
    lines.append("# Duplicate Photo Report")
    lines.append("")
    lines.append(f"Total images scanned: {total}")
    lines.append("")
    lines.append("Two detection methods used:")
    lines.append("1. **SHA256 hash** — byte-for-byte identical files (same size, same bytes).")
    lines.append("2. **Perceptual hash (dHash)** + 32x32 grayscale pixel-diff verification — finds the *same photo saved as a different file* (different resolution, compression, or format). A mean pixel-diff <= 1.5/255 was treated as confirmed visually identical; higher diffs were excluded as likely different shots from a burst sequence.")
    lines.append("")

    lines.append("## 1. Exact byte-identical duplicates (same file, copied)")
    lines.append("")
    lines.append(f"{len(real_exact)} groups.")
    lines.append("")
    for i, g in enumerate(real_exact, 1):
        lines.append(f"### Group E{i} — {fmt_size(g[0]['size'])}, {g[0]['w']}x{g[0]['h']} (identical, same size)")
        for item in g:
            lines.append(f"- {item['path']}")
        lines.append("")

    if zero_byte:
        lines.append("## 1b. Empty / corrupted files (0 bytes)")
        lines.append("")
        lines.append("These are NOT real duplicate photos — they are 0-byte placeholder/corrupted files that all hash the same because they're empty.")
        lines.append("")
        for g in zero_byte:
            for item in g:
                lines.append(f"- {item['path']}")
        lines.append("")

    lines.append("## 2. Same photo saved as a different file (different size/resolution/format) — visually confirmed identical")
    lines.append("")
    lines.append(f"{len(confirmed)} groups.")
    lines.append("")
    for i, (g, d) in enumerate(confirmed, 1):
        lines.append(f"### Group P{i} (pixel-diff {d:.2f}/255)")
        for item in g:
            lines.append(f"- {item['path']} — {fmt_size(item['size'])}, {item['w']}x{item['h']}")
        lines.append("")

    if include_similar:
        lines.append("## 3. Visually similar but NOT confirmed identical (likely different shots from a burst/series)")
        lines.append("")
        lines.append(f"{len(likely_different)} groups. Matching perceptual hashes (similar composition) but pixel-level differences too large to call them the same photo — most look like consecutive burst-mode shots. Listed for awareness only; **not** recommended for deletion.")
        lines.append("")
        for i, (g, d) in enumerate(likely_different, 1):
            lines.append(f"### Group S{i} (pixel-diff {d:.2f}/255)")
            for item in g:
                lines.append(f"- {item['path']} — {fmt_size(item['size'])}, {item['w']}x{item['h']}")
            lines.append("")

    with open(out_path, 'w') as f:
        f.write('\n'.join(lines))

    print(f"exact={len(real_exact)} zero_byte_groups={len(zero_byte)} confirmed_perceptual={len(confirmed)} likely_different={len(likely_different)}")
    print(f"Report written to {out_path}")


def save_cache(data, cache_path):
    with open(cache_path, 'w') as f:
        json.dump(data, f)
    print(f"Cache saved to {cache_path} ({len(data)} entries)", file=sys.stderr)


def load_cache(cache_path):
    with open(cache_path, 'r') as f:
        data = json.load(f)
    print(f"Loaded {len(data)} entries from cache", file=sys.stderr)
    return data


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('root', nargs='?', help='folder to scan')
    p.add_argument('--exclude', nargs='*', default=[], help='directory names to skip')
    p.add_argument('--out', default='DUPLICATES.md', help='output report path')
    p.add_argument('--cache', default='scan_cache.json', help='cache file for scan data')
    p.add_argument('--from-cache', metavar='FILE', help='skip scan, regenerate report from cached data')
    p.add_argument('--include-similar', action='store_true', help='include section 3 (visually similar but not identical)')
    args = p.parse_args()

    if args.from_cache:
        data = load_cache(args.from_cache)
    else:
        if not args.root:
            p.error("root folder required (or use --from-cache)")
        data = scan(args.root, set(args.exclude))
        save_cache(data, args.cache)

    exact, perceptual_mixed = analyze(data)
    verified = verify(perceptual_mixed)
    write_report(args.out, len(data), exact, verified, include_similar=args.include_similar)
