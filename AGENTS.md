# AUR PKGBUILD Workflow

This repository tracks Arch Linux AUR packages. Each package lives in its own
directory with `PKGBUILD` and `.SRCINFO` tracked in git.

## Updating a package

Use `aurpublish` to update packages. This is the only supported workflow. It
handles everything: updating `PKGBUILD`, regenerating `.SRCINFO`, writing a
commit message, and pushing to the AUR remote.

```sh
aurpublish <package_name>
```

For example, after bumping `pkgver` and `sha256sums` in a package's `PKGBUILD`,
run `aurpublish <pkgname>` to regenerate `.SRCINFO` and commit.

If you only need to regenerate `.SRCINFO` without publishing (e.g. for CI or
before pushing to the main repo):

```sh
makepkg --printsrcinfo > .SRCINFO
```

## Automated version detection

`nvchecker` (via `.github/workflows/nvchecker.yml`) detects new upstream
versions every 12 hours and opens a PR with updates. It uses the
`scripts/update-package.py` helper to bump versions and checksums.

After a PR is merged, run `aurpublish` for each updated package to sync the
changes to AUR.

## Remotes

| Remote   | URL                                               | Direction      |
|----------|---------------------------------------------------|----------------|
| `origin` | https://github.com/nihalxkumar/PKGBUILDs.git       | Push main work |
| `codeberg` | git@codeberg-personal:nihalxkumar/PKGBUILDs.git  | Mirror only    |

After pushing to `origin`, mirror to Codeberg:

```sh
git push codeberg main
```

## Rules

1. **Always** use `aurpublish` for AUR updates. It writes its own commit
   message and pushes to the AUR remote (you will be prompted for the SSH
   password).
2. After `aurpublish` completes, push the same change to GitHub (`origin`)
   and mirror to Codeberg.
3. **Never** commit or push `AGENTS.md` to any remote. This file is local
   workflow documentation only.
4. For git operations (status, log, diff, grep) use read-only commands. Do
   not run `git commit`, `git push`, or `git merge` - the user handles those.
5. When bumping a package, update `old_versions.txt` via `nvchecker -c
   nvchecker.toml` or manually add the new entry.
