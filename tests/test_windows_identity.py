import os
import tempfile
import unittest
from tokeneconomizer.plugin import paths


class WindowsIdentityTests(unittest.TestCase):
    def test_local_remote_spellings_share_identity(self):
        expected = 'c:/work/sample/repo'
        self.assertEqual(paths.normalize_remote(r'C:\Work\Sample\Repo'), expected)
        self.assertEqual(paths.normalize_remote(r'C:\\Work\\Sample\\Repo'), expected)
        self.assertEqual(paths.normalize_remote('C:/Work/Sample/Repo.git'), expected)
        self.assertEqual(paths.normalize_remote('git@example.com:Owner/Repo.git'), 'example.com/owner/repo')
        self.assertEqual(paths.normalize_remote('https://example.com/Owner/Repo'), 'example.com/owner/repo')

    def test_state_writers_accept_windows_labels(self):
        from tokeneconomizer.microskills.distiller import Distiller
        from tokeneconomizer.microskills.envfacts import EnvFactsDistiller
        from tokeneconomizer.toolprune.codex import CodexUntoolProfile
        from tokeneconomizer.plugin.repos import _rename_label
        label = r'C:\Work\Sample\Repo'
        with tempfile.TemporaryDirectory() as directory:
            for writer in (Distiller(label), EnvFactsDistiller(label)):
                writer.save(directory)
                self.assertTrue(os.path.isfile(writer._path(directory, label)))
            profile = CodexUntoolProfile(label, directory, fresh=True)
            self.assertNotIn(':', os.path.basename(os.path.dirname(profile.path)))
            _rename_label(directory, label)

    def test_repo_labels_are_valid_single_filenames(self):
        from tokeneconomizer.names import repo_filename
        for label in ('owner/repo', r'C:\Work\Repo', 'owner:repo', 'name?bad', ''):
            filename = repo_filename(label) + '.json'
            self.assertFalse(any(c in filename for c in '<>:"/\\|?*'))
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, filename)
                with open(path, 'w', encoding='utf-8') as file:
                    file.write('{}')
                self.assertTrue(os.path.isfile(path))
        self.assertEqual(repo_filename('owner/repo'), 'owner__repo')
        self.assertEqual(repo_filename('.dotfiles'), '.dotfiles')


if __name__ == '__main__':
    unittest.main()
