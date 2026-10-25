"""Full item-review evidence must not be squeezed into the author's 2,000-character field."""
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, patch

from backend.api.error_protocol import DomainApiError
from backend.llm.schemas.item_pydantic import ItemsResultV1
from backend.services.novel import first_chapter_item_repair as repair


class OpeningItemRepairTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        item = {"candidate_ref":"item_candidate_1","name":"旧粮账本","aliases":[],"item_type":"文书",
            "description":"开局已经存在的旧粮账本。","appearance":"","origin":"","abilities":[],"limitations":[],"costs":[],
            "owner_character_id":None,"controlling_faction_id":None,"current_location_id":None,
            "lifecycle_status":"available","story_function":"查清旧账","tags":[]}
        future = {**deepcopy(item),"candidate_ref":"item_candidate_2","name":"粮券母册","description":"登记未来才推行的粮券。"}
        self.envelope = {"candidate":{"items":[item,future]},"basis":{"generation_input":{"user_guidance":"作者原始要求","item_count":None}},
            "context":{"summary":"主角越狱并建立靖北之后才推行粮券。"}}
        issue={"message":"未来才形成的物品不能登记为书初事实。"*30,"candidate_quote":"未来才推行的粮券",
               "source_path":"context.summary","source_quote":"越狱并建立靖北之后才推行粮券。"}
        self.review={"summary":"8条完整冲突引证","issues":[deepcopy(issue) for _ in range(8)]}

    async def test_long_report_is_passed_in_full_without_changing_guidance_or_basis(self):
        before=deepcopy(self.envelope)
        result=ItemsResultV1.model_validate({"items":[self.envelope["candidate"]["items"][0]]})
        model=AsyncMock(return_value=(result,{"model":"test"}))
        self.assertGreater(len(json.dumps(self.review,ensure_ascii=False)),2000)
        with patch.object(repair,"structured_advice",model):
            candidate,metadata=await repair.repair_opening_items(self.envelope,self.review)
        self.assertEqual(len(candidate["items"]),1)
        self.assertEqual(self.envelope,before)
        material=model.call_args.args[2]
        self.assertEqual(material["review"],self.review)
        self.assertEqual(material["material"]["adoption_contract"]["position"],"book_start")
        self.assertEqual(self.envelope["basis"]["generation_input"]["user_guidance"],"作者原始要求")

    async def test_item_repair_cannot_add_or_reorder_candidate_refs(self):
        for change in ("add","reorder"):
            candidate=deepcopy(self.envelope["candidate"])
            if change=="add": candidate["items"][0]["candidate_ref"]="item_candidate_99"
            else: candidate["items"].reverse()
            model=AsyncMock(return_value=(ItemsResultV1.model_validate(candidate),{}))
            with self.subTest(change=change),patch.object(repair,"structured_advice",model),self.assertRaises(DomainApiError) as error:
                await repair.repair_opening_items(self.envelope,self.review)
            self.assertEqual(error.exception.code,"FIRST_CHAPTER_ITEM_REPAIR_SCOPE")

    async def test_invalid_evidence_never_sends_item_repair_request(self):
        self.review["issues"][0]["source_quote"]="不存在的原文"
        model=AsyncMock()
        with patch.object(repair,"structured_advice",model),self.assertRaises(DomainApiError):
            await repair.repair_opening_items(self.envelope,self.review)
        model.assert_not_awaited()
