import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from graft.core import Manager, GraftError


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = Manager(Path(self.tmp.name))
        self.a = self.manager.add_profile('source')
        self.b = self.manager.add_profile('destination')
        self.manager.set_group('test', [self.a['id'], self.b['id']])
        self.paths = [self.manager.profile_path(p['id']) for p in [self.a, self.b]]
        self.folders = []
        for path in self.paths:
            (path/'config.json').write_text(json.dumps({'lastKnownAccountUuid': 'account-12345678'}))
            folder = path/'claude-code-sessions/account-12345678/organization-12345678'
            folder.mkdir(parents=True)
            self.folders.append(folder)
        self.name = 'local_12345678.json'
        self.cli = '99209ba9-a2f8-45e8-8633-efa0d0f78082'
        self.relative = Path('.claude-code/projects/project')/(self.cli+'.jsonl')
        self.transcript = b'{"type":"user","message":{"content":"fixture"}}\n'
        self.record = {'sessionId': 'local_12345678', 'cliSessionId': self.cli, 'title': 'fixture'}
        (self.folders[0]/self.name).write_text(json.dumps(self.record))
        source = self.paths[0]/self.relative
        source.parent.mkdir(parents=True)
        source.write_bytes(self.transcript)
        self.source = source
        self.destination = self.paths[1]/self.relative
        self.closed = patch('graft.platform.running_claudes', return_value=[])
        self.closed.start()
        self.addCleanup(self.closed.stop)

    def test_metadata_and_transcript_transfer_and_idempotence(self):
        report = self.manager.merge()
        self.assertEqual(self.destination.read_bytes(), self.transcript)
        self.assertEqual(json.loads((self.folders[1]/self.name).read_text())['cliSessionId'], self.cli)
        self.assertEqual(report['transcriptsCopied'], 1)
        self.assertEqual(self.manager.merge()['transcriptsCopied'], 0)
        self.assertEqual(self.source.read_bytes(), self.transcript)

    def test_dropped_resume_handle_repaired(self):
        broken = dict(self.record)
        broken.pop('cliSessionId')
        broken['transcriptUnavailable'] = True
        (self.folders[1]/self.name).write_text(json.dumps(broken))
        self.manager.merge()
        fixed = json.loads((self.folders[1]/self.name).read_text())
        self.assertEqual(fixed['cliSessionId'], self.cli)
        self.assertNotIn('transcriptUnavailable', fixed)
        self.assertEqual(self.destination.read_bytes(), self.transcript)

    def test_subagent_transfer(self):
        rel = Path('.claude-code/projects/project')/self.cli/'subagents/agent-abcdef.jsonl'
        source = self.paths[0]/rel
        source.parent.mkdir(parents=True)
        source.write_bytes(self.transcript)
        self.manager.merge()
        self.assertEqual((self.paths[1]/rel).read_bytes(), self.transcript)

    def test_divergent_destination_preserved(self):
        self.destination.parent.mkdir(parents=True)
        other = b'{"type":"user","message":{"content":"different"}}\n'
        self.destination.write_bytes(other)
        broken = dict(self.record, transcriptUnavailable=True)
        broken.pop('cliSessionId')
        (self.folders[1]/self.name).write_text(json.dumps(broken))
        report = self.manager.merge()
        self.assertTrue(any('diverg' in w for w in report['warnings']))
        self.assertEqual(self.destination.read_bytes(), other)
        self.assertTrue(json.loads((self.folders[1]/self.name).read_text())['transcriptUnavailable'])

    def test_interrupted_transfer_recovers(self):
        import graft.core as core
        original = core._atomic
        def interrupt(path, data):
            if path == self.destination:
                raise OSError('fixture interrupted transfer')
            return original(path, data)
        with patch('graft.core._atomic', side_effect=interrupt):
            with self.assertRaises(OSError):
                self.manager.merge()
        self.assertTrue(self.manager.pending_file.exists())
        self.manager.merge()
        self.assertFalse(self.manager.pending_file.exists())
        self.assertEqual(self.destination.read_bytes(), self.transcript)

    def test_journal_rejects_arbitrary_files(self):
        for path in ('../secret.jsonl', 'Claude-Graft-'+self.b['id']+'/.claude-code/projects/project/credentials.json'):
            with self.assertRaises(GraftError):
                self.manager._transaction_path(path)


if __name__ == '__main__':
    unittest.main()

