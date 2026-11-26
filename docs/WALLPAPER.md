# Desktop wallpaper integration

Choose an image, review, apply. The image picker enables **Set as desktop
wallpaper** automatically. Uncheck it to use the image only as a palette source.
Existing saved profiles default to colors only until the next image selection.
Clearing the image or disabling the choice leaves the current wallpaper alone;
use History to review and restore an earlier background.

The active session selects the adapter, not the presence of an installed package.
Probes only query settings and live IPC endpoints. They never install software,
start a daemon, restart applications or invoke legacy walltheme apply hooks.
System tools run with the host's library paths; the AppImage's private OpenSSL
and Qt/QML paths are not passed to them. Their stderr appears in failure details.
Desktop owners take precedence over generic X11/Wayland tools. An unavailable
GNOME API, for example, cannot fall back to setting an invisible X11 root image.
On standalone Wayland sessions, awww, then swww, then compatible Hyprland
hyprpaper IPC are probed. Plasma tries available qdbus variants. All fallbacks
are resolved before review; application never switches to an unreviewed adapter.

| Session/backend | Applied changes | Conditions/limits |
|---|---|---|
| GNOME, Budgie, Unity, Pantheon | GSettings background URI, dark URI when present, zoom style | Active desktop evidence; readable/writable schema and restorable previous images |
| Cinnamon | Its own background schema and zoom style | Uses Cinnamon settings rather than GNOME's installed schema |
| MATE | Its own picture filename and style | Uses local paths rather than URIs |
| KDE Plasma | Image plugin URI per desktop containment, via live scripting API | Only existing Image plugins; slideshows/third-party plugins are preserved. Requires an enabled evaluateScript API and qdbus6/qdbus/qdbus-qt5 |
| Xfce | Existing monitor/workspace image properties, zoom style, disable cycling | Restores individual previous images, styles and cycling flags. Uninitialized properties are not created |
| swww / awww | Image per output reported by a live query, without transitions | Image-backed outputs with readable previous files; both original and namespaced query formats are supported; solid colors are rejected |
| Hyprland + hyprpaper | Preload and set image per active output | Legacy `hyprctl hyprpaper listactive/preload/wallpaper` IPC, checked at runtime. New protocols without these queries are unsupported. Commas in image paths are rejected. Loaded images are not unloaded |
| X11 + feh (e.g. i3/Openbox) | Root background using `--bg-fill --no-fehbg` | Requires a simple parseable existing `.fehbg` restoration record and readable xprop root state (including absent pixmap properties on a fresh session). The script is never executed, and `.fehbg` is never rewritten, including symlinks |

Swaybg has a separate **Use image** configuration workflow: it edits literal
background directives in `sway/config` for your next manual Sway reload. It never
launches/replaces the running process or claims to query its live background.
When selected, this reviewed file workflow takes precedence over live wallpaper
application. It composes with the Sway border adapter and retains exact original
output settings for disconnect/undo. Includes and complex output blocks are
blocked; see [INTEGRATIONS.md](INTEGRATIONS.md).

Nitrogen, xwallpaper, PCManFM/LXQt,
Enlightenment and other services without implemented read/restore protocols
remain unsupported. In Sway, an already running swww/awww can
be used. The application does not replace an existing swaybg process or launch
an alternative. Missing support blocks the requested wallpaper change with an
explanation; terminal colors can still be applied by switching off **Desktop wallpaper**.

## Review, failure and restoration

The review includes backend, per-target previous/proposed values and the profile
change. Image content hashes, session identity and live settings are rechecked
before application. Actions and previous values are retained in the same private
journal as file changes. Partial setting failures roll back completed settings
and files; interrupted work is recovered through History. Later external edits,
changed output sets and changed/missing restoration images block restoration.
Original image files must remain available for live restoration unless the i3
option below saved a private copy. swww/awww and legacy hyprpaper queries do
not report prior scaling or animation options; undo restores their source
images using the adapter defaults, not those unqueryable options. Native desktop settings persist according to
the desktop's behavior. With the default settings, feh backgrounds last for the
current X11 session.

Feh has no query API returning its current source image. On first use its
existing `.fehbg` record is assumed to describe the root background; the X11
pixmap identity detects subsequent replacements. A fresh session with both root
pixmap properties absent is also supported: review explicitly warns that undo
restores the last configured `.fehbg` image, not the current unregistered
background. The absent-property state is rechecked before applying, so a
wallpaper set after review invalidates the plan. Malformed query output and
missing restoration records/images remain blocked. Further application and ordered
undo use journalled images and newly allocated pixmap identities. An abrupt
process/power loss between feh replacing the pixmap and recording its new ID
cannot be automatically attributed safely: recovery refuses an unknown pixmap
rather than overwriting a later change. Preserve the journal for manual recovery
in that case. Root output/layout changes and desktop slideshows can likewise
produce conflicts. External tools can race a setting between the last check and
the setter; these APIs do not provide atomic compare-and-set.

## Restore the wallpaper at login

Full desktop environments (GNOME, Cinnamon, MATE, Budgie, Unity, Pantheon, KDE
Plasma and Xfce) remember their native wallpaper setting across restarts, and
Sway's **Use image** workflow configures `swaybg` directly inside `sway/config`.
Standalone X11 and Wayland window managers using `feh`, `awww`, `swww` or
`hyprpaper` apply changes to the running session only unless login restoration
is enabled.

In an active standalone X11 or Wayland session, choose a desktop wallpaper, turn
on **Restore wallpaper at login**, then review and approve. The app generates
`$XDG_CONFIG_HOME/linux-theming/restore-wallpaper.sh`, verifies the saved image's
SHA-256 checksum on login before calling the reviewed host tool (`feh`, `awww`,
`swww` or `hyprctl`), and leaves `~/.fehbg` intact. It works with the AppImage
closed and never starts a background daemon.

| Window manager | Startup file | Hook and behavior |
|---|---|---|
| i3 | Active per-user config (`loaded_config_file_name`) | Appends `exec_always --no-startup-id ...`; validated with `i3 -C`. Allows `set_from_resource` |
| bspwm | `bspwm/bspwmrc` | Appends backgrounded POSIX shell command; validated with `sh -n` / `bash -n` |
| Openbox | `openbox/autostart` | Creates or appends backgrounded shell command; validated with `sh -n` |
| Sway | `sway/config` | Appends `exec_always ...` (skipped when `swaybg` **Use image** is selected) |
| Hyprland | `hypr/hyprland.conf` | Appends `exec-once = ...` |
| Niri | `niri/config.kdl` | Appends `spawn-at-startup ...` with `//` comment; validated with `niri validate` when installed |
| River | `river/init` | Appends backgrounded shell command; ignores `riverctl map` bindings |
| labwc | `labwc/autostart` | Creates or appends backgrounded shell command; validated with `sh -n` |
| Other standalone WMs | Manual startup file | Generates and updates the startup script and saved copy; review shows the one-line command to add yourself |

On Wayland sessions using `awww`, `swww` or `hyprpaper`, the generated script
waits briefly (up to 5 seconds) for your compositor's wallpaper daemon to accept
IPC queries/preloads at login, without ever launching a daemon itself. If a
known WM config uses `exec_always wal -R`, `exec-once = wal -R`, or a standalone
`wal -R`, the reviewed change adds `-n` so Pywal restores colors without
overwriting the wallpaper. Other competing startup wallpaper commands (and
`output ... bg ...` lines in Sway) must be resolved manually; daemon launches
such as `swww-daemon`, `awww-daemon`, `swww init`, `awww init` and bare
`hyprpaper` are allowed. For linked configs, `include`/`source` directives, or
paths outside `$XDG_CONFIG_HOME` / `$HOME`, review shows the exact command to
add yourself.

Each applied wallpaper is saved under `$XDG_DATA_HOME/linux-theming/wallpapers`
(default `~/.local/share/linux-theming/wallpapers`). The script selects a copy
by its content hash, so moving or removing the original image does not break
login. Copies are retained for History and identical images share one copy.
At login on X11, the `_LINUX_THEMING_WALLPAPER` root marker binds the restored
image to its X11 pixmap so the next wallpaper change can use it as a previous
background. Live History undo still checks the session and pixmap of the
reviewed change.

Turn the option off and approve to remove the app's startup command (or remove
newly created `openbox/autostart` / `labwc/autostart` files) and restore any
modified `wal -R` line. Unrelated edits are preserved on update and undo, and
edits to managed lines stop the operation with a conflict explanation. With a
manual hook, disabling removes the script and the hook can be deleted from your
configuration.

## Validation and protocol references

Unit tests use disposable HOME/XDG trees and mocked desktop commands. They
cover adapter selection, read-only review, multiple outputs, light/dark variants,
stale sessions/images/settings, failures, partial recovery, ordered feh undo,
symlink preservation, conflict rejection, all eight automatic WM targets plus
manual fallback, and real `/bin/sh` execution of the generated restore scripts
against isolated fake `swww`/`awww`/`hyprctl` executables. They do not establish
verification on every desktop version or real compositor. Real-session checks must
be done by explicitly applying a reviewed plan on a test desktop. The bundled
`--smoke-test-wallpaper` diagnostic additionally runs real feh/xprop and the
reviewed first-use apply on an uninitialized root, undo and failure rollback
against its own private Xvfb with temporary HOME/XDG directories. It never sets
wallpaper on the caller's display. `--smoke-test-feh` only decodes a temporary
image with host feh and needs no Xvfb. The i3 startup tests use two private
Xvfb sessions to validate actual login with `set_from_resource` and the original
wallpaper removed, and cover disable, manual hooks and managed-line conflicts.

Protocol references: [Plasma scripting API](https://develop.kde.org/docs/plasma/scripting/api/),
[swww source and manuals](https://github.com/LGFae/swww),
[hyprpaper source](https://github.com/hyprwm/hyprpaper),
[GNOME background schemas](https://gitlab.gnome.org/GNOME/gsettings-desktop-schemas),
[Xfce xfconf](https://docs.xfce.org/xfce/xfconf/start),
[feh source and manual](https://github.com/derf/feh).
