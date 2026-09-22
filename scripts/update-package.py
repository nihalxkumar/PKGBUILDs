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
import os
import re
import subprocess
import sys
import tempfile
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
                url = pattern.split('::', 1)[1] if '::' in pattern else pattern
                if url.startswith('git+'):
                    continue  # Can't download git repos for sha256
                if 'github.com' in url and '/releases/download/' in url:
                    url = url.replace(old_ver, new_ver)
                    label = suffix or 'default'
                    print(f"  Downloading [{label}] {url}...")
                    digest = compute_sha256(url)
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
