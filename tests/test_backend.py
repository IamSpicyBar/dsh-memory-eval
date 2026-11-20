import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend


class BackendBoundaryTests(unittest.TestCase):
    def test_model_prompt_contains_only_declared_input_lines_and_selected_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'source.txt').write_text('permitted source\nexcluded source\n')
            (root / 'lesson-test.lesson.md').write_text('candidate experience')
            task = {'prompt': 'implement the clock', 'targets': ['clock.ts'], 'inputs': [{'path': 'source.txt', 'lines': [1, 1]}],
                    'checks': [{'id': 'private-check', 'kind': 'contains_all', 'values': ['grader-only-secret']}], 'lessons': ['lesson-test']}
            before = backend.build_prompt(task, root, root, False); after = backend.build_prompt(task, root, root, True)
            self.assertIn('permitted source', before); self.assertNotIn('excluded source', before)
            self.assertNotIn('grader-only-secret', after); self.assertNotIn('candidate experience', before)
            self.assertIn('candidate experience', after)

    def test_invalid_spans_and_output_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'source.txt').write_text('source')
            for span in ([0, 1], [2, 1], [1, 2], [True, 1]):
                with self.assertRaises(ValueError): backend.build_prompt({'prompt': 'p', 'targets': ['f'], 'inputs': [{'path': 'source.txt', 'lines': span}]}, root, root, False)
        for name in ('../clock.ts', '/clock.ts', '.agent-replay/replay.json', 'a\\clock.ts'):
            with self.assertRaises(ValueError): backend.relative_target(name)

    def test_unknown_checks_and_duplicate_model_outputs_fail_closed(self):
        with self.assertRaises(ValueError): backend.run_check({'kind': 'unknown'}, 'content')
        with self.assertRaises(ValueError): backend.extract_files('===FILE: clock.ts===\na\n===END===\n===FILE: clock.ts===\nb\n===END===')


if __name__ == '__main__': unittest.main()
