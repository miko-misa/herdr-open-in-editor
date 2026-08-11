import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import open_in_editor as plugin


class WorkspacePathTests(unittest.TestCase):
    def test_prefers_worktree_checkout(self):
        context = {
            "worktree": {"checkout_path": "/repo/worktrees/issue-12"},
            "workspace_cwd": "/repo",
            "focused_pane_cwd": "/repo/target",
        }
        self.assertEqual(
            plugin.select_workspace_path(context),
            "/repo/worktrees/issue-12",
        )

    def test_falls_back_to_workspace_then_pane(self):
        self.assertEqual(
            plugin.select_workspace_path(
                {"workspace_cwd": "/repo", "focused_pane_cwd": "/repo/subdir"}
            ),
            "/repo",
        )
        self.assertEqual(
            plugin.select_workspace_path({"focused_pane_cwd": "/repo/subdir"}),
            "/repo/subdir",
        )

    def test_rejects_relative_path(self):
        with self.assertRaises(plugin.OpenEditorError):
            plugin.select_workspace_path({"workspace_cwd": "relative"})


class CommandTests(unittest.TestCase):
    def test_vscode_remote_command_uses_ssh_authority(self):
        self.assertEqual(
            plugin.remote_editor_command("vscode", "workbox", "/repo/a b"),
            ["code", "--remote", "ssh-remote+workbox", "/repo/a b"],
        )

    def test_zed_remote_command_encodes_path(self):
        self.assertEqual(
            plugin.remote_editor_command(
                "zed",
                "ssh://me@example.com:2222",
                "/repo/a b",
            ),
            ["zed", "ssh://me@example.com:2222/repo/a%20b"],
        )

    def test_tunnel_command_keeps_target_as_one_argument(self):
        command = plugin.reverse_tunnel_start_command(
            "ssh",
            "/tmp/control",
            "me@example.com",
            47831,
            40123,
        )
        self.assertEqual(command[-1], "me@example.com")
        self.assertIn("127.0.0.1:47831:127.0.0.1:40123", command)


class RelayTests(unittest.TestCase):
    def test_relay_launches_remote_editor(self):
        launched = []
        relay = plugin.RelayServer(
            target="workbox",
            preferred_editor="zed",
            editor_executables={"zed": "/opt/zed"},
            launch=lambda command: launched.append(list(command)),
        )
        relay.start()
        try:
            plugin.send_relay_request(
                "127.0.0.1",
                relay.local_port,
                "/repo/worktree",
                "auto",
            )
        finally:
            relay.close()
        self.assertEqual(
            launched,
            [["/opt/zed", "ssh://workbox/repo/worktree"]],
        )

    def test_relay_rejects_relative_path(self):
        relay = plugin.RelayServer(
            target="workbox",
            preferred_editor="zed",
            editor_executables={"zed": "/opt/zed"},
            launch=lambda command: None,
        )
        relay.start()
        try:
            with self.assertRaises(plugin.OpenEditorError):
                plugin.send_relay_request(
                    "127.0.0.1",
                    relay.local_port,
                    "relative",
                    "zed",
                )
        finally:
            relay.close()


class RequestTests(unittest.TestCase):
    def test_local_request_falls_back_to_local_editor(self):
        context = json.dumps({"workspace_cwd": "/repo"})
        args = plugin.build_parser().parse_args(["request", "--editor", "zed"])
        with (
            mock.patch.dict(
                os.environ,
                {"HERDR_PLUGIN_CONTEXT_JSON": context},
                clear=True,
            ),
            mock.patch.object(
                plugin,
                "send_relay_request",
                side_effect=plugin.OpenEditorError("no relay"),
            ),
            mock.patch.object(plugin, "resolve_editor", return_value="zed"),
            mock.patch.object(plugin, "launch_detached") as launch,
        ):
            self.assertEqual(plugin.run_request(args), 0)
        launch.assert_called_once_with(["zed", "/repo"])

    def test_remote_request_requires_relay(self):
        context = json.dumps({"workspace_cwd": "/repo"})
        args = plugin.build_parser().parse_args(["request", "--editor", "zed"])
        with (
            mock.patch.dict(
                os.environ,
                {
                    "HERDR_PLUGIN_CONTEXT_JSON": context,
                    "SSH_CONNECTION": "127.0.0.1 1 127.0.0.1 2",
                },
                clear=True,
            ),
            mock.patch.object(
                plugin,
                "send_relay_request",
                side_effect=plugin.OpenEditorError("no relay"),
            ),
        ):
            with self.assertRaises(plugin.OpenEditorError):
                plugin.run_request(args)


class ArgumentParsingTests(unittest.TestCase):
    def test_attach_options_work_after_target(self):
        args = plugin.parse_cli_args(
            ["attach", "workbox", "--editor", "zed", "--remote-port", "47832"]
        )
        self.assertEqual(args.target, "workbox")
        self.assertEqual(args.editor, "zed")
        self.assertEqual(args.remote_port, 47832)
        self.assertEqual(args.herdr_args, [])

    def test_attach_options_work_before_target(self):
        args = plugin.parse_cli_args(["attach", "--editor", "vscode", "workbox"])
        self.assertEqual(args.target, "workbox")
        self.assertEqual(args.editor, "vscode")
        self.assertEqual(args.herdr_args, [])

    def test_attach_forwards_remaining_herdr_arguments(self):
        args = plugin.parse_cli_args(
            ["attach", "workbox", "--editor", "zed", "--", "--session", "demo"]
        )
        self.assertEqual(args.editor, "zed")
        self.assertEqual(args.herdr_args, ["--session", "demo"])


def _pane_list_reply(*panes):
    return json.dumps(
        {"id": "cli:pane:list", "result": {"panes": list(panes), "type": "pane_list"}}
    )


class MirrorPaneTests(unittest.TestCase):
    MAP = {
        "workspaces": {"w1": {"localId": "w0", "lastRemoteLabel": "scholion"}},
        "panes": {"w1:p1": {"localId": "w0:p2", "seq": 217, "reported": "claude"}},
    }

    def test_finds_the_host_and_remote_pane_behind_a_local_pane(self):
        self.assertEqual(
            plugin.find_mirrored_pane("w0:p2", {"buildbox": self.MAP}, "w0"),
            ("buildbox", "w1:p1"),
        )

    def test_an_ordinary_local_pane_is_not_mirrored(self):
        self.assertIsNone(plugin.find_mirrored_pane("w3:p1", {"buildbox": self.MAP}, "w3"))

    def test_a_stale_map_naming_a_recycled_pane_id_is_rejected(self):
        # Same local pane id, different workspace: the pane was closed and its
        # id handed to something unrelated. Matching anyway would open a remote
        # directory that has nothing to do with what the user is looking at.
        self.assertIsNone(plugin.find_mirrored_pane("w0:p2", {"buildbox": self.MAP}, "w7"))

    def test_an_incomplete_map_still_matches(self):
        # Absent corroboration is not a mismatch.
        without_workspaces = {"panes": self.MAP["panes"]}
        self.assertEqual(
            plugin.find_mirrored_pane("w0:p2", {"buildbox": without_workspaces}, "w0"),
            ("buildbox", "w1:p1"),
        )

    def test_malformed_map_entries_are_skipped_rather_than_raised_on(self):
        maps = {"broken": {"panes": {"w1:p1": "not-an-object"}}, "ok": self.MAP}
        self.assertEqual(
            plugin.find_mirrored_pane("w0:p2", maps, "w0"),
            ("ok", "w1:p1"),
        )

    def test_reads_the_working_directory_of_the_named_pane_only(self):
        output = _pane_list_reply(
            {"pane_id": "w1:p1", "cwd": "/home/dev/scholion"},
            {"pane_id": "w2:p1", "cwd": "/home/dev/anypages"},
        )
        self.assertEqual(
            plugin.parse_remote_pane_cwd(output, "w2:p1"),
            "/home/dev/anypages",
        )

    def test_tolerates_a_login_banner_ahead_of_the_json(self):
        output = "Welcome to buildbox\nLast login: yesterday\n" + _pane_list_reply(
            {"pane_id": "w1:p1", "cwd": "/home/dev/scholion"}
        )
        self.assertEqual(
            plugin.parse_remote_pane_cwd(output, "w1:p1"),
            "/home/dev/scholion",
        )

    def test_a_pane_that_vanished_remotely_is_an_error_not_a_wrong_path(self):
        output = _pane_list_reply({"pane_id": "w9:p9", "cwd": "/home/dev/other"})
        with self.assertRaises(plugin.OpenEditorError):
            plugin.parse_remote_pane_cwd(output, "w1:p1")

    def test_refuses_a_non_absolute_remote_path(self):
        output = _pane_list_reply({"pane_id": "w1:p1", "cwd": "relative/path"})
        with self.assertRaises(plugin.OpenEditorError):
            plugin.parse_remote_pane_cwd(output, "w1:p1")

    def test_the_remote_query_cannot_hang_on_a_password_prompt(self):
        command = plugin.remote_pane_list_command("buildbox")
        self.assertIn("BatchMode=yes", command)
        self.assertEqual(command[-2], "buildbox")

    def test_the_remote_herdr_is_not_assumed_to_be_on_the_path(self):
        # A non-interactive `ssh host herdr ...` never sourced the user's
        # profile, so an install under ~/.local/bin is invisible to it. This is
        # not hypothetical: of two mirrored hosts it broke one of them.
        remote = plugin.remote_pane_list_command("buildbox")[-1]
        self.assertIn("command -v herdr", remote)
        self.assertIn("~/.local/bin/herdr", remote)
        self.assertTrue(remote.endswith(" pane list"), remote)

    def test_the_fallback_never_relies_on_the_remote_login_shell(self):
        # fish and csh reject `$(...)`, and `ssh host cmd` hands the string to
        # whatever shell the user has. Only `sh -c '<literal>'` parses alike.
        remote = plugin.remote_pane_list_command("buildbox")[-1]
        self.assertTrue(remote.startswith("sh -c '"), remote)

    def test_an_explicitly_configured_remote_binary_wins(self):
        command = plugin.remote_pane_list_command("buildbox", "/opt/herdr/bin/herdr")
        self.assertEqual(command[-1], "/opt/herdr/bin/herdr pane list")

    def test_a_hostile_ssh_target_is_rejected(self):
        with self.assertRaises(plugin.OpenEditorError):
            plugin.remote_pane_list_command("-oProxyCommand=touch /tmp/pwned")

    def test_missing_mirror_state_leaves_the_plugin_unchanged(self):
        self.assertEqual(plugin.load_mirror_maps(Path("/nonexistent/herdr-mirror")), {})

    def test_a_remote_without_herdr_is_diagnosed_as_such(self):
        with self.assertRaises(plugin.OpenEditorError) as raised:
            plugin.parse_remote_pane_cwd("herdr: command not found\n", "w1:p1")
        self.assertIn("PATH", str(raised.exception))

    def test_resolves_a_mirror_pane_end_to_end(self):
        # Through the real state files rather than around them: this is the
        # only test that proves the map on disk reaches the editor argv.
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "buildbox-map.json").write_text(
                json.dumps(self.MAP), encoding="utf-8"
            )
            hosts = state / "hosts.toml"
            hosts.write_text('[hosts.buildbox]\ntarget = "buildbox"\n', encoding="utf-8")
            issued = []

            def runner(command):
                issued.append(command)
                return _pane_list_reply({"pane_id": "w1:p1", "cwd": "/home/dev/scholion"})

            resolved = plugin.resolve_mirror_location(
                {"focused_pane_id": "w0:p2", "workspace_id": "w0"},
                runner=runner,
                state_dir=state,
                hosts_file=hosts,
            )
        self.assertEqual(resolved, ("buildbox", "/home/dev/scholion"))
        self.assertEqual(issued, [plugin.remote_pane_list_command("buildbox")])
        self.assertEqual(
            plugin.remote_editor_command("zed", *resolved),
            ["zed", "ssh://buildbox/home/dev/scholion"],
        )

    def test_an_ordinary_pane_never_reaches_ssh(self):
        # The cost of the feature on the common path has to stay zero.
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "buildbox-map.json").write_text(
                json.dumps(self.MAP), encoding="utf-8"
            )

            def runner(command):
                raise AssertionError(f"unexpected SSH for a local pane: {command}")

            self.assertIsNone(
                plugin.resolve_mirror_location(
                    {"focused_pane_id": "w3:p1", "workspace_id": "w3"},
                    runner=runner,
                    state_dir=state,
                )
            )

    def test_a_context_without_a_pane_id_resolves_to_nothing(self):
        self.assertIsNone(plugin.resolve_mirror_location({"workspace_cwd": "/repo"}))


class MirrorHostsFileTests(unittest.TestCase):
    def settings(self, body, host="mini"):
        with tempfile.TemporaryDirectory() as directory:
            hosts = Path(directory) / "hosts.toml"
            hosts.write_text(body, encoding="utf-8")
            return plugin.mirror_host_settings(host, hosts)

    def test_prefers_the_configured_ssh_target_over_the_host_key(self):
        target, _ = self.settings('[hosts.mini]\ntarget = "dev@buildbox.internal"\n')
        self.assertEqual(target, "dev@buildbox.internal")

    def test_falls_back_to_the_host_key_when_unconfigured(self):
        target, _ = self.settings('[hosts.mini]\nprefix = "mini"\n')
        self.assertEqual(target, "mini")

    def test_honours_the_remote_binary_herdr_mirror_was_told_to_use(self):
        _, remote = self.settings(
            '[hosts.mini]\nremote_bin = "/opt/herdr/bin/herdr"\n'
        )
        self.assertEqual(remote, "/opt/herdr/bin/herdr")

    def test_falls_back_to_the_same_resolution_herdr_mirror_uses(self):
        _, remote = self.settings('[hosts.mini]\nprefix = "mini"\n')
        self.assertEqual(remote, plugin.MIRROR_REMOTE_HERDR)

    def test_a_missing_hosts_file_falls_back_to_the_defaults(self):
        self.assertEqual(
            plugin.mirror_host_settings("mini", Path("/nonexistent/hosts.toml")),
            ("mini", plugin.MIRROR_REMOTE_HERDR),
        )


if __name__ == "__main__":
    unittest.main()
