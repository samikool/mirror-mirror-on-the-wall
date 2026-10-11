# Themes

The TUI's colours come entirely from its theme. Pick one on the **Settings**
page or from the command palette (**Ctrl+P → Change theme**); the choice is
saved as `theme` under `[settings]` in `mmw.toml`.

Built in: `mmw` (default), `mmw-light`, and every Textual theme — `nord`,
`gruvbox`, `tokyo-night`, `dracula`, `catppuccin-mocha`, `rose-pine`,
`solarized-light`, `flexoki`, and more.

## Your own themes

Put `.toml` files in a `themes/` directory beside `mmw.toml`. Each file is one
theme, named after the file unless it sets `name`. `primary` is required;
anything left out is derived from it.

```toml
# themes/harbour.toml
dark = true
primary = "#4FA3C7"
secondary = "#9A8FD1"
accent = "#E3A857"
success = "#86C28B"
warning = "#E6C26E"
error = "#E0707A"
foreground = "#D9DEE6"
background = "#11161C"
surface = "#171E26"
panel = "#1F2833"

# Optional: mmw's own colour roles. Each defaults from the colours above.
[variables]
mmw-ok = "#86C28B"        # finished cleanly
mmw-warn = "#E6C26E"      # finished with something to look at
mmw-error = "#E0707A"     # failed
mmw-running = "#4FA3C7"   # in progress
mmw-idle = "#5C6773"      # not started
mmw-selected = "#E3A857"  # ticked rows
mmw-path = "#9A8FD1"      # paths and URIs
```

Colours must be concrete (`#rrggbb`, `rgb(…)`, or a colour name) — not
`$variables`. A file with a mistake is skipped with a message naming it; the
TUI still opens.
