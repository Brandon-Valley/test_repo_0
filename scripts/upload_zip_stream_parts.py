#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--prefix', required=True)
    parser.add_argument('--chunk-size', type=int, default=471859200)
    parser.add_argument('--manifest', required=True)
    args = parser.parse_args()

    overall = hashlib.sha256()
    parts = []
    index = 0
    total = 0
    stream = sys.stdin.buffer

    while True:
        name = f'{args.prefix}.part-{index:03d}'
        path = Path(tempfile.gettempdir()) / name
        part_hash = hashlib.sha256()
        written = 0
        with path.open('wb', buffering=64 * 1024 * 1024) as handle:
            while written < args.chunk_size:
                block = stream.read(min(64 * 1024 * 1024, args.chunk_size - written))
                if not block:
                    break
                handle.write(block)
                part_hash.update(block)
                overall.update(block)
                written += len(block)
                total += len(block)
        if written == 0:
            path.unlink(missing_ok=True)
            break
        subprocess.run([
            'gh', 'release', 'upload', args.tag, str(path),
            '--repo', args.repo, '--clobber'
        ], check=True)
        parts.append({'index': index, 'name': name, 'bytes': written, 'sha256': part_hash.hexdigest()})
        print(f'uploaded {name} {written} bytes', flush=True)
        path.unlink(missing_ok=True)
        index += 1
        if written < args.chunk_size:
            break

    manifest = {
        'format': 'ordered binary chunks of one ZIP file',
        'zip_filename': args.prefix,
        'repository': args.repo,
        'release_tag': args.tag,
        'chunk_size': args.chunk_size,
        'total_bytes': total,
        'zip_sha256': overall.hexdigest(),
        'part_count': len(parts),
        'parts': parts,
        'reassembly': 'Concatenate parts in numeric order without modifying them.',
    }
    Path(args.manifest).write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == '__main__':
    main()
