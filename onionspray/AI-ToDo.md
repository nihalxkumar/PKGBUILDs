Fix Permissions Properly
Current PKGBUILD:
find "$pkgdir/opt/onionspray" -name "*.sh" -exec chmod 755 {} \;
find "$pkgdir/opt/onionspray" -name "*.pl" -exec chmod 755 {} \;
find "$pkgdir/opt/onionspray/vendors" -type f -exec chmod 755 {} \;
This approach causes problems because:

- Makes EVERYTHING in vendors/ executable (wrong)
- Doesn't fix .git/ permissions
- Doesn't ensure .git/modules/ structure is accessible
Better approach:

```
package() {
  cd "$srcdir/$pkgname"
  # Install to /opt/onionspray
  install -dm755 "$pkgdir/opt/onionspray"
  # Copy everything, preserve mode
  cp -r --preserve=mode . "$pkgdir/opt/onionspray/" 2>/dev/null || true
  # Fix .git directory permissions (ensure readable but not writable by all)
  chmod -R go-rwx "$pkgdir/opt/onionspray/.git"
  chmod -R u+rwX "$pkgdir/opt/onionspray/.git"
  # Ensure .git/modules exists and is accessible
  chmod 755 "$pkgdir/opt/onionspray/.git/modules"
  if [[ -d "$pkgdir/opt/onionspray/.git/modules/vendors" ]]; then
    chmod 755 "$pkgdir/opt/onionspray/.git/modules/vendors"
  fi
  # Make main script executable
  chmod 755 "$pkgdir/opt/onionspray/onionspray"
  # Make scripts executable (selective)
  find "$pkgdir/opt/onionspray" -maxdepth 1 -name "*.sh" -exec chmod 755 {} \;
  find "$pkgdir/opt/onionspray" -maxdepth 1 -name "*.pl" -exec chmod 755 {} \;
  
  # Library scripts
  find "$pkgdir/opt/onionspray/lib" -type f \( -name "*.sh" -o -name "*.pl" \) -exec chmod 755 {} \;
  
  # Onionmine binaries
  chmod 755 "$pkgdir/opt/onionspray/vendors/onionmine/onionmine" 2>/dev/null || true
  chmod 755 "$pkgdir/opt/onionspray/vendors/onionmine/bin/"* 2>/dev/null || true
  # Rest of installation...
}
```

5. Consider Patching Update Command
To prevent confusing errors, add a patch that gracefully handles git operations:
Create disable-git-update.patch:

```
#!/bin/bash
# Patch to disable git update operations in packaged version
# Apply in prepare() if desired
# This would modify onionspray to fail gracefully or show message
Or add wrapper in /usr/bin/onionspray:
cat >"$pkgdir/usr/bin/onionspray" <<'EOF'
#!/bin/sh
# Check for update command
if [[ "$1" == "update" || "$1" == "upgrade" ]]; then
  echo "error: 'onionspray $1' is not supported when installed as package."
  echo "Please update via: sudo pacman -Syu onionspray"
  exit 1
fi
exec /opt/onionspray/onionspray "$@"
EOF
```
