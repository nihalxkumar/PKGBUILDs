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

    # Extract source array (handle multi-line)
    sources = []
    in_source = False
    paren_depth = 0
    source_block = ""
    for line in content.splitlines():
        stripped = line.strip()
        if 'source=(' in stripped:
            in_source = True
            paren_depth = stripped.count('(') - stripped.count(')')
            source_block = stripped
            if paren_depth <= 0:
                in_source = False
            continue
        if in_source:
            source_block += " " + stripped
            paren_depth += stripped.count('(') - stripped.count(')')
            if paren_depth <= 0:
                in_source = False
                break

    if source_block:
        m = re.search(r'source=\((.*)\)', source_block, re.DOTALL)
        if m:
            raw = m.group(1)
            tokens = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', raw)
            sources = [t.strip('"').strip("'") for t in tokens]

    # Resolve variable references in sources
    variables = {}
    for name in ['_pkgname', '_url', 'url', 'pkgver', 'pkgname']:
        val = extract_var(name)
        if val:
            variables[name] = val

    resolved_sources = []
    for src in sources:
        resolved = src
        for var_name, var_val in variables.items():
            resolved = resolved.replace(f'${{{var_name}}}', var_val)
            resolved = resolved.replace(f'${var_name}', var_val)
        resolved_sources.append(resolved)

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
    # Replace version in source= lines
    lines = content.splitlines()
    result = []
    in_source = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('source=('):
            in_source = True

        if in_source:
            # Replace old version with new version in source lines
            line = line.replace(old_ver, new_ver)

        if in_source and ')' in stripped:
            in_source = False

        result.append(line)
    return '\n'.join(result)


def update_srcinfo(pkgdir: str, pkgname: str, old_ver: str, new_ver: str,
                   new_sha256: str | None):
    """Update .SRCINFO file."""
    srcinfo_path = os.path.join(pkgdir, '.SRCINFO')
    if not os.path.exists(srcinfo_path):
        return

    content = Path(srcinfo_path).read_text()

    # Update pkgver
    content = re.sub(r'(\tpkgver = )(.+)', f'\\g<1>{new_ver}', content)

    # Update source lines (replace old version with new)
    content = content.replace(old_ver, new_ver)

    # Update sha256sums if we have a new hash
    if new_sha256:
        content = re.sub(
            r'(\tsha256sums = )[0-9a-f]{64}',
            f'\\g<1>{new_sha256}',
            content
        )
        # Also handle per-arch sha256sums
        content = re.sub(
            r'(\tsha256sums_\w+ = )[0-9a-f]{64}',
            f'\\g<1>{new_sha256}',
            content
        )

    Path(srcinfo_path).write_text(content)


def update_package(pkgdir: str, new_ver: str):
    """Update a package to a new version."""
    pkgbuild_path = os.path.join(pkgdir, 'PKGBUILD')
    if not os.path.exists(pkgbuild_path):
        print(f"Error: No PKGBUILD found in {pkgdir}", file=sys.stderr)
        return False

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

    # Determine new sha256sum
    new_sha256 = None
    if info['has_skip_sha256']:
        print(f"  sha256sums = SKIP, no hash computation needed")
    elif info['source_url']:
        # Try to resolve and download the new source
        resolved_url = resolve_source_url(
            info['source_url'], old_ver, new_ver, info['tag_prefix']
        )
        if resolved_url:
            print(f"  Downloading {resolved_url}...")
            new_sha256 = compute_sha256(resolved_url)
            if new_sha256:
                print(f"  sha256 = {new_sha256}")
            else:
                print(f"  Warning: Could not compute sha256, leaving unchanged")
        else:
            print(f"  Warning: Could not resolve source URL for sha256 computation")
    else:
        print(f"  Warning: No source URL found, cannot compute sha256")

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

    # Update sha256sums if we have a new hash
    if new_sha256:
        content = re.sub(
            r"(sha256sums=\()['\"][0-9a-f]{64}['\"]\)",
            f"\\g<1>'{new_sha256}')",
            content
        )
        # Also handle per-arch sha256sums
        content = re.sub(
            r"(sha256sums_\w+=\()['\"][0-9a-f]{64}['\"]\)",
            f"\\g<1>'{new_sha256}')",
            content
        )

    Path(pkgbuild_path).write_text(content)

    # Update .SRCINFO
    update_srcinfo(pkgdir, info['pkgname'], old_ver, new_ver, new_sha256)

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
