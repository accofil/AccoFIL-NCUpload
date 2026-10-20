"""System package setup respects installed packages and APT pinning."""
import subprocess
import unittest
from unittest import mock

from ncore_up import installer


class SystemPackagesTests(unittest.TestCase):
    def test_installed_and_pinned_packages_are_preserved(self):
        def run(args, **kwargs):
            if args[0] == "dpkg-query":
                status = {"ffmpeg": b"install ok installed", "held": b"hold ok installed"}.get(args[-1])
                return subprocess.CompletedProcess(args, 0 if status else 1, status or b"")
            if args[0] == "apt-cache":
                self.assertEqual(kwargs["env"]["LC_ALL"], "C")
                candidate = b"1.0" if args[-1] == "new-tool" else b"(none)"
                return subprocess.CompletedProcess(args, 0, b"  Candidate: " + candidate + b"\n")
            return subprocess.CompletedProcess(args, 0)

        with mock.patch.object(installer, "APT_PACKAGES", ["ffmpeg", "held", "new-tool", "missing"]), \
             mock.patch.object(installer.shutil, "which", return_value="/usr/bin/apt-get"), \
             mock.patch.object(installer.os, "geteuid", return_value=1000), \
             mock.patch.object(installer.subprocess, "run", side_effect=run) as calls, \
             mock.patch("builtins.print"):
            installer.system_packages()
        installs = [call.args[0] for call in calls.call_args_list if "install" in call.args[0]]
        self.assertEqual(installs, [["sudo", "apt-get", "install", "--no-remove", "-y", "new-tool"]])
        queried = [call.args[0][-1] for call in calls.call_args_list if call.args[0][0] == "apt-cache"]
        self.assertEqual(queried, ["new-tool", "missing"])

    def test_no_install_command_when_all_packages_are_installed(self):
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, b"install ok installed")

        with mock.patch.object(installer, "APT_PACKAGES", ["ffmpeg"]), \
             mock.patch.object(installer.shutil, "which", return_value="/usr/bin/apt-get"), \
             mock.patch.object(installer.os, "geteuid", return_value=1000), \
             mock.patch.object(installer.subprocess, "run", side_effect=run) as calls, \
             mock.patch("builtins.print"):
            installer.system_packages()
        self.assertFalse(any("install" in call.args[0] for call in calls.call_args_list))
