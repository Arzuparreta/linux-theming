# README screenshot

`theme.png` is captured from the current Qt Quick application at 1440 × 960,
using the real controller, photo library and wallpaper-derived palette. The
sample bar and terminal are the application's preview, not separate desktop apps.

To refresh it with the pinned GUI dependencies:

```sh
.venv/bin/python scripts/capture-readme.py
```

The script uses a disposable HOME, XDG directories, isolated PATH and unreachable
session bus. A version-only Kitty stub and a temporary Kitty configuration let
the engine create a real file-only theme transaction, so the Applied badge and
Undo action represent actual fixture state. Desktop wallpaper application is off.
No developer configuration, running applications or desktop wallpaper is changed.
The fixture is deleted afterwards. The script downloads nothing.

## Wallpaper credits and reuse

These are photographs distributed as desktop wallpapers by
[GNOME Backgrounds](https://gitlab.gnome.org/GNOME/gnome-backgrounds), replacing
the previous invented landscape images. The bundled documentation copies are
resized to a maximum dimension of 1920 pixels and re-encoded as JPEG (quality 88);
their proportions and colors are preserved. They are not installed by the app.

| Local file | Upstream title | Photographer | License |
|---|---|---|---|
| [forest-road.jpg](wallpapers/forest-road.jpg) | Road | Joshua Welch | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| [sea-sunset.jpg](wallpapers/sea-sunset.jpg) | SeaSunset | Sebastian Voortman | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| [snowy-forest.jpg](wallpapers/snowy-forest.jpg) | ColdWarm | invisiblepower | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| [light-waves.jpg](wallpapers/light-waves.jpg) | LightWaves | Skitterphoto | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| [dark-ivy.jpg](wallpapers/dark-ivy.jpg) | Dark_Ivy | © 2012 Garrett LeSage | [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/) |

The resized Dark_Ivy photograph retains its CC BY-SA 3.0 license, including when
shown as a thumbnail in the screenshot. CC0 photographs may be reused without
permission, including in the README. Attribution and the ShareAlike license must
accompany reuse of Dark_Ivy and its adaptations. These image licenses are separate
from the application code; the photographers do not endorse this project.

[sources.json](wallpapers/sources.json) pins the upstream commit, original image
URLs, original and local SHA-256 digests, and dimensions. The upstream
[AUTHORS file](wallpapers/GNOME-AUTHORS.txt) records the per-image license grants
and original photographer links. The screenshot's main wallpaper is **Road** by
Joshua Welch (CC0 1.0).
