import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from preflight import ATTACHMENT_OVERHEAD, ENCODED_BATCH_LIMIT, FILE_LIMIT
from windows_picker import Dialog, FilePicker, compress_selection, selected_names


class FilePickerTests(unittest.TestCase):
    def test_parse_names_with_spaces_and_multiple_absolute_paths(self):
        self.assertEqual(selected_names("report image.png"), ("report image.png",))
        self.assertEqual(
            selected_names(r'"C:\one dir\image.png" "D:\other dir\notes.txt"'),
            (r"C:\one dir\image.png", r"D:\other dir\notes.txt"),
        )
        self.assertEqual(selected_names(' "unclosed'), ())

    def test_mixed_selection_uses_one_image_budget_and_preserves_non_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ("first image.png", "notes.txt", "second image.png")
            note = root / names[1]
            # Both images would fill most of the individual budget before the batch is considered.
            image = Image.effect_noise((1200, 900), 60).convert("RGB")
            for name in (names[0], names[2]):
                image.save(root / name)
            note.write_text("test file", encoding="utf-8")
            files = compress_selection(names, f"地址: {root}")
            self.assertEqual(files[1], note)
            attachments = (files[0], files[2])
            try:
                self.assertTrue(all(file.parent != root for file in attachments))
                sizes = [file.stat().st_size for file in attachments]
                self.assertLessEqual(max(sizes), FILE_LIMIT)
                estimate = 4 * sum((size + 2) // 3 for size in sizes)
                self.assertLessEqual(estimate + len(sizes) * ATTACHMENT_OVERHEAD, ENCODED_BATCH_LIMIT)
            finally:
                for file in attachments:
                    file.unlink()
                attachments[0].parent.rmdir()

    def test_a_changed_selection_is_not_replaced_after_compression(self):
        dialog, future = Mock(), Mock()
        dialog.selection.return_value = ("other.png", r"Address: C:\Images")
        picker = FilePicker(Mock())
        with patch.object(picker, "active", return_value=dialog):
            picker.job = dialog, ("original.png", r"Address: C:\Images"), future
            picker.finish()
        self.assertIsNone(picker.job)
        dialog.confirm.assert_not_called()

    def test_dialog_is_discovered_after_its_controls_finish_loading(self):
        picker = FilePicker(Mock())
        dialog = Mock()
        with (
            patch("windows_picker.win32gui.GetForegroundWindow", return_value=123),
            patch("windows_picker.monotonic", side_effect=[1, 1.1]),
            patch.object(Dialog, "find", side_effect=[None, dialog]) as find,
        ):
            picker.tick()
            self.assertIsNone(picker.dialog)
            picker.tick()
        self.assertIs(picker.dialog, dialog)
        self.assertEqual(find.call_count, 2)


if __name__ == "__main__":
    unittest.main()
