#!/bin/bash
# Build the notifier app that the hourly pulse posts its banner through.
#
# Why a whole app bundle exists just to show a banner: macOS takes a
# notification's icon from the bundle that sent it, and offers no way to
# override it per notification. terminal-notifier used to fake this with
# -sender and -appIcon; both were removed in 3.0.0 because
# UNUserNotificationCenter reads the real signed identity, so there was nothing
# to override. A Hanuman icon therefore means a bundle whose icon IS the
# Hanuman icon. This is the route terminal-notifier's own README documents.
#
# The alternative was posting from the Tauri app itself, which owns the icon by
# construction — but launchd runs the pulse whether or not the app is open, and
# a Hub-side banner would go silent the moment Ben closed the window. That is
# the objection pulse_notify.py's docstring raises, and this route keeps it
# answered: the copy is a standalone agent that needs nothing running.
#
# Idempotent — safe to re-run after a Homebrew upgrade of terminal-notifier or
# a rebuild of Hanuman Hub that changes the icon.
set -euo pipefail

SRC_APP="$(brew --prefix terminal-notifier)/terminal-notifier.app"
ICON_SRC="/Applications/Hanuman Hub.app/Contents/Resources/icon.icns"
DEST_APP="$HOME/Applications/Hanuman Pulse.app"

# Distinct from the Hub's own com.benmyers.hanuman-hub. Sharing an identifier
# with a real installed app is what would confuse LaunchServices; a separate id
# is also precisely what earns this bundle its own icon and its own entry under
# System Settings -> Notifications.
# If you ever have to change this, know what it costs: the notification
# permission is recorded per bundle id in the usernoted database, which needs
# Full Disk Access to read and which `tccutil reset UserNotification` refuses to
# clear for an id it has no ncprefs entry for. A denial recorded against an id is
# effectively permanent, and the only way back is a new id — which is why the
# first one was burned. So do not send from this bundle before it is registered
# below, and never kill a send that is waiting on the permission prompt: a
# pending request that dies gets recorded as a denial.
BUNDLE_ID="com.benmyers.hanumanpulse.v3"
# What the banner prints above the title. The notification says "Hourly Pulse";
# this line says which of Ben's things is talking.
BUNDLE_NAME="Hanuman Hub"

for path in "$SRC_APP" "$ICON_SRC"; do
  if [ ! -e "$path" ]; then
    echo "missing: $path" >&2
    exit 1
  fi
done

mkdir -p "$HOME/Applications"
rm -rf "$DEST_APP"
cp -R "$SRC_APP" "$DEST_APP"

PLIST="$DEST_APP/Contents/Info.plist"
/usr/bin/plutil -replace CFBundleIdentifier -string "$BUNDLE_ID" "$PLIST"
/usr/bin/plutil -replace CFBundleName -string "$BUNDLE_NAME" "$PLIST"
# CFBundleIconFile is extensionless by convention and already reads "Terminal";
# the file is replaced rather than the key retargeted, so the two cannot drift.
cp "$ICON_SRC" "$DEST_APP/Contents/Resources/Terminal.icns"

# Editing Info.plist and Resources breaks the seal Homebrew's copy shipped
# with, and an app with a broken signature is killed rather than launched. The
# original is ad-hoc signed, so re-signing ad-hoc loses nothing.
rm -rf "$DEST_APP/Contents/_CodeSignature"
/usr/bin/codesign --force --sign - "$DEST_APP" 2>&1 | sed 's/^/codesign: /'

# Load-bearing, not housekeeping. An unregistered bundle cannot be resolved by
# the notification service, and the symptom is not an error — the first send
# hangs indefinitely instead of reporting anything. `tccutil` looks the app up
# the same way, which is why it only works for a bundle in /Applications or
# ~/Applications.
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
"$LSREGISTER" -f "$DEST_APP"

echo "built: $DEST_APP"
echo "binary: $DEST_APP/Contents/MacOS/terminal-notifier"
