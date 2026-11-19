"""GTK 3 stylesheet rules for the GTK 3 and Thunar adapters. No GTK code is run.

Both adapters append their rules to the user's gtk-3.0/gtk.css. GTK lets that
file override the theme for every property it sets, whatever the selector, so a
rule that sets a background must also set the text color drawn on it.

Thunar's rules are limited to its main windows (``window.thunar``, the style
class Thunar puts on them). They define no named colors: ``@define-color`` in
gtk.css is shared by every GTK 3 application. Selected files use the GTK
selection colors because Thunar paints them itself, with those names, when file
highlighting is enabled (``thunar_util_clip_view_background`` in Thunar 4.20).
"""

THUNAR = "window.thunar"


def mix(color, other, amount):
    """``color`` moved ``amount`` of the way to ``other`` in sRGB."""
    start = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    end = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(a + (b - a) * amount):02x}" for a, b in zip(start, end, strict=True))


def roles(c):
    """Surface colors derived from the palette background and text color."""
    bg, fg = c["bg"], c["fg"]
    return {"window": bg, "text": fg, "view": bg, "accent": c["accent"],
            "chrome": mix(bg, fg, 0.06),     # Title, tool, menu, tab and status bars.
            "sidebar": mix(bg, fg, 0.03),
            "inactive": mix(bg, fg, 0.08),   # The split-view pane without focus.
            "raised": mix(bg, fg, 0.10),     # Buttons that are not flat.
            "hover": mix(bg, fg, 0.16), "pressed": mix(bg, fg, 0.24),
            "border": mix(bg, fg, 0.22), "disabled": mix(bg, fg, 0.45),
            "slider": mix(bg, fg, 0.40), "slider-hover": mix(bg, fg, 0.55)}


def stylesheet(rules):
    return "".join(f"{selector} {{ {declarations} }}\n" for selector, declarations in rules)


def scoped(scopes, *selectors):
    return ", ".join(f"{scope} {selector}".strip() for scope in scopes for selector in selectors)


def bars_and_buttons(scopes, bars, r):
    """Bars and the buttons inside them, in every state the theme styles.

    Themes give buttons their own text color and light gradients, so buttons on
    a recolored bar need their backgrounds and text replaced together. Each
    state rule has the same specificity as the rule before it and comes later.
    """
    return [
        (scoped(scopes, *bars), f"background-color: {r['chrome']}; background-image: none; color: {r['text']}; "
                                f"border-color: {r['border']}; box-shadow: none;"),
        (scoped(scopes, "button"), f"background-color: {r['raised']}; background-image: none; color: {r['text']}; "
                                   f"border-color: {r['border']}; outline-color: alpha(currentColor, 0.3); "
                                   "box-shadow: none; text-shadow: none; -gtk-icon-shadow: none;"),
        (scoped(scopes, "button.flat", "button.titlebutton"), "background-color: transparent; border-color: transparent;"),
        (scoped(scopes, "button:hover"), f"background-color: {r['hover']};"),
        (scoped(scopes, "button:active", "button:checked"), f"background-color: {r['pressed']};"),
        (scoped(scopes, "button:disabled", "button:disabled label"), f"color: {r['disabled']};"),
    ]


def gtk3(c):
    """Surfaces GTK 3 themes draw with their own background but inherited text.

    Without these, the palette text color on the window reaches menus and
    header bars drawn with the theme's background (light text on Adwaita's
    white menus and light header bars).
    """
    r = roles(c)
    menus = ("menu", ".menu", ".context-menu")
    return stylesheet([
        (", ".join(menus), f"background-color: {r['window']}; color: {r['text']}; border-color: {r['border']};"),
        (scoped(menus, "menuitem:hover"), f"background-color: {r['accent']}; color: {r['window']};"),
        (scoped(menus, "menuitem:disabled"), f"color: {r['disabled']};"),
        *bars_and_buttons(("headerbar", ".titlebar"), ("",), r),
    ])


def thunar(c):
    """Thunar's main windows: bars, location bar, side pane, file views and tabs."""
    r = roles(c)
    w = (THUNAR,)
    rules = [(THUNAR, f"background-color: {r['window']}; color: {r['text']};"),
             *bars_and_buttons(w, ("headerbar", ".titlebar", "toolbar", "menubar", "statusbar", "notebook > header"), r),
             (scoped(w, "menubar > menuitem"), f"color: {r['text']};"),
             (scoped(w, "menubar > menuitem:hover"), f"background-color: {r['hover']}; box-shadow: inset 0 -3px {r['accent']};"),
             (scoped(w, "menubar > menuitem:disabled"), f"color: {r['disabled']};"),
             (scoped(w, "entry"), f"background-color: {r['view']}; background-image: none; color: {r['text']}; "
                                  f"border-color: {r['border']}; box-shadow: none; caret-color: {r['text']};"),
             (scoped(w, "entry:focus"), f"border-color: {r['accent']}; box-shadow: inset 0 0 0 1px {r['accent']};"),
             (scoped(w, "entry:disabled"), f"color: {r['disabled']};"),
             (scoped(w, ".view", "iconview", "treeview", "notebook > stack"),
              f"background-color: {r['view']}; color: {r['text']};"),
             (scoped(w, ".sidebar", ".sidebar .view"), f"background-color: {r['sidebar']}; color: {r['text']};"),
             # Thunar's own rule uses @theme_unfocused_bg_color, which gtk.css would override.
             (scoped(w, ".split-view-inactive-pane .view"), f"background-color: {r['inactive']};"),
             (scoped(w, ".view:selected", "entry selection", ".view text selection"),
              "background-color: @theme_selected_bg_color; color: @theme_selected_fg_color;"),
             # Unfocused windows: Thunar paints highlighted selections with this
             # color at half opacity; match it without highlighting too.
             (scoped(w, ".view:selected:backdrop"),
              f"background-color: mix(@theme_unfocused_selected_bg_color, {r['view']}, 0.5); color: {r['text']};"),
             (scoped(w, "treeview.view header button"), f"background-color: {r['chrome']}; color: {r['text']}; "
                                                        f"border-color: {r['border']};"),
             (scoped(w, "treeview.view header button:hover"), f"background-color: {r['hover']};"),
             (scoped(w, "rubberband", ".rubberband"),
              f"border: 1px solid {r['accent']}; background-color: alpha({r['accent']}, 0.2);"),
             # Thunar's tabs can be reordered; themes give those a translucent background.
             (scoped(w, "notebook > header tab"), f"background-color: transparent; border-color: transparent; color: {r['text']};"),
             (scoped(w, "notebook > header tab:hover"), f"background-color: {r['hover']};"),
             (scoped(w, "notebook > header tab:checked"), f"background-color: {r['view']}; box-shadow: inset 0 -3px {r['accent']};"),
             (scoped(w, ".frame", "frame > border", "notebook"), f"border-color: {r['border']};"),
             (scoped(w, "paned > separator"), f"background-color: transparent; background-image: image({r['border']});"),
             (scoped(w, "scrollbar"), f"background-color: {r['chrome']}; border-color: {r['border']};"),
             (scoped(w, "scrollbar.overlay-indicator:not(.dragging):not(.hovering)"),
              "background-color: transparent; border-color: transparent;"),
             (scoped(w, "scrollbar slider"), f"background-color: {r['slider']};"),
             (scoped(w, "scrollbar slider:hover"), f"background-color: {r['slider-hover']};"),
             (scoped(w, "scrollbar slider:active"), f"background-color: {r['accent']};")]
    return "/* Generated by linux-theming: thunar palette */\n" + stylesheet(rules)
