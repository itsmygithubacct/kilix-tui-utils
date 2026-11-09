# Install

Moved verbatim from the README.

```sh
./install.sh                      # into ~/.local/bin
KILIX_TUI_UTILS_PREFIX=/usr/local ./install.sh
make runtime-check                # immutable-package launcher closure
```

Each command is a small launcher that runs the tool from this checkout, so
updating is `git pull` rather than a reinstall.
`make runtime` builds the same closure under `.runtime/bin` without mutating
the user's Start menu; `kilix-content` uses that explicit target when one
package provides the full application suite.

The pixel interfaces use workspace checkouts under
`<source-root>/kilix-modules` (`../../kilix-modules` from this repository), or
normally installed copies of `kitty-frame-presenter`, `soft-raster-py`, and
`soft-raster` libraries. The Python binding is maintained under
[`soft-raster/python`](https://github.com/itsmygithubacct/soft-raster/tree/main/python),
not in the archived standalone `soft-raster-py` repository. Their text
fallbacks remain available when the graphical dependencies are absent.
