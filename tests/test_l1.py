import importlib.util, json, tempfile, unittest, copy
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import l1

class L1EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.item = {"id": "lesson-test", "lesson": {"how": ["Use an elapsed clock."], "why": "Wall clock can jump."}, "evidence": [{"claim": "The observed clock was Date.now()."}], "base": None, "neighbors": []}
        self.docs = {"documents": [{"uri": "official://clock", "text": "ACTIVE excludes deep sleep."}]}
        self.reply = {"rules": [{"rule": "R" + str(n), "verdict": "pass", "lessonQuote": "Use an elapsed clock.", "evidenceQuote": "The observed clock was Date.now().", "reason": "Bounded evidence"} for n in range(1, 8)], "facts": [{"claim": "", "verdict": "n/a", "reason": "No HarmonyOS API assertion"}]}
    def test_exact_quotes_and_all_rules_required(self):
        l1.valid_reply(self.item, self.docs, self.reply)
        self.reply["rules"][0]["evidenceQuote"] = "Invented evidence"
        with self.assertRaises(ValueError): l1.valid_reply(self.item, self.docs, self.reply)
        self.reply["rules"] = self.reply["rules"][1:]
        with self.assertRaises(ValueError): l1.valid_reply(self.item, self.docs, self.reply)
    def test_r8_cannot_assert_support_without_official_quote(self):
        self.reply["facts"] = [{"claim": "Use an elapsed clock.", "verdict": "supported", "doc": "official://clock", "docQuote": "Invented declaration", "reason": "Claim"}]
        with self.assertRaises(ValueError): l1.valid_reply(self.item, self.docs, self.reply)
    def test_quotes_match_original_strings_including_newlines_and_double_quotes(self):
        self.item["lesson"]["how"] = ['Use "elapsed"\nclock.']
        self.item["evidence"][0]["claim"] = 'Observed "Date.now()"\nclock.'
        for row in self.reply["rules"]:
            row.update(lessonQuote='"elapsed"\nclock', evidenceQuote='"Date.now()"\nclock')
        l1.valid_reply(self.item, self.docs, self.reply)
    def test_missing_r8_is_not_a_pass(self):
        self.reply["facts"] = []
        with self.assertRaises(ValueError): l1.valid_reply(self.item, self.docs, self.reply)
    def test_quote_ids_resolve_only_exact_frozen_strings(self):
        catalog=l1.quote_catalog(self.item,self.docs)
        lesson_id=next(k for k,v in catalog['lesson'].items() if v=='Use an elapsed clock.')
        evidence_id=next(k for k,v in catalog['evidence'].items() if v=='The observed clock was Date.now().')
        for row in self.reply['rules']:
            row.pop('lessonQuote');row.pop('evidenceQuote');row.update(lessonQuoteId=lesson_id,evidenceQuoteId=evidence_id)
        result=l1.valid_reply(self.item,self.docs,copy.deepcopy(self.reply),catalog)
        self.assertEqual(result['rules'][0]['lessonQuote'],'Use an elapsed clock.')
        bad=copy.deepcopy(self.reply);bad['rules'][0]['lessonQuoteId']='invented'
        with self.assertRaises(ValueError):l1.valid_reply(self.item,self.docs,bad,catalog)
        bad=copy.deepcopy(self.reply);bad['rules'][0]['lessonQuote']='rewritten'
        with self.assertRaises(ValueError):l1.valid_reply(self.item,self.docs,bad,catalog)
    def test_document_ids_bind_the_claim_and_official_document(self):
        catalog=l1.quote_catalog(self.item,self.docs)
        lesson_id=next(k for k,v in catalog['lesson'].items() if v=='Use an elapsed clock.')
        doc_id=next(iter(catalog['documents']))
        self.reply['facts']=[{'claimQuoteId':lesson_id,'docQuoteIds':[doc_id],'verdict':'supported','reason':'Exact frozen fixture'}]
        result=l1.valid_reply(self.item,self.docs,copy.deepcopy(self.reply),catalog)
        self.assertEqual(result['facts'][0]['docQuote'],'ACTIVE excludes deep sleep.')
        bad=copy.deepcopy(self.reply);bad['facts'][0]['docQuoteIds']=['unknown']
        with self.assertRaises(ValueError):l1.valid_reply(self.item,self.docs,bad,catalog)
        bad=copy.deepcopy(self.reply);bad['facts'][0]['claimQuoteId']='unknown'
        with self.assertRaises(ValueError):l1.valid_reply(self.item,self.docs,bad,catalog)
    def test_redundant_r8_rule_remains_invalid(self):
        self.reply['rules'].append({'rule':'R8','verdict':'pass','reason':'Redundant'})
        with self.assertRaises(ValueError):l1.valid_reply(self.item,self.docs,self.reply)
    def test_malformed_rows_do_not_crash_scoring(self):
        for reply in [[],{'rules':['R1'],'facts':[]},{'rules':[],'facts':None}]:
            with self.assertRaises(ValueError):l1.valid_reply(self.item,self.docs,reply)
    def test_three_valid_uncalibrated_votes_still_need_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"suite"; replies=Path(temp)/"replies";root.mkdir(); replies.mkdir()
            item=self.item
            prompt=l1.prompt(item,self.docs)
            suite={"schemaVersion":"hmbot.memory.l1-suite/1","memoryRevision":"r","settings":{"judgeModel":"judge"},"items":[item],"facts":self.docs,"lint":{"lessons_with_open_hits":0},"rulesSha256":"rules"}
            l1.write(root/"suite.json",suite)
            (root/"lesson-test.prompt.txt").write_text(prompt)
            l1.write(root/"frozen.json",l1.manifest(root))
            for n in range(3):
                text=json.dumps(self.reply)
                (replies/f"lesson-test.{n}.txt").write_text(text)
                l1.write(replies/f"lesson-test.{n}.meta.json",{"stop_reason":"end_turn","model":"judge","suiteSha256":l1.sha((root/"suite.json").read_bytes()),"promptSha256":l1.sha(prompt.encode()),"replySha256":l1.sha(text.encode())})
            report=l1.score(root,replies)
            self.assertTrue(report["complete"]);self.assertFalse(report["accepted"]);self.assertTrue(report["needsReview"])
            suite['calibration']={'protocol':l1.PROTOCOL,'judgeModel':'judge','rulesSha256':'rules','agreement':{key:1.0 for key in l1.BLOCKING},'humanLabelsSha256':'fixture-labels-only'}
            l1.write(root/'suite.json',suite);(root/'frozen.json').unlink();l1.write(root/'frozen.json',l1.manifest(root))
            for n in range(3):
                self.reply['rules'][1]['verdict']='fail'
                text=json.dumps(self.reply);(replies/f'lesson-test.{n}.txt').write_text(text)
                l1.write(replies/f'lesson-test.{n}.meta.json',{'stop_reason':'end_turn','model':'judge','suiteSha256':l1.sha((root/'suite.json').read_bytes()),'promptSha256':l1.sha(prompt.encode()),'replySha256':l1.sha(text.encode())})
            failed=l1.score(root,replies)
            self.assertTrue(failed['complete']);self.assertFalse(failed['accepted']);self.assertTrue(failed['needsReview'])
            (replies/"lesson-test.0.txt").write_text("{}")
            self.assertFalse(l1.score(root,replies)["complete"])
if __name__ == "__main__": unittest.main()
