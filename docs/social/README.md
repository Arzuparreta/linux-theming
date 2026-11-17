# GitHub social preview

Upload `github.png` in the repository **Settings → General → Social preview**.
The image is 1280 × 640 pixels. `github.svg` is the editable source; it uses
Noto Sans Medium and embeds the existing application icon.

Tux by Larry Ewing (lewing@isc.tamu.edu), created with The GIMP.
See [artwork attribution](../../packaging/THIRD_PARTY.md#icon-artwork).

## Demo video

`scripts/capture-demo.py` records a ~32 second H.264 video (1920 × 1440, 30 fps,
no audio) for posting on social sites. It drives the real Qt application:
browsing the five documentation wallpapers, switching Dark/Light, opening the
review with exact changes, applying, and showing History. A drawn pointer,
captions in a band below the window, and an end card with the repository URL
are added on top.

```sh
.venv/bin/python scripts/capture-demo.py            # writes dist/linux-theming-demo.mp4
.venv/bin/python scripts/capture-demo.py out.mp4    # or a chosen path
```

It needs `ffmpeg` on the host. Isolation is the same as the README screenshot
([details](../screenshots/README.md)): a disposable HOME and XDG tree, a PATH of
version-only stubs for kitty, Waybar, Rofi and Dunst, and an unreachable session
bus. The applied theme is a real file-only transaction inside that temporary
HOME. No developer configuration, running application or desktop wallpaper is
changed, and nothing is downloaded. The end card credits every
photograph, including Dark_Ivy (© 2012 Garrett LeSage, CC BY-SA 3.0), which the
video shows full-size; see
[wallpaper credits](../screenshots/README.md#wallpaper-credits-and-reuse).
