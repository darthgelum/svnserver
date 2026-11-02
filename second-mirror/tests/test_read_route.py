import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from read_route import route


class ReadRouteTests(unittest.TestCase):
    def test_missing_revision_switches_to_lan_without_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            current = data / 'repositories/Example/db/current'
            current.parent.mkdir(parents=True)
            current.write_text('21\n')
            self.assertEqual(route('Example/21', data), 'local')
            self.assertEqual(route('Example/22', data), 'master')
            current.write_text('22\n')
            self.assertEqual(route('Example/22', data), 'local')
            current.write_text('invalid')
            self.assertEqual(route('Example/22', data), 'master')

    def test_invalid_keys_never_resolve_a_filesystem_path(self):
        for key in ('../22', 'Example/-1', 'Example/x', 'Example/22/../secret', 'Example/1\n'):
            self.assertEqual(route(key), 'NULL')


if __name__ == '__main__':
    unittest.main()
