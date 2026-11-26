import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import windows_clipboard as clipboard


class ClipboardBackupTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.multiple(clipboard, user=Mock(), ole=Mock(), release=Mock())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = clipboard.user
        self.opaque_formats = {
            0xC001: "DataObject",
            0xC002: "Ole Private Data",
            0xC003: "FileContents",
            0xC004: "FileGroupDescriptorW",
        }
        self.path_format = clipboard.CF_HDROP
        self.effect_format = 0xC005

    def set_formats(self, formats, unreadable=()):
        self.api.IsClipboardFormatAvailable.return_value = self.path_format in formats
        self.api.EnumClipboardFormats.side_effect = [*formats, 0]

        def get_name(fmt, buffer, size):
            buffer.value = formats[fmt]
            return len(buffer.value)

        self.api.GetClipboardFormatNameW.side_effect = get_name
        self.api.GetClipboardData.side_effect = lambda fmt: None if fmt in unreadable else fmt + 100
        clipboard.ole.OleDuplicateData.side_effect = lambda handle, fmt, flags: handle + 100

    def test_explorer_image_replaces_then_restores_original_paths(self):
        self.set_formats(
            {**self.opaque_formats, self.path_format: "", self.effect_format: "Preferred DropEffect"},
            unreadable=self.opaque_formats,
        )
        self.api.GetClipboardSequenceNumber.side_effect = [10, 18, 18]
        files = (Path("compressed.png"),)
        with (
            patch.object(clipboard, "file_drop", return_value=999) as file_drop,
            patch.object(clipboard, "global_bytes", return_value=998),
        ):
            with clipboard.temporary_files(1, files, 10):
                self.assertEqual(self.api.SetClipboardData.call_args_list[0].args, (self.path_format, 999))
            file_drop.assert_called_once_with(files)
        self.assertEqual(self.api.EmptyClipboard.call_count, 2)
        self.assertEqual(
            [call.args for call in self.api.SetClipboardData.call_args_list[-2:]],
            [(self.path_format, self.path_format + 200), (self.effect_format, self.effect_format + 200)],
        )
        self.assertEqual(
            [call.args[0] for call in self.api.GetClipboardData.call_args_list],
            [self.path_format, self.effect_format],
        )

    def test_virtual_file_without_paths_still_requires_a_backup(self):
        self.set_formats({0xC003: "FileContents"}, unreadable={0xC003})
        with self.assertRaisesRegex(ValueError, "无法备份"):
            clipboard.snapshot({})

    def test_unreadable_original_paths_leave_clipboard_intact(self):
        self.set_formats({self.path_format: ""}, unreadable={self.path_format})
        self.api.GetClipboardSequenceNumber.return_value = 10
        with patch.object(clipboard, "file_drop", return_value=999):
            with self.assertRaisesRegex(ValueError, "无法备份"):
                with clipboard.temporary_files(1, (Path("compressed.png"),), 10):
                    self.fail("Clipboard was replaced without a backup")
        self.api.EmptyClipboard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
