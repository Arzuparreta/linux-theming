"""GTK 3 stylesheet rules for the GTK 3 adapter. No GTK code is run.

The adapter appends its rules to the user's gtk-3.0/gtk.css. GTK lets that file
override the theme for every property it sets, whatever the selector, so a rule
that sets a background must also set the text color drawn on it.
"""

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
