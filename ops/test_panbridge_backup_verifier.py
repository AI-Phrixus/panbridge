import json
import sqlite3
import unittest
from verify_panbridge_google_remote import compare, plan, EXPECTED


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            create table jobs(id integer,destination text,target_account_id text);
            create table files(id integer,job_id integer,relative_path text,size integer,
                status text,downloaded_bytes integer,uploaded_bytes integer,meta_json text);
            insert into jobs values(16,'google','test-account'),(18,'google','test-account');
        ''')
        self.items = []
        for original, jid in ((13,16),(14,18)):
            count, total = EXPECTED[jid]
            for index in range(count):
                size = 10 if index < count - 1 else total - (count - 1) * 10
                self.db.execute('insert into files values(?,?,?,?,?,?,?,?)',
                    (jid * 1000 + index, jid, f'folder/{index}', size, 'done', size, size,
                     json.dumps({'google_delivery': {'item_id': f'object-{jid}-{index}'}})))
            self.items.append({'original_job_id':original,'relative_path':'folder/0',
                               'bytes':10,'sha256':'a'*64,'backup_path':f'{original}/0.bin'})

    def tearDown(self):
        self.db.close()

    def test_unique_exact_mapping(self):
        jobs, entries = plan(self.db, self.items)
        self.assertEqual([e['result'] for e in entries], ['READY','READY'])
        self.assertEqual(set(jobs), {16,18})

    def test_ambiguous_path_rejected(self):
        self.db.execute("update files set relative_path='folder/0' where id=16001")
        self.assertEqual(plan(self.db,self.items)[1][0]['result'], 'AMBIGUOUS')

    def test_duplicate_original_rejected(self):
        with self.assertRaises(ValueError):
            plan(self.db,self.items+[self.items[0]])

    def test_changed_total_fails_closed(self):
        self.db.execute('update files set size=size+1 where id=16001')
        with self.assertRaises(ValueError):
            plan(self.db,self.items)

    def test_non_done_not_verified(self):
        self.db.execute("update files set status='uploading' where id=16000")
        self.assertEqual(plan(self.db,self.items)[1][0]['result'],'NOT_DONE')

    def test_done_without_full_bytes_rejected(self):
        self.db.execute('update files set uploaded_bytes=9 where id=16000')
        self.assertEqual(plan(self.db,self.items)[1][0]['result'],'INCOMPLETE_BYTES')

    def test_duplicate_delivery_rejects_both(self):
        self.db.execute('update files set meta_json=? where id=18000',
                        (json.dumps({'google_delivery':{'item_id':'object-16-0'}}),))
        self.assertEqual([e['result'] for e in plan(self.db,self.items)[1]],
                         ['DUPLICATE_DELIVERY_ID','DUPLICATE_DELIVERY_ID'])

    def test_unsafe_id_rejected(self):
        self.db.execute('update files set meta_json=? where id=16000',
                        (json.dumps({'google_delivery':{'item_id':'../private?token=bad'}}),))
        self.assertEqual(plan(self.db,self.items)[1][0]['result'],'DELIVERY_ID_MISSING')

    def test_google_metadata_all_gates(self):
        entry = {**self.items[0], 'google_file_id':'object-16-0'}
        good = {'id':'object-16-0','size':'10','trashed':False,'sha256Checksum':'a'*64}
        self.assertEqual(compare(entry,good),'MATCH')
        for patch, expected in (({'id':'other'},'ID_MISMATCH'),({'size':'11'},'GOOGLE_SIZE_MISMATCH'),
            ({'trashed':True},'TRASHED_OR_UNKNOWN'),({'trashed':None},'TRASHED_OR_UNKNOWN'),
            ({'sha256Checksum':None},'SHA256_MISSING'),({'sha256Checksum':'b'*64},'SHA256_MISMATCH')):
            with self.subTest(expected=expected):
                self.assertEqual(compare(entry,{**good,**patch}),expected)


if __name__ == '__main__':
    unittest.main()
