# Prompt for the local-machine agent

Use the prompt below on the machine where VS Code or Zed is installed. Replace
`<SSH_ALIAS>` with the same SSH config alias used for `herdr --remote`.

```text
Set up and validate the Herdr Open in Editor plugin for both local and remote
workflows.

Repository:
https://github.com/timofey-TK/herdr-open-in-editor

Remote Herdr target:
<SSH_ALIAS>

Preferred editor:
zed

Requirements:

1. Do not patch or rebuild Herdr.
2. Do not overwrite unrelated Herdr or SSH configuration.
3. Check that `herdr`, `ssh`, `python3`, and the preferred editor CLI are
   available locally. For Zed verify `zed`; for VS Code verify `code`.
4. Clone or update the plugin at
   `~/.local/share/herdr-open-in-editor`.
5. Run:
   `python3 -m unittest -v test_open_in_editor.py`
   from that checkout.
6. On the remote host, install the plugin with:
   `herdr plugin install timofey-TK/herdr-open-in-editor --yes`
   Then verify:
   `herdr plugin action list --plugin timofey-tk.open-in-editor`
7. Add this keybinding to the Herdr config used by the remote server, preserving
   all existing config:

   [[keys.command]]
   key = "prefix+shift+o"
   type = "plugin_action"
   command = "timofey-tk.open-in-editor.open"
   description = "open workspace in editor"

   Check for a conflict first. If the key is already used, stop and ask me which
   binding to use instead.
8. Reload the remote Herdr config.
9. Add the transparent `herdr` shell function documented in README to the
   local shell config without overwriting unrelated configuration. Start the
   remote session with the unchanged command:
   `herdr --remote <SSH_ALIAS>`
   Configure the function with `--editor vscode` instead if VS Code is
   preferred.
10. In Herdr, focus a workspace backed by a linked worktree and invoke
    `prefix+shift+o`. Verify that the LOCAL editor opens the exact remote
    worktree checkout, not the main repository and not merely the pane cwd.
11. Repeat once with a normal non-worktree workspace.
12. Report the exact commands run, the editor command produced, the opened
    remote path, and any remaining caveats. Do not claim remote validation if
    the editor did not actually open.

The wrapper must create its own temporary SSH reverse tunnel; do not add a
permanent `RemoteForward` to `~/.ssh/config`.
```
