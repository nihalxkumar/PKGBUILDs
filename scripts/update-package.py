#!/usr/bin/env python3
"""Update a PKGBUILD to a new version.

Reads the PKGBUILD, determines the package type, downloads new sources
to compute sha256sums, and updates pkgver + sha256sums in place.

Usage:
    scripts/update-package.py <package_dir> <new_version>

Examples:
    scripts/update-package.py ensu-bin 0.1.17
    scripts/update-package.py smithery-cli 1.2.0
    scripts/update-package.py onionspray 1.8.0
"""

import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


def parse_pkgbuild(pkgbuild_path: str) -> dict:
    """Parse a PKGBUILD file and extract key fields."""
    content = Path(pkgbuild_path).read_text()

    def extract_var(name: str) -> str | None:
        # Match both var="value" and var='value'
        m = re.search(rf'^{name}=(["\'])(.*?)\1', content, re.MULTILINE)
        if m:
            return m.group(2)
        # Match unquoted: var=value (stop at whitespace or comment)
        m = re.search(rf'^{name}=([^\s#]+)', content, re.MULTILINE)
        if m:
            return m.group(1)
        return None

    # Check for pkgver() function — auto-generated version
    has_pkgver_func = bool(re.search(r'^pkgver\(\)', content, re.MULTILINE))

    # Check sha256sums
    has_skip = bool(re.search(r"sha256sums=\(['\"]SKIP['\"]\)", content))
    has_arch_skip = bool(re.search(r"sha256sums_\w+=\(['\"]SKIP['\"]\)", content))

    # Extract source arrays (handle multi-line). Arch-specific arrays such as
    # source_x86_64/source_aarch64 are kept in separate groups so each arch
    # gets its own sha256sum later.
    source_groups = {}
    lines = content.splitlines()
    idx = 0
    while idx < len(lines):
        stripped = lines[idx].strip()
        array_match = re.match(r'source(_\w+)?=\(', stripped)
        if not array_match:
            idx += 1
            continue
        suffix = array_match.group(1) or ''
        block = stripped
        depth = stripped.count('(') - stripped.count(')')
        while depth > 0 and idx + 1 < len(lines):
            idx += 1
            stripped = lines[idx].strip()
            block += " " + stripped
            depth += stripped.count('(') - stripped.count(')')
        inner = re.search(r'source(?:_\w+)?=\((.*)\)', block, re.DOTALL)
        tokens = []
        if inner:
            tokens = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', inner.group(1))
        source_groups[suffix] = [t.strip('"').strip("'") for t in tokens]
        idx += 1

    # Resolve variable references in sources
    variables = {}
    for name in ['_pkgname', '_url', 'url', 'pkgver', 'pkgname']:
        val = extract_var(name)
        if val:
            variables[name] = val

    resolved_groups = {}
    for suffix, entries in source_groups.items():
        resolved = []
        for src in entries:
            item = src
            for var_name, var_val in variables.items():
                item = item.replace(f'${{{var_name}}}', var_val)
                item = item.replace(f'${var_name}', var_val)
            resolved.append(item)
        resolved_groups[suffix] = resolved

    # Flatten groups for callers that only need "any source".
    sources = [s for entries in source_groups.values() for s in entries]
    resolved_sources = [s for entries in resolved_groups.values() for s in entries]

    # Determine source URL pattern
    source_url = None
    is_git = False
    is_binary = False
    tag_prefix = 'v'

    for src in resolved_sources:
        if src.startswith('git+'):
            is_git = True
            if '#tag=' in src:
                tag_match = re.search(r'#tag=(.+?)(?:&|$)', src)
                if tag_match:
                    tag_val = tag_match.group(1)
                    if tag_val.startswith('v'):
                        tag_prefix = 'v'
                    else:
                        tag_prefix = ''
            source_url = src
            break
        elif 'github.com' in src or 'gitlab' in src:
            source_url = src
            if '/releases/download/' in src:
                is_binary = True
            break

    return {
        'pkgver': extract_var('pkgver'),
        'pkgrel': extract_var('pkgrel') or '1',
        'pkgname': extract_var('pkgname'),
        '_pkgname': extract_var('_pkgname'),
        'url': extract_var('url'),
        'variables': variables,
        'has_pkgver_func': has_pkgver_func,
        'has_skip_sha256': has_skip or has_arch_skip,
        'sources': sources,
        'resolved_sources': resolved_sources,
        'source_groups': source_groups,
        'resolved_groups': resolved_groups,
        'source_url': source_url,
        'is_git': is_git,
        'is_binary': is_binary,
        'tag_prefix': tag_prefix,
        'content': content,
    }


def format_file_size(size_bytes: int) -> str:
    s = float(size_bytes)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if s < 1024:
            return f"{s:.2f} {unit}"
        s /= 1024
    return f"{s:.2f} TB"


def extract_expected_targets(pkgbuild_content: str) -> set[str]:
    """Extract expected files/destinations referenced in PKGBUILD."""
    targets = set()
    in_heredoc = False
    heredoc_delimiter = ''

    # Normalize shell line continuations (\ followed by newline)
    normalized_content = re.sub(r'\\\s*\n', ' ', pkgbuild_content)

    for line in normalized_content.splitlines():
        stripped = line.strip()

        # Handle heredoc blocks: ignore content generated within heredocs
        if in_heredoc:
            if stripped == heredoc_delimiter:
                in_heredoc = False
                heredoc_delimiter = ''
            continue

        heredoc_match = re.search(r"<<\s*['\"]?([a-zA-Z0-9_]+)['\"]?", stripped)
        if heredoc_match:
            in_heredoc = True
            heredoc_delimiter = heredoc_match.group(1)
            continue

        # Skip lines that generate files from stdin or echo/cat
        if '/dev/stdin' in stripped or 'cat >' in stripped:
            continue

        # Scan install commands: only track source files, never destinations ($pkgdir)
        if stripped.startswith('install '):
            parts = [p.strip('\"\'') for p in stripped.split() if p]
            args = [p for p in parts[1:] if not p.startswith('-')]
            if len(args) >= 2:
                # args[:-1] are sources, args[-1] is destination
                for src in args[:-1]:
                    if 'pkgdir' not in src and src != '/dev/stdin':
                        b = os.path.basename(src)
                        if b and not b.startswith('$') and '*' not in b:
                            targets.add(b)
            continue

        # Scan desktop-file-edit: modifies an extracted desktop file
        if 'desktop-file-edit' in stripped:
            for word in stripped.split():
                clean = word.strip('\"\'();,')
                if clean.endswith('.desktop'):
                    b = os.path.basename(clean)
                    if b and not b.startswith('$') and '*' not in b:
                        targets.add(b)
            continue

    return targets


def inspect_archive_members(file_path: str, filename: str) -> tuple[str, str, list[str]]:
    """Inspect the contents of an archive file.

    Returns:
        (format_name, format_details, list_of_file_paths)
    """
    # 1. Debian package (.deb)
    if filename.endswith('.deb'):
        res = subprocess.run(['ar', 't', file_path], capture_output=True, text=True)
        if res.returncode == 0:
            members = [m.strip() for m in res.stdout.splitlines() if m.strip()]
            data_tar = next((m for m in members if m.startswith('data.tar')), None)
            if data_tar:
                p = subprocess.run(['ar', 'p', file_path, data_tar], capture_output=True)
                files = []
                try:
                    with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tf:
                        files = [m.name for m in tf.getmembers() if not m.isdir()]
                except Exception:
                    p2 = subprocess.run(['tar', '-taf', '-'], input=p.stdout, capture_output=True)
                    if p2.returncode == 0:
                        out = p2.stdout.decode('utf-8', errors='replace')
                        files = [f.strip() for f in out.splitlines() if f.strip() and not f.endswith('/')]
                return 'deb', f'Debian package ({data_tar})', files

    # 2. Tar archives (.tar, .tar.gz, .tar.xz, .tar.zst, .tgz, etc.)
    try:
        with tarfile.open(file_path) as tf:
            files = [m.name for m in tf.getmembers() if not m.isdir()]
            return 'tar', 'Tar archive', files
    except Exception:
        pass

    # 3. Zip archives
    try:
        with zipfile.ZipFile(file_path) as zf:
            files = [n for n in zf.namelist() if not n.endswith('/')]
            return 'zip', 'Zip archive', files
    except Exception:
        pass

    # 4. Raw file / ELF binary
    res = subprocess.run(['file', '-b', file_path], capture_output=True, text=True)
    file_info = res.stdout.strip() or 'Binary file'
    return 'file', file_info, [filename]


def inspect_and_validate_asset(
    file_path: str,
    asset_name: str,
    pkgbuild_content: str,
    pkgname: str,
    new_ver: str,
    report_file: str | None = None
) -> dict:
    """Inspect downloaded asset, check against PKGBUILD, log output, and write report."""
    size_bytes = os.path.getsize(file_path)
    size_str = format_file_size(size_bytes)
    kind, details, files = inspect_archive_members(file_path, asset_name)

    expected_targets = extract_expected_targets(pkgbuild_content)
    matched_targets = []
    missing_targets = []

    for target in expected_targets:
        found = any(
            f == target or f.endswith('/' + target) or os.path.basename(f) == target
            for f in files
        )
        if found:
            matched_targets.append(target)
        else:
            missing_targets.append(target)

    format_warnings = []
    if kind == 'deb':
        if 'data.tar.gz' in pkgbuild_content and 'data.tar.gz' not in details:
            format_warnings.append(
                f"PKGBUILD references data.tar.gz, but deb contains {details}"
            )

    key_files = []
    for f in files:
        b = os.path.basename(f)
        if (
            f.startswith(('usr/bin/', 'bin/', 'opt/')) or
            b.endswith(('.desktop', '.service', '.png', '.svg')) or
            f in expected_targets or b in expected_targets
        ):
            key_files.append(f)

    if not key_files and len(files) <= 15:
        key_files = list(files)

    print(f"  [archive-check] {asset_name} ({size_str}, {details})")
    if key_files:
        print(f"    Key files ({len(key_files)} of {len(files)}):")
        for kf in key_files[:10]:
            print(f"      - {kf}")
        if len(key_files) > 10:
            print(f"      ... and {len(key_files) - 10} more")
    for mt in matched_targets:
        print(f"    ✓ Confirmed: '{mt}' found in archive")
    for wt in missing_targets:
        print(f"    ⚠️ Warning: '{wt}' expected by PKGBUILD but NOT found in archive!")
    for fw in format_warnings:
        print(f"    ⚠️ Warning: {fw}")

    report_target = report_file or os.environ.get(
        'PACKAGE_INSPECTION_FILE', '/tmp/package_inspections.md'
    )
    try:
        md_lines = [
            f"<details>",
            f"<summary>📦 Asset Inspection: <code>{pkgname}</code> ({asset_name})</summary>\n",
            f"- **Asset**: `{asset_name}`",
            f"- **Size**: {size_str}",
            f"- **Format**: {details}",
            f"- **Total files**: {len(files)}",
        ]

        if key_files:
            md_lines.append("\n**Key files:**")
            for kf in key_files[:12]:
                md_lines.append(f"- `{kf}`")
            if len(key_files) > 12:
                md_lines.append(f"- *... and {len(key_files) - 12} more*")

        if matched_targets or missing_targets or format_warnings:
            md_lines.append("\n**PKGBUILD validation:**")
            for mt in matched_targets:
                md_lines.append(f"- ✅ Confirmed `{mt}` present in asset")
            for wt in missing_targets:
                md_lines.append(f"- ⚠️ **Warning**: `{wt}` expected by PKGBUILD but missing!")
            for fw in format_warnings:
                md_lines.append(f"- ⚠️ **Warning**: {fw}")

        md_lines.append("\n</details>\n")

        with open(report_target, 'a') as rf:
            rf.write('\n'.join(md_lines) + '\n')
    except Exception as e:
        print(f"  Note: Could not write inspection report: {e}", file=sys.stderr)

    return {
        'asset_name': asset_name,
        'size_str': size_str,
        'details': details,
        'files': files,
        'matched_targets': matched_targets,
        'missing_targets': missing_targets,
        'warnings': format_warnings,
    }


def download_and_hash_asset(
    url: str,
    asset_name: str,
    pkgbuild_content: str,
    pkgname: str,
    new_ver: str
) -> str | None:
    """Download a URL, compute SHA256, inspect its archive contents, and clean up."""
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False)
        result = subprocess.run(
            ['curl', '-sL', '-o', tmp.name, url],
            capture_output=True, timeout=180
        )
        if result.returncode != 0:
            os.unlink(tmp.name)
            return None

        h = hashlib.sha256()
        with open(tmp.name, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        digest = h.hexdigest()

        # Run inspection and validation (fail-safe so inspection errors never drop checksum)
        try:
            inspect_and_validate_asset(
                tmp.name, asset_name, pkgbuild_content, pkgname, new_ver
            )
        except Exception as err:
            print(f"  Warning: Asset inspection encountered an issue: {err}", file=sys.stderr)

        os.unlink(tmp.name)
        return digest
    except Exception as e:
        print(f"  Error downloading {url}: {e}", file=sys.stderr)
        return None


def compute_sha256(url: str) -> str | None:
    """Download a URL and compute its SHA256 hash."""
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            result = subprocess.run(
                ['curl', '-sL', '-o', tmp.name, url],
                capture_output=True, timeout=120
            )
            if result.returncode != 0:
                return None
            h = hashlib.sha256()
            with open(tmp.name, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    h.update(chunk)
            os.unlink(tmp.name)
            return h.hexdigest()
    except Exception:
        return None


def resolve_source_url(source_pattern: str, old_ver: str, new_ver: str,
                       tag_prefix: str) -> str | None:
    """Resolve a source URL pattern with the new version."""
    # Replace version in URL
    url = source_pattern.replace(old_ver, new_ver)

    # Handle "localname::url" format — extract just the URL part
    if '::' in url:
        url = url.split('::', 1)[1]

    # Handle GitHub archive URLs
    if 'github.com' in url and '/archive/' in url:
        return url

    # Handle GitHub release URLs
    if 'github.com' in url and '/releases/download/' in url:
        return url

    # Handle git+ URLs with tags
    if url.startswith('git+'):
        return None  # Can't download git repos for sha256

    return url


def update_source_url_in_content(content: str, old_ver: str, new_ver: str) -> str:
    """Update version references in source URLs within PKGBUILD content."""
    # Replace version in source= lines (including arch-specific
    # source_x86_64/source_aarch64 arrays).
    ends_with_newline = content.endswith('\n')
    lines = content.splitlines()
    result = []
    in_source = False
    for line in lines:
        stripped = line.strip()
        if re.match(r'source(_\w+)?=\(', stripped):
            in_source = True

        if in_source:
            # Replace old version with new version in source lines
            line = line.replace(old_ver, new_ver)

        if in_source and ')' in stripped:
            in_source = False

        result.append(line)
    updated = '\n'.join(result)
    if ends_with_newline:
        updated += '\n'
    return updated


def update_srcinfo(pkgdir: str, pkgname: str, old_ver: str, new_ver: str,
                   new_sha256: dict | str | None):
    """Update .SRCINFO file.

    new_sha256 maps arch suffix ('' for plain sha256sums) to hash. A plain
    string is accepted for backwards compatibility and applies to the
    un-suffixed sha256sums line only.
    """
    srcinfo_path = os.path.join(pkgdir, '.SRCINFO')
    if not os.path.exists(srcinfo_path):
        return

    if isinstance(new_sha256, str):
        hashes = {'': new_sha256}
    else:
        hashes = new_sha256 or {}

    content = Path(srcinfo_path).read_text()

    # Update pkgver
    content = re.sub(r'(\tpkgver = )(.+)', f'\\g<1>{new_ver}', content)

    # Update source lines (replace old version with new)
    content = content.replace(old_ver, new_ver)

    # Update sha256sums, one hash per arch suffix
    for suffix, digest in hashes.items():
        content = re.sub(
            rf'(\tsha256sums{re.escape(suffix)} = )[0-9a-f]{{64}}',
            f'\\g<1>{digest}',
            content
        )

    Path(srcinfo_path).write_text(content)


def update_package(pkgdir: str, new_ver: str):
    """Update a package to a new version."""
    pkgbuild_path = os.path.join(pkgdir, 'PKGBUILD')
    if not os.path.exists(pkgbuild_path):
        print(f"Error: No PKGBUILD found in {pkgdir}", file=sys.stderr)
        return False

    # Strip leading 'v' if version starts with 'v' followed by a digit (e.g. 'v1.8.1' -> '1.8.1')
    if re.match(r'^v\d', new_ver):
        new_ver = new_ver[1:]

    info = parse_pkgbuild(pkgbuild_path)
    old_ver = info['pkgver']

    if not old_ver:
        print(f"Error: Could not parse pkgver from {pkgbuild_path}", file=sys.stderr)
        return False

    if old_ver == new_ver:
        print(f"Already at version {new_ver}, skipping")
        return True

    print(f"Updating {info['pkgname']}: {old_ver} -> {new_ver}")

    # Skip packages with auto-generated pkgver()
    if info['has_pkgver_func']:
        print(f"  Package has pkgver() function, version is auto-generated")
        print(f"  Only updating tracked version in old_versions.txt")
        return True

    # Determine new sha256sums, one per arch suffix. Arch-specific source
    # arrays (e.g. twmd-bin's source_x86_64/source_aarch64) each point at a
    # different asset, so each needs its own download + hash. Sharing one
    # hash across arches leaves stale checksums behind.
    new_hashes: dict = {}
    if info['has_skip_sha256']:
        print(f"  sha256sums = SKIP, no hash computation needed")
    else:
        for suffix, entries in info['resolved_groups'].items():
            for pattern in entries:
                local_name = None
                if '::' in pattern:
                    local_name, url = pattern.split('::', 1)
                else:
                    url = pattern
                if url.startswith('git+'):
                    continue  # Can't download git repos for sha256
                if (url.startswith('https://') or url.startswith('http://')) and (
                    '/releases/download/' in url or
                    '/archive/' in url or
                    'releases' in url or
                    url.endswith(('.deb', '.tar.gz', '.tar.xz', '.tgz', '.zip', '.tar.zst'))
                ):
                    url = url.replace(old_ver, new_ver)
                    if local_name:
                        local_name = local_name.replace(old_ver, new_ver)
                    asset_name = local_name or url.split('/')[-1]
                    label = suffix or 'default'
                    print(f"  Downloading [{label}] {url}...")
                    digest = download_and_hash_asset(
                        url, asset_name, info['content'], info['pkgname'], new_ver
                    )
                    if digest:
                        print(f"  sha256 [{label}] = {digest}")
                        new_hashes[suffix] = digest
                    else:
                        print(f"  Warning: Could not compute sha256 for [{label}], leaving unchanged")
                    break
            else:
                continue
        if not new_hashes:
            print(f"  Warning: No downloadable source URL found, cannot compute sha256")

    # Read and update PKGBUILD
    content = Path(pkgbuild_path).read_text()

    # Update pkgver
    content = re.sub(
        rf'(^pkgver=)(.+)',
        f'\\g<1>{new_ver}',
        content,
        count=1,
        flags=re.MULTILINE
    )

    # Update source URLs (replace old version with new)
    content = update_source_url_in_content(content, old_ver, new_ver)

    # Update sha256sums, one hash per arch suffix
    for suffix, digest in new_hashes.items():
        content = re.sub(
            rf"(sha256sums{re.escape(suffix)}=\()['\"][0-9a-f]{{64}}['\"]\)",
            f"\\g<1>'{digest}')",
            content
        )

    Path(pkgbuild_path).write_text(content)

    # Update .SRCINFO
    update_srcinfo(pkgdir, info['pkgname'], old_ver, new_ver, new_hashes)

    print(f"  Updated PKGBUILD and .SRCINFO")
    return True


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <package_dir> <new_version>", file=sys.stderr)
        sys.exit(1)

    pkgdir = sys.argv[1]
    new_ver = sys.argv[2]

    if not os.path.isdir(pkgdir):
        print(f"Error: {pkgdir} is not a directory", file=sys.stderr)
        sys.exit(1)

    success = update_package(pkgdir, new_ver)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
