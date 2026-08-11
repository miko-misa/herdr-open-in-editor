# Open in Editor

A local Herdr plugin that opens the active workspace checkout in VS Code or
Zed. The same action supports both local Herdr sessions and `herdr --remote`.

Repository: `timofey-TK/herdr-open-in-editor`

The selected path is, in order:

1. the remote checkout, when the focused pane is a [herdr-mirror] pane
   (see [Mirrored panes](#mirrored-panes));
2. the current worktree `checkout_path`;
3. the workspace cwd;
4. the focused pane cwd.

[herdr-mirror]: https://github.com/nikok6/herdr-mirror

## Install the plugin

Install it on every machine where the Herdr server runs:

```bash
herdr plugin install timofey-TK/herdr-open-in-editor
```

During local development, link a checkout instead:

```bash
herdr plugin link /absolute/path/to/herdr-open-in-editor
```

For a remote Herdr server, run the install command on the remote host.

## Keybinding

Add one action to the Herdr config used by the server:

```toml
[[keys.command]]
key = "prefix+shift+o"
type = "plugin_action"
command = "timofey-tk.open-in-editor.open"
description = "open workspace in editor"
```

Use `timofey-tk.open-in-editor.open-vscode` or
`timofey-tk.open-in-editor.open-zed` to select an editor explicitly.
`prefix+o` is not used here because it is Herdr's default
`open_notification_target` binding.

Reload the config or restart Herdr after adding the keybinding.

## Local sessions

Run Herdr normally. The action first probes for a relay and, when none exists,
launches `zed` or `code` directly on the same machine:

```bash
herdr
```

The automatic action prefers Zed when both CLIs are installed. Bind an explicit
action if a fixed choice is preferable.

## Remote sessions

The plugin action runs on the remote Herdr server, so a small reverse relay is
needed to ask the local machine to launch its editor. Clone this repository on
the local machine:

```bash
git clone https://github.com/timofey-TK/herdr-open-in-editor.git \
  ~/.local/share/herdr-open-in-editor
```

Then use the included attach wrapper instead of calling `herdr --remote`
directly:

```bash
~/.local/share/herdr-open-in-editor/open_in_editor.py \
  attach workbox --editor zed
```

For VS Code:

```bash
~/.local/share/herdr-open-in-editor/open_in_editor.py \
  attach workbox --editor vscode
```

To keep the original `herdr --remote workbox` command, add this transparent
wrapper to `~/.zshrc`:

```zsh
herdr() {
  if [[ "$1" == "--remote" && -n "$2" ]]; then
    local target="$2"
    shift 2

    "$HOME/.local/share/herdr-open-in-editor/open_in_editor.py" \
      attach "$target" --editor zed "$@"
  else
    command herdr "$@"
  fi
}
```

Reload the shell with `source ~/.zshrc`. Local commands still run the real
binary; only `herdr --remote <target>` starts the editor relay. Use
`command herdr --remote <target>` to bypass the wrapper explicitly.

The wrapper:

1. starts a loopback-only local relay;
2. creates an SSH reverse forward on remote port `47831`;
3. runs `herdr --remote workbox --remote-keybindings server`;
4. removes the tunnel when Herdr exits.

Use an SSH config alias such as `workbox`. VS Code passes that alias to
`code --remote ssh-remote+workbox`; Zed opens
`ssh://workbox/<remote-path>`.

If port `47831` is occupied on the remote host, choose another port on both
sides. The wrapper accepts `--remote-port`; the remote action reads
`HERDR_OPEN_EDITOR_RELAY_PORT` from the Herdr server environment.

To keep local Herdr keybindings during remote attach, start only the relay:

```bash
~/.local/share/herdr-open-in-editor/open_in_editor.py \
  relay workbox --editor zed
herdr --remote workbox
```

Plugin-action keybindings are server-side commands and are not included in the
local remote-keybinding snapshot, so invoke the action through the remote
server config or with:

```bash
herdr plugin action invoke timofey-tk.open-in-editor.open
```

## Mirrored panes

[herdr-mirror] streams a pane from another host into a local workspace. Herdr
reports that pane's path as the local placeholder the stream is painted into,
so without help this action would open
`~/.local/state/herdr-mirror/.mirror-pane` rather than the project on screen.

When the focused pane is a mirrored one, the plugin resolves where it really
is and opens it over SSH — the same `ssh://host/path` and
`--remote ssh-remote+host` commands the relay already uses:

```
mini: scholion   →  zed  ssh://buildbox/home/dev/scholion
                    code --remote ssh-remote+buildbox /home/dev/scholion
```

Nothing has to be configured. The pane is looked up in herdr-mirror's own
`~/.local/state/herdr-mirror/<host>-map.json`, and the SSH target, along with
`remote_bin` if you set one, comes from `~/.config/herdr-mirror/hosts.toml`.
The remote host is then asked for the pane's working directory with
`herdr pane list`, resolved the way herdr-mirror resolves it: the PATH first,
then `~/.local/bin/herdr`. A non-interactive `ssh host herdr …` never sourced
the login profile, so an install under `~/.local/bin` is otherwise invisible
to it.

Passwordless SSH to the mirrored host is the one requirement, which
herdr-mirror already needs.

Without herdr-mirror installed there is nothing to find, and an ordinary local
workspace never reaches this path at all, so behaviour is unchanged and no SSH
is attempted.

Two failures are worth recognising:

- *herdr is not installed where … could find it* — neither the PATH nor
  `~/.local/bin` has it. Set `remote_bin` for that host in
  `~/.config/herdr-mirror/hosts.toml`; herdr-mirror reads the same key.
- *the mirrored host did not answer `herdr pane list`* — it replied, but with
  something other than a pane list. `ssh <host> 'herdr pane list'` shows what.
- *mirrored pane … is gone from the remote Herdr* — the map is stale. Run
  `herdr-mirror once`, or start the daemon.

## Tests

```bash
python3 -m unittest -v test_open_in_editor.py
```
