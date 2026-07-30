import json
import os
import unittest
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


if __name__ == "__main__":
    unittest.main()
