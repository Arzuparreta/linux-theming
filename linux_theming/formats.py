"""Small, conservative native configuration editors. They never evaluate config."""

import re
import string
import tomllib


class LineError(ValueError):
    """A configuration line that stops editing; `line` is its 1-based number."""

    def __init__(self, message, line=None):
        super().__init__(message)
        self.line = line

    def at(self, path):
        """The same error, naming the file it was found in."""
        where = f"{path}:{self.line}" if self.line else str(path)
        return type(self)(f"{where}: {self}", self.line)


class Unreadable(LineError):
    """The owning program accepts this file, but Linux Theming cannot edit it yet.

    This is a current limitation of Linux Theming, not a problem with the user's configuration.
    """


def kde_header(text):
    """(group, locked) for a KConfig group header, or None when KConfig ignores it.

    Mirrors KConfigIniBackend::parseConfig: `[A][B]` names the nested group
    B of A and a final `[$i]` locks the group. The name is kept as written
    between the outer brackets, so `[` + name + `]` writes the same header.
    """
    parts, start, locked = [], 1, False
    while True:
        end = text.find("]", start)
        if end < 0:
            return None
        part = text[start:end]
        if part == "$i" and end + 1 == len(text):
            locked = True
        else:
            parts.append(part)
        if end + 1 < len(text) and text[end + 1] == "[":
            start = end + 2
            continue
        return "][".join(parts), locked


def kde_entry(text):
    """(key, value, locked, problem) for a KConfig entry line.

    Mirrors KConfigIniBackend::parseConfig. Keys may contain spaces; trailing
    `[$flags]` and one `[locale]` suffix are read from the end of the key. The
    locale stays part of the key, because `Name[de]` and `Name` are separate
    entries. A deleted entry (`Key[$d]`, written without `=`) has the value None.
    """
    key, equals, value = text.partition("=")
    key, value = key.strip(), value.strip()
    if not key:
        return None, None, False, "the key is empty"
    flags, locale = "", None
    while (start := key.rfind("[")) >= 0:
        end = key.find("]", start)
        if end < 0:
            return None, None, False, "a '[' in the key is not closed"
        inner = key[start + 1:end]
        if end > start + 1 and inner.startswith("$"):
            flags += inner[1:]
        elif locale is not None:
            return None, None, False, "the key has two language suffixes"
        else:
            locale = inner
        key = key[:start]
    if not equals and "d" not in flags:
        return None, None, False, "there is no '='"
    name = key if locale is None else f"{key}[{locale}]"
    return name, None if "d" in flags else value, "i" in flags, None


def ini_lines(text, *, root=False, kde=False):
    """Yield (number, line, section, key, value, locked) for the grammar we edit and merge.

    Headers have key None; comments and blank lines have section None. With
    `kde`, lines are read like KDE Frameworks' KConfig reads them (kdeglobals,
    konsolerc, Konsole profiles). Unsupported syntax and duplicates are
    rejected with their line number rather than guessed.
    """
    section = "" if root or kde else None
    seen_sections, seen_keys = set(), set()
    file_locked = section_locked = False
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        stripped = line.strip()
        comment = stripped.startswith("#") if kde else stripped.startswith(("#", ";"))
        if not stripped or comment:
            yield number, line, None, None, None, False
            continue
        if kde and stripped.startswith("["):
            header = kde_header(stripped)
            if header is None:
                raise LineError(f"{stripped!r} is not a valid KDE group header (no closing ']'). "
                                "KDE ignores this line too; correct or remove it, then try again.", number)
            section, locked = header
            if not section:
                file_locked |= locked
            section_locked = locked or file_locked
            if section in seen_sections:
                raise Unreadable(f"the group [{section}] appears more than once. KDE combines repeated "
                                 "groups, but Linux Theming cannot edit them yet.", number)
            seen_sections.add(section)
            yield number, line, section, None, None, section_locked
            continue
        if not kde:
            match = re.fullmatch(r"\[([^\[\]\r\n]+)\](?:\s*[#;].*)?", stripped)
            if match:
                section = match[1]
                if section in seen_sections:
                    raise LineError(f"Duplicate configuration section [{section}]; resolve it first.", number)
                seen_sections.add(section)
                yield number, line, section, None, None, False
                continue
        if kde:
            key, value, locked, problem = kde_entry(stripped)
            if problem:
                raise LineError(f"{stripped!r} is not a valid KDE setting ({problem}). "
                                "KDE ignores this line too; correct or remove it, then try again.", number)
            locked |= section_locked
        else:
            match = re.fullmatch(r"([\w./-]+)\s*=\s*(.*)", stripped)
            if not match or section is None:
                raise LineError(f"Unsupported configuration syntax on line {number}; export a theme "
                                "or normalize it first.", number)
            key, value, locked = match[1], match[2], False
        if (section, key) in seen_keys:
            if kde:
                raise Unreadable(f"the setting {key} appears more than once in [{section}]. KDE uses "
                                 "the last one, but Linux Theming cannot edit repeated settings yet.", number)
            raise LineError(f"Duplicate configuration key {key}; resolve it first.", number)
        seen_keys.add((section, key))
        yield number, line, section, key, value, locked


def ini_entries(text, *, root=False, kde=False):
    """Map (section, key) to its raw value; a deleted KDE entry maps to None."""
    return {(section, key): value for _, _, section, key, value, _ in ini_lines(text, root=root, kde=kde)
            if key is not None}


def locked_error(number, section, key=None):
    what = f"{key} in [{section}]" if key else f"[{section}]" if section else "this file"
    return LineError(f"{what} is locked with a [$i] marker (usually set by a system administrator). "
                     "Linux Theming does not change locked KDE settings.", number)


def ini(text, updates, *, root=False, same=None, kde=False):
    """Edit named keys without serializing comments or unrelated preferences.

    A value of None removes the key. `same(old, new)` keeps an existing line
    whose value already means the same, so a program's own spelling survives.
    With `kde`, settings KDE marks as locked are never changed.
    """
    section = "" if root or kde else None
    remaining = {name: dict(values) for name, values in updates.items()}
    result = []
    locks, file_lock = {}, None

    def entry(key, value):
        return [] if value is None else [f"{key}={value}\n"]

    def finish():
        values = remaining.pop(section, {})
        if any(value is not None for value in values.values()) and locks.get(section):
            raise locked_error(locks[section], section)
        for key, value in values.items():
            result.extend(entry(key, value))

    for number, line, current, key, value, locked in ini_lines(text, root=root, kde=kde):
        if current is not None and key is None:
            finish()
            section = current
            if locked:
                locks[section] = number
                if not section:
                    file_lock = number
        elif key is not None and key in remaining.get(section, {}):
            wanted = remaining[section].pop(key)
            if wanted is None or not (same and same(value, wanted)):
                if wanted != value and locked:
                    raise locked_error(number, section, key)
                result.extend(entry(key, wanted))
                continue
        result.append(line if line.endswith("\n") else line + "\n")
    finish()
    for name, values in remaining.items():
        lines = [line for key, value in values.items() for line in entry(key, value)]
        if lines:
            if file_lock:
                raise locked_error(file_lock, "")
            result.append((f"\n[{name}]\n" if result else f"[{name}]\n") if name else "")
            result.extend(lines)
    return "".join(result)


def drop_empty_sections(text, names, *, root=False, kde=False):
    """Remove the named sections when nothing but blank lines remain in them."""
    blocks, headers = [[]], [None]
    for _, line, section, key, _, _ in ini_lines(text, root=root, kde=kde):
        if section is not None and key is None:
            blocks.append([])
            headers.append(section)
        blocks[-1].append(line)
    result, dropped_last = [], False
    for header, block in zip(headers, blocks, strict=True):
        dropped_last = header in names and not any(line.strip() for line in block[1:])
        if not dropped_last:
            result.extend(block)
    while dropped_last and result and not result[-1].strip():
        result.pop()
    text = "".join(result)
    return text if text.strip() else ""


QT_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
              '"': '"', "?": "?", "'": "'", "\\": "\\"}


def utf16(text):
    data = text.encode("utf-16-le", "surrogatepass")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


def qsettings_string(value):
    """Spell a string the way Qt 5 QSettings writes it, which Qt 6 also reads.

    qt5ct and qt6ct rewrite their whole file when they save. Writing the same
    spelling keeps their saves from touching the values we manage.
    """
    if value.startswith("@"):
        value = "@" + value
    result, quote, hex_follows = [], False, False
    for unit in utf16(value):
        char = chr(unit)
        quote |= char in ";,="
        if hex_follows and char in string.hexdigits:
            result.append(f"\\x{unit:x}")
            continue
        hex_follows = False
        if char in '"\\':
            result.append("\\" + char)
        elif unit == 0:
            result.append("\\0")
            hex_follows = True
        elif char in "\a\b\f\n\r\t\v":
            result.append("\\" + next(k for k, v in QT_ESCAPES.items() if v == char))
        elif unit <= 0x1F or unit >= 0x7F:
            result.append(f"\\x{unit:x}")
            hex_follows = True
        else:
            result.append(char)
    text = "".join(result)
    return f'"{text}"' if quote or text[:1] == " " or text[-1:] == " " else text


def qsettings_value(raw):
    """Decode a QSettings INI value as Qt reads it: quotes, escapes and lists."""
    items, units, chop, quoted, inside, i = [], [], 0, False, False, 0

    def skip_spaces(position):
        while position < len(raw) and raw[position] in " \t":
            position += 1
        return position

    def finished():
        end = len(units)
        while not quoted and end > chop and units[end - 1] in (0x20, 0x09):
            end -= 1
        data = b"".join(unit.to_bytes(2, "little") for unit in units[:end])
        return data.decode("utf-16-le", "surrogatepass")

    i = skip_spaces(0)
    while i < len(raw):
        char = raw[i]
        i += 1
        if char == "\\":
            if i >= len(raw):
                break
            char = raw[i]
            i += 1
            if char in QT_ESCAPES:
                units.append(ord(QT_ESCAPES[char]))
            elif char in "x01234567":
                base, digits = (16, string.hexdigits) if char == "x" else (8, "01234567")
                value, found = (0, False) if char == "x" else (int(char), True)
                while i < len(raw) and raw[i] in digits:
                    value = (value * base + int(raw[i], base)) & 0xFFFF
                    found, i = True, i + 1
                if found:
                    units.append(value)
            chop = len(units)
        elif char == '"':
            quoted, inside = True, not inside
            if not inside:
                i = skip_spaces(i)
                chop = len(units)
        elif char == "," and not inside:
            items.append(finished())
            units, chop, quoted = [], 0, False
            i = skip_spaces(i)
        else:
            units.extend(utf16(char))
    value = finished()
    if items:
        return (*items, value)
    return value[1:] if value.startswith("@@") else value


def qsettings_same(left, right):
    return left == right or qsettings_value(left) == qsettings_value(right)


def sections(text, *, kde=False):
    if kde:
        return [section for _, _, section, key, _, _ in ini_lines(text, kde=True)
                if section and key is None]
    return re.findall(r"(?m)^\s*\[([^\[\]\r\n]+)\]\s*$", text)


def alacritty(text, rendered):
    """Modern TOML only; refuse legacy YAML and ambiguous/inline color tables."""
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid Alacritty TOML: {exc}") from exc
    if "import" in parsed or "import" in parsed.get("general", {}):
        raise ValueError("Alacritty imports require manual resolution or standalone export.")
    result, color_section = [], False
    for line in text.splitlines(keepends=True):
        if re.match(r"^\s*\[", line):
            header = re.fullmatch(r"\s*\[([\w.-]+)\]\s*(?:#.*)?", line.strip())
            if not header:
                raise ValueError("Quoted/array TOML tables require manual resolution or export.")
            color_section = header[1] == "colors" or header[1].startswith("colors.")
        if not color_section:
            if re.match(r"^\s*(?:colors\s*[.=]|['\"]colors['\"])", line):
                raise ValueError("Inline/dotted color tables require manual resolution or export.")
            result.append(line)
    value = "".join(result).rstrip() + "\n\n" + rendered
    tomllib.loads(value)
    # TOML multiline strings can imitate headers; verify every unrelated value.
    actual = tomllib.loads(value)
    if {k: v for k, v in parsed.items() if k != "colors"} != {
            k: v for k, v in actual.items() if k != "colors"}:
        raise ValueError("Complex TOML would change unrelated preferences; use export.")
    return value.lstrip("\n")


def append(text, rendered):
    return text.rstrip("\n") + ("\n\n" if text.rstrip("\n") else "") + rendered


IMPORTS_REFUSED = "Imported styles/themes require manual resolution or standalone export."
CSS_IMPORT = re.compile(r"""@import\s+(?:url\(\s*(?:"([^"\\]*)"|'([^'\\]*)'|([^\s"'()\\]+))\s*\)"""
                        r"""|"([^"\\]*)"|'([^'\\]*)')\s*;""")
DEFINE_COLOR = re.compile(r"\s*@define-color\s+[A-Za-z_][\w-]*\s+[^;{}]+;\s*")


def css_imports(text):
    """Literal @import targets, in order; anything else that imports is refused."""
    clean = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    if re.search(r"@theme\b", clean):
        raise ValueError(IMPORTS_REFUSED)
    targets = []
    for found in re.finditer(r"@import\b", clean):
        match = CSS_IMPORT.match(clean, found.start())
        if not match:
            raise ValueError(IMPORTS_REFUSED)
        targets.append(next(group for group in match.groups() if group is not None))
    return targets


def css_color_definitions(text):
    """True when a stylesheet only names colors, like KDE's generated colors.css."""
    clean = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "/*" not in clean and DEFINE_COLOR.sub("", clean).strip() == ""


def css(text, rendered, *, imported=None):
    """Preserve existing layout/styles and append scoped color overrides.

    `imported(target)` says whether an @import can stay: imports and custom
    themes may have higher specificity or reset a theme, and without a native
    CSS parser we only accept what the caller has read and verified.
    """
    if any(not (imported and imported(target)) for target in css_imports(text)):
        raise ValueError(IMPORTS_REFUSED)
    clean = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    if clean.count("{") != clean.count("}") or clean.count("/*"):
        raise ValueError("Unbalanced stylesheet; resolve it before connecting.")
    # Important declarations would defeat appended palette rules.
    if "!important" in clean:
        raise ValueError("Important stylesheet declarations require manual resolution or export.")
    return append(text, rendered)
